#!/usr/bin/env python3
"""
yt-dlp + Tidal Web Frontend
Production: gunicorn -w 1 -k gthread --threads 4 -b 0.0.0.0:5000 app:app
"""

import re
import subprocess
import threading
import queue
import json
import uuid
import os
import base64
import requests
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, request, jsonify, Response, send_from_directory

load_dotenv()

MUSIK_DIR             = os.getenv("MUSIK_DIR", "/musik")
HIFI_URL              = os.getenv("HIFI_API_URL", "https://api.monochrome.tf")
YTDLP_BIN             = os.getenv("YTDLP_BIN", str(Path.home() / "yt-dlp_linux"))

# Nextcloud — all optional, sync only runs if NEXTCLOUD_ENABLED=true
NC_ENABLED            = os.getenv("NEXTCLOUD_ENABLED", "false").lower() == "true"
NC_USER               = os.getenv("NEXTCLOUD_USER", "")
NC_PASSWORD           = os.getenv("NEXTCLOUD_PASSWORD", "")
NC_URL                = os.getenv("NEXTCLOUD_URL", "")
NC_REMOTE_PATH        = os.getenv("NEXTCLOUD_MUSIK_PATH", "/Musik")

app = Flask(__name__, static_folder="static")
jobs: dict = {}

AUDIO_EXTS = {".opus", ".mp3", ".flac", ".ogg", ".m4a", ".wav"}

def find_existing_file(folder, title):
    """Check if a file with the given title already exists in the folder (any audio ext)."""
    p = Path(folder)
    if not p.exists():
        return None
    title_lower = title.lower()
    for f in p.iterdir():
        if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
            if f.stem.lower() == title_lower:
                return f.name
    return None


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
    # exit code 1 = non-fatal sync warnings (bad filenames, server errors on unrelated files)
    return proc.returncode in (0, 1)


# ── yt-dlp helpers ────────────────────────────────────────────────────────────

def get_yt_info(url):
    """Returns (title, uploader) or (None, None)."""
    try:
        result = subprocess.run(
            [YTDLP_BIN, "--print", "%(title)s\n%(uploader)s", url],
            capture_output=True, text=True, timeout=45
        )
        lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
        title    = lines[0] if len(lines) > 0 else None
        uploader = lines[1] if len(lines) > 1 else None
        return title, uploader
    except Exception:
        return None, None


# ── Tidal helpers ─────────────────────────────────────────────────────────────

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


def _search_once(query):
    try:
        r = requests.get(f"{HIFI_URL}/search/", params={"s": query}, timeout=10)
        r.raise_for_status()
        return r.json().get("data", {}).get("items", [])
    except Exception:
        return []


def _best_result(items):
    blocklist = ["tribute", "cover", "karaoke", "made famous", "originally", "in the style"]
    candidates = [
        t for t in items
        if t.get("streamReady")
        and not any(b in t.get("title", "").lower() for b in blocklist)
        and not any(b in t.get("artist", {}).get("name", "").lower() for b in blocklist)
    ]
    pool = candidates if candidates else [t for t in items if t.get("streamReady")]
    if not pool:
        return None
    return max(pool, key=lambda t: t.get("popularity", 0))


def tidal_search(query):
    """Returns (id, title, artist, duration_sec) or None."""
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

    for q in queries:
        if not q:
            continue
        items = _search_once(q)
        if not items:
            continue
        best = _best_result(items)
        if best:
            return best["id"], best["title"], best["artist"]["name"], best.get("duration", 0)
    return None


def estimate_size(duration_sec, quality):
    kbps = {"HI_RES_LOSSLESS": 2000, "LOSSLESS": 850, "HIGH": 320, "LOW": 96}.get(quality, 850)
    return round(kbps * duration_sec / 8 / 1024, 1)


