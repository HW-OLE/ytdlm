#!/usr/bin/env python3
"""
yt-dlp + Tidal Web Frontend
Production: gunicorn -w 1 -k gthread --threads 4 -b 0.0.0.0:5000 --timeout 120 app:app
"""

import re
import io
import time
import threading
import subprocess
import queue
import json
import uuid
import os
import base64
import struct
import requests
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv
from flask import Flask, request, jsonify, Response, send_from_directory

load_dotenv()

MUSIK_DIR      = os.getenv("MUSIK_DIR", "/musik")
YTDLP_BIN      = os.getenv("YTDLP_BIN", str(Path.home() / "yt-dlp_linux"))
HISTORY_FILE   = os.getenv("HISTORY_FILE", str(Path(MUSIK_DIR).parent / "download_history.json"))
NC_ENABLED     = os.getenv("NEXTCLOUD_ENABLED", "false").lower() == "true"
NC_USER        = os.getenv("NEXTCLOUD_USER", "")
NC_PASSWORD    = os.getenv("NEXTCLOUD_PASSWORD", "")
NC_URL         = os.getenv("NEXTCLOUD_URL", "")
NC_REMOTE_PATH = os.getenv("NEXTCLOUD_MUSIK_PATH", "/Musik")

_HIFI_SERVERS = [
    s.strip() for s in os.getenv("HIFI_API_URL", "").split(",") if s.strip()
] or [
    "https://api.monochrome.tf",
    "https://monochrome-api.samidy.com",
    "https://hifi.geeked.wtf",
    "https://wolf.qqdl.site",
    "https://maus.qqdl.site",
    "https://vogel.qqdl.site",
    "https://katze.qqdl.site",
    "https://hund.qqdl.site",
]

_server_failures: dict = {}
_SERVER_COOLDOWN = 300
_server_lock = threading.Lock()

def _get_servers():
    now = time.time()
    return sorted(_HIFI_SERVERS,
                  key=lambda u: 0 if (now - _server_failures.get(u, 0)) > _SERVER_COOLDOWN else 1)

def _mark_failed(url):
    with _server_lock:
        _server_failures[url] = time.time()

def _mark_ok(url):
    with _server_lock:
        _server_failures.pop(url, None)

def hifi_get(path, params=None, timeout=10):
    last_exc = None
    for url in _get_servers():
        try:
            r = requests.get(f"{url}{path}", params=params, timeout=timeout)
            if r.status_code in (403, 429) or r.status_code >= 500:
                _mark_failed(url)
                continue
            r.raise_for_status()
            _mark_ok(url)
            return r, url
        except Exception as e:
            _mark_failed(url)
            last_exc = str(e)
    raise Exception(f"All hifi-api servers failed. Last: {last_exc}")


app = Flask(__name__, static_folder="static")
AUDIO_EXTS = {".opus", ".mp3", ".flac", ".ogg", ".m4a", ".wav"}

# ── Download queue ────────────────────────────────────────────────────────────

_dl_queue: queue.Queue = queue.Queue()
_active_job: dict = {}          # currently running job info
_queue_items: list = []         # list of pending queue items (dicts)
_queue_lock = threading.Lock()
_jobs: dict = {}                # job_id -> {"queue": Queue, "process": Popen}


def _queue_worker():
    """Background thread: processes download queue one item at a time."""
    while True:
        item = _dl_queue.get()
        job_id   = item["job_id"]
        q        = item["stream_queue"]
        with _queue_lock:
            global _active_job
            _active_job = item
        try:
            _execute_download(item, job_id, q)
        except Exception as e:
            q.put({"type": "error", "text": f"❌ {e}"})
        finally:
            q.put(None)
            with _queue_lock:
                _active_job = {}
                _queue_items[:] = [i for i in _queue_items if i["job_id"] != job_id]
        _dl_queue.task_done()


threading.Thread(target=_queue_worker, daemon=True).start()


# ── History ───────────────────────────────────────────────────────────────────

def _load_history() -> list:
    try:
        p = Path(HISTORY_FILE)
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return []


def _save_history(entries: list):
    try:
        Path(HISTORY_FILE).parent.mkdir(parents=True, exist_ok=True)
        Path(HISTORY_FILE).write_text(json.dumps(entries, indent=2, ensure_ascii=False))
    except Exception:
        pass


