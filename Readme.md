# yt-dlp Downloader

A self-hosted web app for downloading audio from YouTube and other platforms, with Tidal lossless support, full metadata embedding, a download queue, history, and optional Nextcloud sync.

## Features

- **Three download modes**
  - **Auto** — fetches the YouTube title and artist, searches Tidal, shows top 5 results to pick from, falls back to yt-dlp if not found
  - **Tidal** — search Tidal directly and download as FLAC with full metadata
  - **yt-dlp** — direct URL download as Opus with thumbnail and metadata
- **Full metadata on Tidal downloads** — title, all credited artists, album, track number, release year, ISRC, copyright, and 1280×1280 cover art embedded in the FLAC
- **Multi-server hifi-api rotation** — 8 servers tried in order, failed servers are skipped for 5 minutes
- **Quality selector** — Hi-Res FLAC, FLAC (16-bit/44.1kHz), AAC 320, AAC 96
- **Download queue** — add multiple items, processed sequentially in the background
- **Download history** — logged to a JSON file, visible in the UI with timestamps
- **Duplicate detection** — warns before downloading if the file already exists in the target folder
- **Folder auto-detection** — target folders are read directly from `MUSIK_DIR` at startup
- **yt-dlp update button** — update the binary with one click from the UI
- **Live terminal output** streamed into the browser for every job
- **Nextcloud sync** — opt-in via `.env`, runs automatically after each download

## Requirements

- Python 3.8+
- `yt-dlp` binary
- `ffmpeg` (required by yt-dlp for audio conversion and thumbnail embedding)
- Access to a hifi-api instance (public pool included by default)

```bash
pip install -r requirements.txt
```

## Setup

**1. Clone and install**
```bash
git clone https://github.com/YOUR_USERNAME/ytdlp-webapp.git
cd ytdlp-webapp
pip install -r requirements.txt
```

**2. Create your `.env`**
```bash
cp .env.example .env
nano .env
```

**3. Create your music folder structure**

The app auto-detects all subdirectories inside `MUSIK_DIR`:
```
/musik/
├── Pop/
├── Rock/
├── DnB/
└── ...
```

Any folder you create under `MUSIK_DIR` will appear automatically as a target button in the UI.

## Running

**Development:**
```bash
python3 app.py
```

**Production:**
```bash
gunicorn -w 1 -k gthread --threads 4 -b 0.0.0.0:5000 --timeout 120 app:app
```

Open **http://localhost:5000**.

## systemd Service

```bash
# Edit User and paths to match your setup
sudo cp ytdlp-webapp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ytdlp-webapp
sudo systemctl status ytdlp-webapp
```

## Tidal Metadata

Every Tidal FLAC download includes:

| Tag | Source |
|---|---|
| Title | Tidal track title |
| Artist | All credited artists (semicolon-separated) |
| Album | Album title |
| Track number | Position on album |
| Date | Release year |
| ISRC | International Standard Recording Code |
| Copyright | Copyright string |
| Cover art | 1280×1280 JPEG from Tidal CDN |

## hifi-api

The app ships with 8 public hifi-api servers tried in rotation. A server that returns 403 or 5xx is skipped for 5 minutes before being retried. You can override the pool via `.env`:

```env
HIFI_API_URL=https://your-instance.example.com,https://api.monochrome.tf
```

For reliable lossless downloads, running your own instance is recommended:

```bash
git clone https://github.com/binimum/hifi-api
cd hifi-api/tidal_auth
pip install -r requirements.txt
python3 tidal_auth.py   # follow the link and log in with Tidal
cp token.json ../token.json
cd ..
pip install -r requirements.txt
python3 main.py         # starts on localhost:8000
```

Then set `HIFI_API_URL=http://localhost:8000` in `.env`.

## Nextcloud Sync

Disabled by default. To enable, set in `.env`:

```env
NEXTCLOUD_ENABLED=true
NEXTCLOUD_USER=your_username
NEXTCLOUD_PASSWORD=your_app_password
NEXTCLOUD_URL=https://your-nextcloud.example.com/nextcloud
NEXTCLOUD_MUSIK_PATH=/Musik
```

Use an **app password** (Nextcloud → Settings → Security → App passwords).

If `nextcloudcmd` fails with IPv6 errors, force IPv4 via `/etc/hosts`:
```bash
echo "$(dig +short A your-nextcloud.example.com | head -1) your-nextcloud.example.com" | sudo tee -a /etc/hosts
```

## Configuration Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `MUSIK_DIR` | Yes | `/musik` | Local base directory — subfolders become target buttons |
| `YTDLP_BIN` | Yes | `~/yt-dlp_linux` | Path to yt-dlp binary |
| `HIFI_API_URL` | No | built-in pool | Comma-separated hifi-api instance URLs |
| `HISTORY_FILE` | No | `../download_history.json` | Path to download history JSON |
| `NEXTCLOUD_ENABLED` | No | `false` | Set `true` to enable sync |
| `NEXTCLOUD_USER` | If enabled | — | Nextcloud username |
| `NEXTCLOUD_PASSWORD` | If enabled | — | App password |
| `NEXTCLOUD_URL` | If enabled | — | Full Nextcloud URL |
| `NEXTCLOUD_MUSIK_PATH` | If enabled | `/Musik` | Remote Nextcloud path to sync |

## Project Structure

```
ytdlp-webapp/
├── app.py                  ← Flask/Gunicorn backend
├── .env                    ← Your config (never commit this)
├── .env.example            ← Template
├── .gitignore
├── requirements.txt
├── ytdlp-webapp.service    ← systemd unit file
└── static/
    └── index.html          ← Frontend
```

## Notes

- Never commit `.env` — it is listed in `.gitignore`.
- One gunicorn worker is intentional — the download queue and job state are in-memory and would break across multiple workers.
- For public exposure, put the app behind a reverse proxy (nginx/Caddy) with authentication.