def tidal_get_download_url(track_id, quality="LOSSLESS", q=None):
    def log(msg):
        if q:
            q.put({"type": "log", "text": f"  [tidal] {msg}"})

    for ql in [quality, "HIGH", "LOW"]:
        try:
            r = requests.get(f"{HIFI_URL}/track/", params={"id": track_id, "quality": ql}, timeout=10)
            if r.status_code == 403:
                log(f"403 für {ql} — überspringe")
                continue
            r.raise_for_status()
            data = r.json().get("data", {})
            mime_type = data.get("manifestMimeType", "")
            manifest_b64 = data.get("manifest", "")
            if not manifest_b64:
                log(f"Kein Manifest für {ql}")
                continue
            padded = manifest_b64 + "=" * (-len(manifest_b64) % 4)
            manifest = json.loads(base64.b64decode(padded))
            if mime_type == "application/vnd.tidal.bts":
                urls = manifest.get("urls", [])
                if urls:
                    ext = "flac" if "flac" in manifest.get("codecs", "flac") else "m4a"
                    log(f"✓ Download-URL erhalten ({ql}, {ext})")
                    return urls[0], ext
                else:
                    log(f"Manifest hat keine URLs ({ql})")
            elif mime_type == "application/dash+xml":
                log(f"MPD-Manifest nicht unterstützt ({ql}) — nächste Qualität")
                continue
            else:
                log(f"Unbekannter mimeType: {mime_type}")
        except Exception as e:
            log(f"Fehler bei {ql}: {e}")
            continue
    return None


def tidal_download_file(url, dest_path, q):
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
                        q.put({"type": "log", "text": f"  {pct}% ({downloaded // 1024} / {total // 1024} KB)"})
    q.put({"type": "log", "text": "✓ Tidal-Datei gespeichert."})


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/config")
def config():
    return jsonify({
        "musik_dir":        MUSIK_DIR,
        "nextcloud_enabled": NC_ENABLED,
    })


@app.route("/api/files")
def list_files():
    folder = request.args.get("path", "").strip()
    if not folder:
        return jsonify({"files": []})
    p = Path(folder)
    if not p.exists() or not p.is_dir():
        return jsonify({"files": []})
    exts = {".opus", ".mp3", ".flac", ".ogg", ".m4a", ".wav"}
    files = sorted(f.name for f in p.iterdir() if f.is_file() and f.suffix.lower() in exts)
    return jsonify({"files": files})


