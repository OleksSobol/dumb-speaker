# Dumb Speaker

Turn your Google Home / Nest speaker into a fast, local-only LAN speaker.
Flip the physical mic switch off on the speaker, and control it via a **local Web UI**, **HTTP API**, or **CLI** with natural offline neural TTS (**Piper**).

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Download the default offline Piper neural voice (~60 MB)
python -m piper.download_voices --download-dir voices en_US-lessac-medium
```

## Web UI & HTTP API (Recommended)

Start the persistent server (keeps speaker connections warm so commands are instant):

```powershell
python speaker.py serve
```

Then open **http://localhost:8765** in your browser (or `http://<your-pc-ip>:8765` from your phone on the same Wi-Fi).

### HTTP API Endpoints

- `GET /api/devices` — list cached speakers
- `POST /api/discover` — refresh mDNS discovery
- `POST /api/say` — `{"text": "Dinner is ready", "device": "Speakeroo", "engine": "auto"}`
- `POST /api/play` — `{"url": "https://example.com/stream.mp3", "device": "Speakeroo"}`
- `POST /api/upload` — multipart form upload (`file`, optional `device`)
- `POST /api/volume` — `{"level": 35, "device": "Speakeroo"}`
- `POST /api/mute` — `{"muted": true, "device": "Speakeroo"}`
- `POST /api/stop` — `{"device": "Speakeroo"}`

## CLI Usage

```powershell
# Find Google speakers on your network
python speaker.py discover

# Check status of a speaker (partial name or IP works)
python speaker.py status --device "Speakeroo"
python speaker.py status --device 192.168.50.108

# Speak text (Piper neural TTS by default, Windows SAPI fallback)
python speaker.py say "Dinner is ready" --device "Cave speaker"
python speaker.py say "System check" --device "Speakeroo" --engine sapi

# Play a local audio file or a stream URL
python speaker.py play path\to\sound.mp3 --device "Speakeroo"
python speaker.py play https://example.com/stream.mp3 --device "Speakeroo"

# Volume & playback controls
python speaker.py volume 40 --device "Speakeroo"
python speaker.py stop --device "Speakeroo"
```
