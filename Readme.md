# yt-dlp Web Frontend

A minimal locally hosted web app to download audio from YouTube and other platforms, with Tidal lossless support via a hifi-api instance and automatic Nextcloud sync after each download.

## Features

- Browser-based UI — no command line needed after setup
- Three download modes:
  - **Auto** — fetches the YouTube title, searches Tidal first, falls back to yt-dlp if not found
  - **Tidal** — search Tidal directly and download as FLAC
  - **yt-dlp** — download from YouTube or other platforms as Opus
- Quality selector for Tidal downloads: Hi-Res FLAC, FLAC, AAC 320, AAC 96
- Track preview before downloading — shows title, artist, duration and estimated file size
- Live terminal output streamed directly into the browser
- Automatically syncs to Nextcloud after each download
- Track listing per folder — updates automatically after each download
- All paths and credentials configurable via `.env`

## Requirements

- Python 3.8+
- `yt-dlp_linux` binary in your home directory (`~/yt-dlp_linux`)
- `nextcloudcmd` installed on the system
- Access to a running hifi-api instance (e.g. `https://api.monochrome.tf`)

```bash
pip install flask python-dotenv requests
chmod +x ~/yt-dlp_linux
```

## Setup

1. Clone or copy the project folder to your server or container.

2. Create a `.env` file in the project root:

```env
NEXTCLOUD_USER=your_username
NEXTCLOUD_PASSWORD=your_password
NEXTCLOUD_URL=https://your-nextcloud-instance.com/nextcloud
NEXTCLOUD_MUSIK_PATH=/Musik
HIFI_API_URL=https://api.monochrome.tf
MUSIK_DIR=/musik
```

Variable reference:

| Variable | Description |
|---|---|
| `NEXTCLOUD_USER` | Nextcloud login username |
| `NEXTCLOUD_PASSWORD` | Nextcloud login password or app password |
| `NEXTCLOUD_URL` | Full URL to your Nextcloud instance |
| `NEXTCLOUD_MUSIK_PATH` | Remote path in Nextcloud to sync (default: `/Musik`) |
| `HIFI_API_URL` | URL of the hifi-api instance for Tidal |
| `MUSIK_DIR` | Local base directory containing your music subfolders |

3. Adjust the folder names in `static/index.html` to match your subdirectories. The base path is loaded automatically from `MUSIK_DIR`:

```html
<button class="dir-btn" data-folder="Pop">Pop</button>
```

4. If your system resolves hostnames via IPv6 but your server only has an IPv4 DNS entry, add a static entry to `/etc/hosts`:

```bash
dig +short A your-nextcloud-instance.com
echo "YOUR.IP.ADDRESS your-nextcloud-instance.com" | sudo tee -a /etc/hosts
```

## Usage

Start the server from inside the project folder:

```bash
cd ytdlp-webapp
python3 app.py
```

Then open **http://localhost:5000** in your browser.

1. Select a download mode (Auto / Tidal / yt-dlp)
2. Select audio quality (for Tidal/Auto modes)
3. Paste a URL or search query
4. Click **Download starten** — a preview card appears showing the matched track and estimated size
5. Confirm to start the download
6. Watch live output in the terminal panel
7. Nextcloud sync runs automatically when done

## Project Structure

```
ytdlp-webapp/
├── app.py              ← Flask backend
├── .env                ← Credentials and config (never commit this)
├── .gitignore
├── requirements.txt
└── static/
    └── index.html      ← Frontend UI
```

## Notes

- The `.env` file is excluded from version control via `.gitignore`. Never commit credentials to a repository.
- The app is intended for local or private network use only. It runs Flask's development server — do not expose it to the public internet without a proper WSGI setup and authentication.
- Tidal downloads require access to a working hifi-api instance with a valid Tidal session token.
- For IPv6 issues with `nextcloudcmd` in LXC containers, disable IPv6:
  ```bash
  sudo sysctl -w net.ipv6.conf.all.disable_ipv6=1
  ```
