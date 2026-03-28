#!/usr/bin/env python3
"""
yt-dlp + Tidal (hifi-api) Web Frontend
Start: python3 app.py  (from the folder containing static/)
"""

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

NC_USER     = os.getenv("NEXTCLOUD_USER")
NC_PASSWORD = os.getenv("NEXTCLOUD_PASSWORD")
NC_URL      = os.getenv("NEXTCLOUD_URL")
HIFI_URL    = os.getenv("HIFI_API_URL", "http://localhost:8000")
MUSIK_DIR   = os.getenv("MUSIK_DIR", "/musik")

app = Flask(__name__, static_folder="static")
jobs: dict = {}


# ── Nextcloud sync ────────────────────────────────────────────────────────────

def run_sync(q):
    q.put({"type": "log", "text": "⟳ nextcloudcmd läuft…"})
    cmd = ["nextcloudcmd", "--user", NC_USER, "--password", NC_PASSWORD,
           "--path", os.getenv("NEXTCLOUD_MUSIK_PATH", "/Musik"), f"{MUSIK_DIR}/", NC_URL]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    keep = ["error", "warning", "upload", "download", "conflict", "finish", "aborted"]
    for line in proc.stdout:
        l = line.rstrip()
        if any(k in l.lower() for k in keep):
            q.put({"type": "log", "text": l})
    proc.wait()
    return proc.returncode == 0


# ── Tidal helpers ─────────────────────────────────────────────────────────────

def _clean_query(query):
    """Strip common YouTube title noise to get a cleaner search term."""
    import re
    # Remove parenthetical/bracketed noise
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
    """Single Tidal search, returns list of items or []."""
    try:
        r = requests.get(f"{HIFI_URL}/search/", params={"s": query}, timeout=10)
        r.raise_for_status()
        return r.json().get("data", {}).get("items", [])
    except Exception:
        return []


def _best_result(items):
    """Pick best streamable non-cover result by popularity."""
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
    """Returns (id, title, artist, duration_sec) or None.
    Tries multiple query strategies to handle obscure/non-English tracks."""

    queries_to_try = []
    cleaned = _clean_query(query)

    # Strategy 1: original query as-is (e.g. "LOKIMITDERMASKE BMW")
    queries_to_try.append(query)

    # Strategy 2: cleaned query (strip YouTube noise like "(Official Video)")
    if cleaned and cleaned.lower() != query.lower():
        queries_to_try.append(cleaned)

    # Strategy 3: if "artist - title" format, try "title artist" (Tidal prefers title first)
    if " - " in query:
        parts = query.split(" - ", 1)
        artist_part = _clean_query(parts[0]).strip()
        title_part  = _clean_query(parts[1]).strip()
        queries_to_try.append(f"{title_part} {artist_part}")
        # Strategy 4: title only
        queries_to_try.append(title_part)
        # Strategy 5: artist only (last resort for very specific artists)
        queries_to_try.append(artist_part)

    # Strategy 6: first 3 words of cleaned query
    words = cleaned.split()
    if len(words) > 3:
        queries_to_try.append(" ".join(words[:3]))

    # Try each strategy, return first good result
    for q in queries_to_try:
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


def tidal_get_download_url(track_id, quality="LOSSLESS"):
    """Returns (url, ext) or None."""
    for ql in [quality, "HIGH", "LOW"]:
        try:
            r = requests.get(f"{HIFI_URL}/track/", params={"id": track_id, "quality": ql}, timeout=10)
            r.raise_for_status()
            data = r.json().get("data", {})
            mime_type = data.get("manifestMimeType", "")
            manifest_b64 = data.get("manifest", "")
            padded = manifest_b64 + "=" * (-len(manifest_b64) % 4)
            manifest = json.loads(base64.b64decode(padded))
            if mime_type == "application/vnd.tidal.bts":
                urls = manifest.get("urls", [])
                if urls:
                    ext = "flac" if "flac" in manifest.get("codecs", "flac") else "m4a"
                    return urls[0], ext
        except Exception:
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
    return jsonify({"musik_dir": MUSIK_DIR})


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
    data    = request.get_json(force=True)
    mode    = (data.get("mode") or "auto").strip()
    url     = (data.get("url") or "").strip()
    quality = (data.get("quality") or "LOSSLESS").strip()

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
        else:
            pass

        if mode != "auto":
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
        yield msg({
            "type": "result",
            "track_id": track_id,
            "title":    track_title,
            "artist":   artist,
            "duration": f"{duration // 60}:{duration % 60:02d}",
            "size_mb":  est,
            "quality":  quality,
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
                        dl = tidal_get_download_url(track_id, quality=quality)
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
                dl = tidal_get_download_url(track_id, quality=quality)
                if not dl:
                    q.put({"type": "error", "text": "❌ Kein Download-Link von Tidal."})
                    return
                dl_url, ext = dl
                safe = "".join(c for c in f"{artist} - {track_title}" if c not in r'\/:*?"<>|')
                tidal_download_file(dl_url, expanded / f"{safe}.{ext}", q)
                used_tidal = True

            # ── YT-DLP (or fallback) ───────────────────────────────────────
            if not used_tidal:
                yt_dlp = str(Path.home() / "yt-dlp_linux")
                cmd = [
                    yt_dlp, "-x",
                    "--audio-format", "opus",
                    "--embed-thumbnail",
                    "--convert-thumbnails", "jpg",
                    "--add-metadata",
                    "--metadata-from-title", "%(title)s",
                    "--parse-metadata", "title:%(title)s",
                    "--parse-metadata", "uploader:%(artist)s",
                    "--output", str(expanded / "%(title)s.%(ext)s"),
                    "--no-overwrites",
                    "--ignore-errors",
                    url,
                ]
                q.put({"type": "log", "text": "▶ yt-dlp startet…"})
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, bufsize=1)
                jobs[job_id]["process"] = proc
                for line in proc.stdout:
                    q.put({"type": "log", "text": line.rstrip()})
                proc.wait()
                if proc.returncode != 0:
                    q.put({"type": "error", "text": f"❌ yt-dlp Fehler (Exit-Code {proc.returncode})"})
                    return

            q.put({"type": "done", "text": "✅ Download abgeschlossen!"})

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
