# yt-dlp Downloader

A self-hosted web app for downloading audio from YouTube and other platforms, with Tidal lossless support via hifi-api, full metadata embedding, a download queue, download history, and optional Nextcloud sync.

## Features

- **Three download modes**
  - **Auto** — fetches YouTube title + artist, searches Tidal, shows top 5 results, falls back to yt-dlp
  - **Tidal** — search tracks or albums directly and download as FLAC
  - **yt-dlp** — direct URL download as Opus with thumbnail and metadata
- **Batch download** — paste multiple URLs (one per line) and all are added to the queue
- **Download queue** — sequential background processing with live progress
- **Playlist support** — toggle to download full YouTube playlists
- **Album search** — search for Tidal albums and download the whole thing in one click
- **Audio preview** — click ▶ on any track in the tracklist to preview it in the browser
- **Dark mode** — system-aware with manual toggle, saved in localStorage
- **Full Tidal metadata** — title, artists, album, track number, year, ISRC, copyright, 1280×1280 cover art embedded in FLAC
- **Multi-server hifi-api rotation** — 8 servers tried in order, failed ones skipped for 5 minutes
- **Server health panel** — live latency and status for all API servers
- **Quality selector** — Hi-Res FLAC, FLAC, AAC 320, AAC 96
- **Duplicate detection** — warns if file already exists in target folder
- **Folder auto-detection** — subfolders of `MUSIK_DIR` appear automatically as buttons
- **Download history** — logged to JSON, visible in the UI
- **yt-dlp update button** — update the binary from the UI
- **PWA** — installable as a home screen app on mobile
- **Nextcloud sync** — opt-in, runs after each completed download

## Requirements

- Python 3.8+
- `yt-dlp` binary
- `ffmpeg` (required for audio conversion and thumbnail embedding)
- `mutagen` (for Tidal FLAC metadata embedding)

```bash
pip install -r requirements.txt
```

## Setup

```bash
git clone https://github.com/YOUR_USERNAME/ytdlp-webapp.git
cd ytdlp-webapp
pip install -r requirements.txt
cp .env.example .env
nano .env
```

The app auto-detects all subdirectories inside `MUSIK_DIR` as target folders. No HTML editing required.

## Running

**Development:**
```bash
python3 app.py
```

**Production (recommended):**
```bash
gunicorn -w 1 -k gthread --threads 4 -b 0.0.0.0:5000 --timeout 120 app:app
```

Open **http://localhost:5000** or install as a PWA from your browser.

## systemd

```bash
sudo cp ytdlp-webapp.service /etc/systemd/system/
# Edit User and paths first
sudo systemctl daemon-reload
sudo systemctl enable --now ytdlp-webapp
```

## hifi-api Setup (recommended for reliable downloads)

The app ships with a pool of 8 public servers. For best results, run your own:

```bash
git clone https://github.com/binimum/hifi-api
cd hifi-api/tidal_auth && pip install -r requirements.txt
python3 tidal_auth.py           # log in with Tidal
cp token.json ../token.json
cd .. && pip install -r requirements.txt
python3 main.py                  # runs on localhost:8000
```

Set `HIFI_API_URL=http://localhost:8000` in `.env`.

## Nextcloud Sync

```env
NEXTCLOUD_ENABLED=true
NEXTCLOUD_USER=your_username
NEXTCLOUD_PASSWORD=your_app_password
NEXTCLOUD_URL=https://your-nextcloud.example.com/nextcloud
NEXTCLOUD_MUSIK_PATH=/Musik
```

Use an **app password** (Nextcloud → Settings → Security → App passwords), not your main password.

## Configuration Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `MUSIK_DIR` | Yes | `/musik` | Base music directory — subfolders become target buttons |
| `YTDLP_BIN` | Yes | `~/yt-dlp_linux` | Path to yt-dlp binary |
| `HIFI_API_URL` | No | built-in pool | Comma-separated hifi-api instance URLs |
| `HISTORY_FILE` | No | `../download_history.json` | Path to history JSON |
| `NEXTCLOUD_ENABLED` | No | `false` | Enable Nextcloud sync |
| `NEXTCLOUD_USER` | If enabled | — | Nextcloud username |
| `NEXTCLOUD_PASSWORD` | If enabled | — | App password |
| `NEXTCLOUD_URL` | If enabled | — | Full Nextcloud URL |
| `NEXTCLOUD_MUSIK_PATH` | If enabled | `/Musik` | Remote path to sync |

## Project Structure

```
ytdlp-webapp/
├── app.py
├── .env
├── .env.example
├── .gitignore
├── requirements.txt
├── ytdlp-webapp.service
└── static/
    ├── index.html
    ├── manifest.json     ← PWA manifest
    └── sw.js             ← Service worker
```

## Notes

- Never commit `.env`.
- One gunicorn worker is intentional — the queue and job state are in-memory.
- Expose publicly only behind a reverse proxy with authentication.
