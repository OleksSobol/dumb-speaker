# Dumb Speaker

Turn your Google Home / Nest speaker into a simple, local-only LAN speaker.
Flip the physical mic switch off on the speaker, and control it directly from your PC over your local network.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Usage

```powershell
# Find Google speakers on your network
python speaker.py discover

# Check status of a speaker (partial name or IP works)
python speaker.py status --device "Speakeroo"
python speaker.py status --device 192.168.50.108

# Speak text (offline Windows TTS -> local HTTP -> speaker)
python speaker.py say "Dinner is ready" --device "Cave speaker"

# Play a local audio file or a stream URL
python speaker.py play path\to\sound.mp3 --device "Speakeroo"
python speaker.py play https://example.com/stream.mp3 --device "Speakeroo"

# Volume & playback controls
python speaker.py volume 40 --device "Speakeroo"
python speaker.py stop --device "Speakeroo"
```

If `--device` is omitted and there is only one speaker (or you want to pick the first found), you can also set `DUMB_SPEAKER` in your environment:

```powershell
$env:DUMB_SPEAKER = "Speakeroo"
python speaker.py say "Hello world"
```
