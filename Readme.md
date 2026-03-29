# yt-dlp Web Frontend

A locally hosted web app to download audio from YouTube and other platforms, with optional Tidal lossless support via a hifi-api instance and optional Nextcloud sync.

## Features

- Browser-based UI — no command line needed after setup
- Three download modes:
  - **Auto** — fetches title and artist from YouTube, searches Tidal first, falls back to yt-dlp if not found
  - **Tidal** — search Tidal directly and download as FLAC
  - **yt-dlp** — download from YouTube or other platforms as Opus
- Quality selector for Tidal: Hi-Res FLAC, FLAC, AAC 320, AAC 96
- Preview card before downloading — shows matched track, duration and estimated file size
- Smart Tidal search with multiple fallback strategies for obscure tracks
- Live terminal output streamed into the browser
- Track listing per folder, auto-refreshes after each download
- Nextcloud sync is **opt-in** via `.env`
- All paths configurable via `.env`

## Requirements

- Python 3.8+
- `yt-dlp` binary (see `YTDLP_BIN` in `.env.example`)
- Access to a hifi-api instance (default: `https://api.monochrome.tf`)
- `nextcloudcmd` installed — only needed if Nextcloud sync is enabled

```bash
pip install -r requirements.txt
```

## Setup

**1. Clone the repo and install dependencies**

```bash
git clone https://github.com/YOUR_USERNAME/ytdlp-webapp.git
cd ytdlp-webapp
pip install -r requirements.txt
```

**2. Create your `.env` from the example**

```bash
cp .env.example .env
```

Then edit `.env` with your values. The only required variables to get started are `MUSIK_DIR`, `YTDLP_BIN` and `HIFI_API_URL`.

**3. Adjust folder names in `static/index.html`**

The base path is loaded automatically from `MUSIK_DIR`. You only need to update the folder names:

```html
<button class="dir-btn" data-folder="Pop">Pop</button>
```

## Running

**Development:**

```bash
python3 app.py
```

**Production (recommended):**

```bash
gunicorn -w 1 -k gthread --threads 4 -b 0.0.0.0:5000 --timeout 120 app:app
```

Then open **http://localhost:5000**.

## Production Setup with systemd

A ready-made service file is included.

```bash
# Edit the service file to match your username and paths
nano ytdlp-webapp.service

# Install and enable
sudo cp ytdlp-webapp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ytdlp-webapp
sudo systemctl start ytdlp-webapp
sudo systemctl status ytdlp-webapp
```

## Enabling Nextcloud Sync

By default sync is disabled. To enable it, set the following in your `.env`:

```env
NEXTCLOUD_ENABLED=true
NEXTCLOUD_USER=your_username
NEXTCLOUD_PASSWORD=your_password_or_app_password
NEXTCLOUD_URL=https://your-nextcloud-instance.com/nextcloud
NEXTCLOUD_MUSIK_PATH=/Musik
```

It is recommended to use an **app password** rather than your main Nextcloud password. Generate one under Nextcloud → Settings → Security → App passwords.

If your container resolves hostnames via IPv6 but your server only has an IPv4 DNS entry, add a static entry to `/etc/hosts`:

```bash
dig +short A your-nextcloud-instance.com
echo "YOUR.IP.ADDRESS your-nextcloud-instance.com" | sudo tee -a /etc/hosts
```

## Configuration Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `MUSIK_DIR` | Yes | `/musik` | Local base directory for music subfolders |
| `YTDLP_BIN` | Yes | `~/yt-dlp_linux` | Path to yt-dlp binary |
| `HIFI_API_URL` | Yes | `https://api.monochrome.tf` | hifi-api instance URL |
| `NEXTCLOUD_ENABLED` | No | `false` | Set to `true` to enable sync |
| `NEXTCLOUD_USER` | If enabled | — | Nextcloud username |
| `NEXTCLOUD_PASSWORD` | If enabled | — | Nextcloud password or app password |
| `NEXTCLOUD_URL` | If enabled | — | Full URL to Nextcloud instance |
| `NEXTCLOUD_MUSIK_PATH` | If enabled | `/Musik` | Remote path in Nextcloud to sync |

## Project Structure

```
ytdlp-webapp/
├── app.py                  ← Flask/Gunicorn backend
├── .env                    ← Your config (never commit this)
├── .env.example            ← Template for .env
├── .gitignore
├── requirements.txt
├── ytdlp-webapp.service    ← systemd service file
└── static/
    └── index.html          ← Frontend UI
```

## Notes

- Never commit `.env` — it is listed in `.gitignore`.
- The app is designed for private network use. If you expose it publicly, put it behind a reverse proxy (nginx/Caddy) with authentication.
- One gunicorn worker is intentional — downloads run in background threads and shared state (`jobs` dict) would break across multiple workers.