def _add_history(title, artist, mode, folder, source="tidal", quality=None):
    entries = _load_history()
    entries.insert(0, {
        "title":     title,
        "artist":    artist,
        "mode":      mode,
        "source":    source,
        "folder":    folder,
        "quality":   quality or "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    entries = entries[:500]  # keep last 500
    _save_history(entries)


# ── Nextcloud sync ────────────────────────────────────────────────────────────

def run_sync(q):
    if not NC_ENABLED:
        return True
    if not all([NC_USER, NC_PASSWORD, NC_URL]):
        q.put({"type": "log", "text": "⚠ Nextcloud nicht konfiguriert — Sync übersprungen."})
        return True
    q.put({"type": "log", "text": "⟳ Nextcloud Sync läuft…"})
    cmd = ["nextcloudcmd", "--user", NC_USER, "--password", NC_PASSWORD,
           "--path", NC_REMOTE_PATH, f"{MUSIK_DIR}/", NC_URL]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    keep = ["error", "warning", "upload", "download", "conflict", "finish", "aborted"]
    for line in proc.stdout:
        l = line.rstrip()
        if any(k in l.lower() for k in keep):
            q.put({"type": "log", "text": l})
    proc.wait()
    return proc.returncode in (0, 1)


# ── yt-dlp ────────────────────────────────────────────────────────────────────

def get_yt_info(url):
    try:
        result = subprocess.run(
            [YTDLP_BIN, "--print", "%(title)s\n%(uploader)s", url],
            capture_output=True, text=True, timeout=45
        )
        lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
        return (lines[0] if lines else None), (lines[1] if len(lines) > 1 else None)
    except Exception:
        return None, None


def run_ytdlp(url, expanded, job_id, q):
    cmd = [
        YTDLP_BIN, "-x",
        "--audio-format", "opus",
        "--embed-thumbnail",
        "--convert-thumbnails", "jpg",
        "--add-metadata",
        "--parse-metadata", "title:%(title)s",
        "--parse-metadata", "uploader:%(artist)s",
        "--output", str(expanded / "%(title)s.%(ext)s"),
        "--no-overwrites",
        "--ignore-errors",
        "--no-post-overwrites",
        url,
    ]
    q.put({"type": "log", "text": "▶ yt-dlp startet…"})
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    _jobs[job_id]["process"] = proc
    for line in proc.stdout:
        q.put({"type": "log", "text": line.rstrip()})
    proc.wait()
    for leftover in expanded.glob("*.webp"):
        try:
            leftover.unlink()
        except Exception:
            pass
    return proc.returncode in (0, 1)


# ── Tidal ─────────────────────────────────────────────────────────────────────

def _clean_query(query):
    noise = [
        r'\(Official.*?\)', r'\(Music.*?\)', r'\(Lyric.*?\)', r'\(Audio.*?\)',
        r'\(Video.*?\)', r'\(HD.*?\)', r'\(HQ.*?\)', r'\(prod\..*?\)',
        r'\[Official.*?\]', r'\[Music.*?\]', r'\[Lyric.*?\]', r'\[Audio.*?\]',
        r'\[prod\..*?\]', r'\[.*?Records.*?\]',
        r'ft\..*', r'feat\..*',
    ]
    for pattern in noise:
        query = re.sub(pattern, '', query, flags=re.IGNORECASE)
    return query.strip()


def tidal_search(query, limit=5):
    cleaned = _clean_query(query)
    queries = [query]
    if cleaned.lower() != query.lower():
        queries.append(cleaned)
    if " - " in query:
        parts = query.split(" - ", 1)
        artist_part = _clean_query(parts[0]).strip()
        title_part  = _clean_query(parts[1]).strip()
        queries += [f"{title_part} {artist_part}", title_part, artist_part]
    words = cleaned.split()
    if len(words) > 3:
        queries.append(" ".join(words[:3]))

    seen_ids = set()
    results  = []
    blocklist = ["tribute", "cover", "karaoke", "made famous", "originally", "in the style"]

    for q in queries:
        if not q or len(results) >= limit:
            break
        try:
            r, _ = hifi_get("/search/", params={"s": q})
            items = r.json().get("data", {}).get("items", [])
        except Exception:
            continue
        for t in items:
            if len(results) >= limit:
                break
            if not t.get("streamReady") or t["id"] in seen_ids:
                continue
            if any(b in t.get("title", "").lower() for b in blocklist):
                continue
            if any(b in t.get("artist", {}).get("name", "").lower() for b in blocklist):
                continue
            seen_ids.add(t["id"])
            dur = t.get("duration", 0)
            results.append({
                "id":           t["id"],
                "title":        t["title"],
                "artist":       t["artist"]["name"],
                "duration":     f"{dur // 60}:{dur % 60:02d}",
                "duration_sec": dur,
                "popularity":   t.get("popularity", 0),
                "source":       "tidal",
            })

    return results


def estimate_size(duration_sec, quality):
    kbps = {"HI_RES_LOSSLESS": 2000, "LOSSLESS": 850, "HIGH": 320, "LOW": 96}.get(quality, 850)
    return round(kbps * duration_sec / 8 / 1024, 1)


def tidal_get_metadata(track_id):
    try:
        r, _ = hifi_get("/info/", params={"id": track_id}, timeout=10)
        data = r.json().get("data", {})
        cover_uuid = data.get("album", {}).get("cover", "")
        cover_url  = None
        if cover_uuid:
            cover_url = f"https://resources.tidal.com/images/{cover_uuid.replace('-', '/')}/1280x1280.jpg"
        artists = [a.get("name", "") for a in data.get("artists", []) if a.get("name")]
        return {
            "title":       data.get("title", ""),
            "artist":      "; ".join(artists) if artists else data.get("artist", {}).get("name", ""),
            "album":       data.get("album", {}).get("title", ""),
            "tracknumber": str(data.get("trackNumber", "")),
            "date":        (data.get("streamStartDate") or "")[:4],
            "copyright":   data.get("copyright", ""),
            "isrc":        data.get("isrc", ""),
            "cover_url":   cover_url,
        }
    except Exception:
        return None


def tidal_embed_metadata(dest_path, metadata, q):
    try:
        from mutagen.flac import FLAC, Picture

        audio = FLAC(str(dest_path))

        if metadata.get("title"):        audio["title"]       = [metadata["title"]]
        if metadata.get("artist"):       audio["artist"]      = [metadata["artist"]]
        if metadata.get("album"):        audio["album"]       = [metadata["album"]]
        if metadata.get("tracknumber"):  audio["tracknumber"] = [metadata["tracknumber"]]
        if metadata.get("date"):         audio["date"]        = [metadata["date"]]
        if metadata.get("copyright"):    audio["copyright"]   = [metadata["copyright"]]
        if metadata.get("isrc"):         audio["isrc"]        = [metadata["isrc"]]

        if metadata.get("cover_url"):
            try:
                r = requests.get(metadata["cover_url"], timeout=15)
                r.raise_for_status()
                pic = Picture()
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.data = r.content
                img_data = io.BytesIO(r.content)
                img_data.read(2)
                while True:
                    marker, = struct.unpack(">H", img_data.read(2))
                    length,  = struct.unpack(">H", img_data.read(2))
                    if marker in (0xFFC0, 0xFFC2):
                        img_data.read(1)
                        h, w = struct.unpack(">HH", img_data.read(4))
                        pic.width  = w
                        pic.height = h
                        break
                    img_data.read(length - 2)
                audio.clear_pictures()
                audio.add_picture(pic)
                q.put({"type": "log", "text": f"  🎨 Cover eingebettet ({pic.width}×{pic.height})"})
            except Exception as e:
                q.put({"type": "log", "text": f"  ⚠ Cover konnte nicht geladen werden: {e}"})

        audio.save()
        q.put({"type": "log", "text": "  ✓ Metadaten eingebettet"})
    except ImportError:
        q.put({"type": "log", "text": "  ⚠ mutagen nicht installiert — pip install mutagen"})
    except Exception as e:
        q.put({"type": "log", "text": f"  ⚠ Metadaten-Fehler: {e}"})


def tidal_get_download_url(track_id, quality="LOSSLESS", q=None):
    def log(msg):
        if q:
            q.put({"type": "log", "text": f"  [tidal] {msg}"})

    for ql in [quality, "HIGH", "LOW"]:
        for server in _get_servers():
            try:
                r = requests.get(f"{server}/track/",
                                 params={"id": track_id, "quality": ql}, timeout=10)
                if r.status_code == 403 or r.status_code >= 500:
                    _mark_failed(server)
                    continue
                r.raise_for_status()
                data         = r.json().get("data", {})
                mime_type    = data.get("manifestMimeType", "")
                manifest_b64 = data.get("manifest", "")
                if not manifest_b64:
                    continue
                padded   = manifest_b64 + "=" * (-len(manifest_b64) % 4)
                manifest = json.loads(base64.b64decode(padded))
                if mime_type == "application/vnd.tidal.bts":
                    urls = manifest.get("urls", [])
                    if urls:
                        ext = "flac" if "flac" in manifest.get("codecs", "flac") else "m4a"
                        log(f"✓ {server.split('//')[1]} · {ql} · {ext}")
                        _mark_ok(server)
                        return urls[0], ext
                elif mime_type == "application/dash+xml":
                    log(f"MPD nicht unterstützt ({ql})")
                    break
            except Exception as e:
                _mark_failed(server)
                log(f"Fehler von {server.split('//')[1]}: {e}")
                continue
    return None


def tidal_download_file(url, dest_path, q, track_id=None, metadata=None):
    q.put({"type": "log", "text": f"⬇ Tidal Download: {dest_path.name}"})
    r = requests.get(url, stream=True, timeout=60)
    r.raise_for_status()
    total = int(r.headers.get("content-length", 0))
    downloaded = 0
    last_pct = -1
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = int(downloaded / total * 100)
                    if pct // 10 != last_pct // 10:
                        last_pct = pct
                        q.put({"type": "log", "text":
                               f"  {pct}% ({downloaded // 1024} / {total // 1024} KB)"})
    q.put({"type": "log", "text": "✓ Tidal-Datei gespeichert."})
    if dest_path.suffix.lower() == ".flac":
        if metadata is None and track_id:
            q.put({"type": "log", "text": "  🔍 Metadaten abrufen…"})
            metadata = tidal_get_metadata(track_id)
        if metadata:
            tidal_embed_metadata(dest_path, metadata, q)


# ── Core download executor ────────────────────────────────────────────────────

def find_existing_file(folder, title):
    p = Path(folder)
    if not p.exists():
        return None
    title_lower = title.lower()
    for f in p.iterdir():
        if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
            if f.stem.lower() == title_lower:
                return f.name
    return None


def _execute_download(item, job_id, q):
    """Core download logic, called by queue worker."""
    mode         = item.get("mode", "ytdlp")
    url          = item.get("url", "")
    output_dir   = item.get("output_dir", "")
    quality      = item.get("quality", "LOSSLESS")
    track_id     = item.get("track_id")
    track_title  = item.get("track_title", "")
    track_artist = item.get("track_artist", "")

    expanded = Path(output_dir)
    expanded.mkdir(parents=True, exist_ok=True)

    used_tidal = False

    # ── Selected Tidal track ──────────────────────────────────────────────────
    if track_id:
        q.put({"type": "log", "text": f"⬇ Tidal: {track_artist} – {track_title}"})
        dl = tidal_get_download_url(track_id, quality=quality, q=q)
        if dl:
            dl_url, ext = dl
            safe = "".join(c for c in f"{track_artist} - {track_title}"
                           if c not in r'\/:*?"<>|')
            tidal_download_file(dl_url, expanded / f"{safe}.{ext}", q, track_id=track_id)
            _add_history(track_title, track_artist, mode, output_dir, "tidal", quality)
            used_tidal = True
        else:
            q.put({"type": "log", "text": "⚠ Kein Tidal-Link — falle auf yt-dlp zurück."})

    # ── Auto mode: try Tidal first ────────────────────────────────────────────
    elif mode == "auto" and url:
        q.put({"type": "log", "text": "🔍 Infos von YouTube abrufen…"})
        title, uploader = get_yt_info(url)
        search_query = f"{uploader} {title}" if (title and uploader) else title
        if search_query:
            q.put({"type": "log", "text": f"  Titel: {title}, Interpret: {uploader}"})
            results = tidal_search(search_query, limit=1)
            if results:
                best = results[0]
                q.put({"type": "log", "text": f"✓ Tidal: {best['artist']} – {best['title']}"})
                dl = tidal_get_download_url(best["id"], quality=quality, q=q)
                if dl:
                    dl_url, ext = dl
                    safe = "".join(c for c in f"{best['artist']} - {best['title']}"
                                   if c not in r'\/:*?"<>|')
                    tidal_download_file(dl_url, expanded / f"{safe}.{ext}", q,
                                        track_id=best["id"])
                    _add_history(best["title"], best["artist"], mode, output_dir,
                                 "tidal", quality)
                    used_tidal = True
                else:
                    q.put({"type": "log", "text": "⚠ Kein Download-Link — falle auf yt-dlp zurück."})
            else:
                q.put({"type": "log", "text": "⚠ Nicht auf Tidal — falle auf yt-dlp zurück."})
        else:
            q.put({"type": "log", "text": "⚠ Titel nicht abrufbar — falle auf yt-dlp zurück."})

    # ── yt-dlp (direct or fallback) ───────────────────────────────────────────
    if not used_tidal:
        if not url:
            q.put({"type": "error", "text": "❌ Keine URL angegeben."})
            return
        ok = run_ytdlp(url, expanded, job_id, q)
        if not ok:
            q.put({"type": "error", "text": "❌ yt-dlp Fehler."})
            return
        _add_history(url, "", mode, output_dir, "ytdlp", None)

    q.put({"type": "done", "text": "✅ Download abgeschlossen!"})

    if NC_ENABLED:
        if not run_sync(q):
            q.put({"type": "error", "text": "⚠ Sync fehlgeschlagen."})
        else:
            q.put({"type": "done", "text": "☁ Sync abgeschlossen!"})


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/config")
def config():
    now = time.time()
    return jsonify({
        "musik_dir":         MUSIK_DIR,
        "nextcloud_enabled": NC_ENABLED,
        "servers": [
            {"url": u, "healthy": (now - _server_failures.get(u, 0)) > _SERVER_COOLDOWN}
            for u in _HIFI_SERVERS
        ],
    })


@app.route("/api/files")
def list_files():
    folder = request.args.get("path", "").strip()
    if not folder:
        return jsonify({"files": []})
    p = Path(folder)
    if not p.exists() or not p.is_dir():
        return jsonify({"files": []})
    files = sorted(f.name for f in p.iterdir()
                   if f.is_file() and f.suffix.lower() in AUDIO_EXTS)
    return jsonify({"files": files})


@app.route("/api/folders")
def list_folders():
    """Return all subdirectories of MUSIK_DIR."""
    p = Path(MUSIK_DIR)
    if not p.exists():
        return jsonify({"folders": []})
    folders = sorted(f.name for f in p.iterdir() if f.is_dir() and not f.name.startswith('.'))
    return jsonify({"folders": folders})


@app.route("/api/search", methods=["POST"])
def search():
    data       = request.get_json(force=True)
    mode       = (data.get("mode") or "tidal").strip()
    query      = (data.get("query") or "").strip()
    quality    = (data.get("quality") or "LOSSLESS").strip()
    output_dir = (data.get("output_dir") or "").strip()

    def generate():
        def msg(payload):
            return "data: " + json.dumps(payload) + "\n\n"

        if mode == "ytdlp":
            yield msg({"type": "skip"})
            return

        search_query = query

        if mode == "auto":
            yield msg({"type": "status", "text": "🔍 Infos von YouTube abrufen…"})
            title, uploader = get_yt_info(query)
            if not title:
                yield msg({"type": "fallback", "text": "⚠ Titel nicht abrufbar — direkt zur Queue."})
                return
            search_query = f"{uploader} {title}" if uploader else title

        yield msg({"type": "status", "text": f"🔍 Suche auf Tidal: {search_query}"})
        results = tidal_search(search_query, limit=5)

        if not results:
            if mode == "auto":
                yield msg({"type": "fallback", "text": "⚠ Nicht auf Tidal — direkt zur Queue."})
            else:
                yield msg({"type": "error", "text": "❌ Keine Ergebnisse auf Tidal."})
            return

        for track in results:
            track["size_mb"] = estimate_size(track["duration_sec"], quality)
            safe = "".join(c for c in f"{track['artist']} - {track['title']}"
                           if c not in r'\/:*?"<>|')
            existing = find_existing_file(output_dir, safe) if output_dir else None
            if not existing and output_dir:
                existing = find_existing_file(output_dir, track["title"])
            track["existing"] = existing

        yield msg({"type": "results", "tracks": results, "quality": quality})

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Queue routes ──────────────────────────────────────────────────────────────

@app.route("/api/queue", methods=["GET"])
def get_queue():
    with _queue_lock:
        active = dict(_active_job) if _active_job else None
        pending = list(_queue_items)
    return jsonify({"active": active, "pending": pending})


@app.route("/api/queue/add", methods=["POST"])
def queue_add():
    data = request.get_json(force=True)
    job_id = str(uuid.uuid4())[:8]
    sq: queue.Queue = queue.Queue()

    item = {
        "job_id":       job_id,
        "mode":         (data.get("mode") or "ytdlp").strip(),
        "url":          (data.get("url") or "").strip(),
        "output_dir":   (data.get("output_dir") or "").strip(),
        "quality":      (data.get("quality") or "LOSSLESS").strip(),
        "track_id":     data.get("track_id"),
        "track_title":  data.get("track_title", ""),
        "track_artist": data.get("track_artist", ""),
        "stream_queue": sq,
        "label":        data.get("label", data.get("url", "Download")),
        "status":       "queued",
    }

    if not item["output_dir"]:
        return jsonify({"error": "Kein Zielordner angegeben."}), 400

    _jobs[job_id] = {"queue": sq, "process": None}
    with _queue_lock:
        _queue_items.append({k: v for k, v in item.items() if k != "stream_queue"})
    _dl_queue.put(item)
    return jsonify({"job_id": job_id, "position": _dl_queue.qsize()})


@app.route("/api/stream/<job_id>")
def stream(job_id):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job nicht gefunden."}), 404
    sq = job["queue"]

    def generate():
        while True:
            msg = sq.get()
            if msg is None:
                yield "data: " + json.dumps({"type": "end"}) + "\n\n"
                _jobs.pop(job_id, None)
                break
            yield "data: " + json.dumps(msg) + "\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/cancel/<job_id>", methods=["POST"])
def cancel(job_id):
    job = _jobs.get(job_id)
    if job and job.get("process"):
        job["process"].terminate()
        return jsonify({"status": "cancelled"})
    # Remove from pending queue if not yet started
    with _queue_lock:
        before = len(_queue_items)
        _queue_items[:] = [i for i in _queue_items if i["job_id"] != job_id]
        removed = len(_queue_items) < before
    if removed:
        return jsonify({"status": "removed from queue"})
    return jsonify({"error": "Job nicht gefunden."}), 404


# ── History routes ────────────────────────────────────────────────────────────

@app.route("/api/history", methods=["GET"])
def get_history():
    return jsonify({"entries": _load_history()})


@app.route("/api/history/clear", methods=["POST"])
def clear_history():
    _save_history([])
    return jsonify({"status": "cleared"})


# ── yt-dlp update route ───────────────────────────────────────────────────────

@app.route("/api/update-ytdlp", methods=["POST"])
def update_ytdlp():
    def generate():
        def msg(payload):
            return "data: " + json.dumps(payload) + "\n\n"
        try:
            yield msg({"type": "log", "text": f"⟳ Starte yt-dlp Update: {YTDLP_BIN}"})
            proc = subprocess.Popen(
                [YTDLP_BIN, "-U"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1
            )
            for line in proc.stdout:
                yield msg({"type": "log", "text": line.rstrip()})
            proc.wait()
            if proc.returncode == 0:
                yield msg({"type": "done", "text": "✅ yt-dlp ist aktuell."})
            else:
                yield msg({"type": "error", "text": f"⚠ Update beendet mit Code {proc.returncode}"})
        except Exception as e:
            yield msg({"type": "error", "text": f"❌ {e}"})
        finally:
            yield msg({"type": "end"})

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    print("🎵  http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