@app.route("/api/preview", methods=["POST"])
def preview():
    data       = request.get_json(force=True)
    mode       = (data.get("mode") or "auto").strip()
    url        = (data.get("url") or "").strip()
    quality    = (data.get("quality") or "LOSSLESS").strip()
    output_dir = (data.get("output_dir") or "").strip()

    def generate():
        def msg(payload):
            return "data: " + json.dumps(payload) + "\n\n"

        if mode == "ytdlp":
            yield msg({"type": "skip"})
            return

        query = url
        if mode == "auto":
            yield msg({"type": "status", "text": "🔍 Infos von YouTube abrufen…"})
            title, uploader = get_yt_info(url)
            if not title:
                yield msg({"type": "fallback", "text": "⚠ Titel nicht abrufbar — starte yt-dlp…"})
                return
            query = f"{uploader} {title}" if uploader else title

        yield msg({"type": "status", "text": f"🔍 Suche auf Tidal: {query}"})
        result = tidal_search(query)
        if not result:
            if mode == "auto":
                yield msg({"type": "fallback", "text": "⚠ Nicht auf Tidal — starte yt-dlp…"})
            else:
                yield msg({"type": "error", "text": "❌ Kein Ergebnis auf Tidal gefunden."})
            return

        track_id, track_title, artist, duration = result
        est = estimate_size(duration, quality)
        # Check if file already exists
        safe_name = "".join(c for c in f"{artist} - {track_title}" if c not in r'\/:*?"<>|')
        existing = find_existing_file(output_dir, safe_name) if output_dir else None
        if not existing and output_dir:
            # Also check title-only match (yt-dlp fallback filenames)
            existing = find_existing_file(output_dir, track_title)
        yield msg({
            "type":     "result",
            "track_id": track_id,
            "title":    track_title,
            "artist":   artist,
            "duration": f"{duration // 60}:{duration % 60:02d}",
            "size_mb":  est,
            "quality":  quality,
            "existing": existing,
        })

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/download", methods=["POST"])
def start_download():
    data       = request.get_json(force=True)
    mode       = (data.get("mode") or "ytdlp").strip()
    url        = (data.get("url") or "").strip()
    output_dir = (data.get("output_dir") or "").strip()
    quality    = (data.get("quality") or "LOSSLESS").strip()

    if not url:
        return jsonify({"error": "Keine URL angegeben."}), 400
    if not output_dir:
        return jsonify({"error": "Kein Zielordner angegeben."}), 400

    expanded = Path(output_dir)
    try:
        expanded.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return jsonify({"error": f"Ordner konnte nicht erstellt werden: {e}"}), 500

    job_id = str(uuid.uuid4())[:8]
    q: queue.Queue = queue.Queue()

    def run():
        try:
            used_tidal = False

            # ── AUTO ──────────────────────────────────────────────────────
            if mode == "auto":
                q.put({"type": "log", "text": "🔍 Infos von YouTube abrufen…"})
                title, uploader = get_yt_info(url)
                search_query = f"{uploader} {title}" if (title and uploader) else title
                if search_query:
                    q.put({"type": "log", "text": f"  Titel: {title}"})
                    if uploader:
                        q.put({"type": "log", "text": f"  Interpret: {uploader}"})
                    q.put({"type": "log", "text": f"🔍 Suche auf Tidal: {search_query}"})
                    result = tidal_search(search_query)
                    if result:
                        track_id, track_title, artist, duration = result
                        est = estimate_size(duration, quality)
                        q.put({"type": "log", "text": f"✓ Gefunden: {artist} – {track_title}"})
                        q.put({"type": "log", "text": f"  ~{est} MB · {duration//60}:{duration%60:02d} min · {quality}"})
                        dl = tidal_get_download_url(track_id, quality=quality, q=q)
                        if dl:
                            dl_url, ext = dl
                            safe = "".join(c for c in f"{artist} - {track_title}" if c not in r'\/:*?"<>|')
                            tidal_download_file(dl_url, expanded / f"{safe}.{ext}", q)
                            used_tidal = True
                        else:
                            q.put({"type": "log", "text": "⚠ Kein Download-Link — falle auf yt-dlp zurück."})
                    else:
                        q.put({"type": "log", "text": "⚠ Nicht auf Tidal — falle auf yt-dlp zurück."})
                else:
                    q.put({"type": "log", "text": "⚠ Titel nicht abrufbar — falle auf yt-dlp zurück."})

            # ── TIDAL ─────────────────────────────────────────────────────
            elif mode == "tidal":
                q.put({"type": "log", "text": f"🔍 Suche auf Tidal: {url}"})
                result = tidal_search(url)
                if not result:
                    q.put({"type": "error", "text": "❌ Kein Ergebnis auf Tidal."})
                    return
                track_id, track_title, artist, duration = result
                est = estimate_size(duration, quality)
                q.put({"type": "log", "text": f"✓ Gefunden: {artist} – {track_title}"})
                q.put({"type": "log", "text": f"  ~{est} MB · {duration//60}:{duration%60:02d} min · {quality}"})
                dl = tidal_get_download_url(track_id, quality=quality, q=q)
                if not dl:
                    q.put({"type": "error", "text": "❌ Kein Download-Link von Tidal."})
                    return
                dl_url, ext = dl
                safe = "".join(c for c in f"{artist} - {track_title}" if c not in r'\/:*?"<>|')
                tidal_download_file(dl_url, expanded / f"{safe}.{ext}", q)
                used_tidal = True

            # ── YT-DLP (or fallback) ───────────────────────────────────────
            if not used_tidal:
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
                jobs[job_id]["process"] = proc
                for line in proc.stdout:
                    q.put({"type": "log", "text": line.rstrip()})
                proc.wait()
                # exit code 1 with --ignore-errors means non-fatal warnings, not a real failure
                if proc.returncode not in (0, 1):
                    q.put({"type": "error", "text": f"❌ yt-dlp Fehler (Exit-Code {proc.returncode})"})
                    return
                # Clean up leftover thumbnail files that would cause sync errors
                for leftover in expanded.glob("*.webp"):
                    try:
                        leftover.unlink()
                    except Exception:
                        pass

            q.put({"type": "done", "text": "✅ Download abgeschlossen!"})

            if NC_ENABLED:
                if not run_sync(q):
                    q.put({"type": "error", "text": "⚠ Sync fehlgeschlagen."})
                else:
                    q.put({"type": "done", "text": "☁ Sync abgeschlossen!"})

        except Exception as e:
            q.put({"type": "error", "text": f"❌ {e}"})
        finally:
            q.put(None)

    jobs[job_id] = {"queue": q, "process": None}
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/stream/<job_id>")
def stream(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job nicht gefunden."}), 404
    q = job["queue"]

    def generate():
        while True:
            msg = q.get()
            if msg is None:
                yield "data: " + json.dumps({"type": "end"}) + "\n\n"
                jobs.pop(job_id, None)
                break
            yield "data: " + json.dumps(msg) + "\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/cancel/<job_id>", methods=["POST"])
def cancel(job_id):
    job = jobs.get(job_id)
    if job and job.get("process"):
        job["process"].terminate()
        return jsonify({"status": "cancelled"})
    return jsonify({"error": "Kein aktiver Job."}), 404


if __name__ == "__main__":
    print("🎵  http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
