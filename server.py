"""FastAPI daemon and local Web UI for dumb-speaker."""

from __future__ import annotations

import asyncio
import contextlib
import mimetypes
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from speaker_manager import SpeakerManager, get_lan_ip_for_target
from tts import list_piper_voices, synthesize_wav

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
MEDIA_DIR = BASE_DIR / "media"
DEFAULT_PORT = int(os.environ.get("DUMB_SPEAKER_PORT", "8765"))

manager = SpeakerManager()


def _cleanup_old_media(max_age_seconds: int = 3600) -> None:
    if not MEDIA_DIR.is_dir():
        return
    cutoff = time.time() - max_age_seconds
    for item in MEDIA_DIR.iterdir():
        if item.is_file() and item.stat().st_mtime < cutoff:
            try:
                item.unlink()
            except OSError:
                pass


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        await asyncio.to_thread(manager.refresh, 4.0)
    except Exception as exc:
        print(f"Initial speaker discovery failed: {exc}")
    yield
    await asyncio.to_thread(manager.close)


app = FastAPI(title="Dumb Speaker", lifespan=lifespan)


class SayRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    device: Optional[str] = None
    engine: str = "auto"
    voice: Optional[str] = None


class PlayRequest(BaseModel):
    url: str = Field(..., min_length=1)
    device: Optional[str] = None
    content_type: Optional[str] = None


class VolumeRequest(BaseModel):
    level: int = Field(..., ge=0, le=100)
    device: Optional[str] = None


class MuteRequest(BaseModel):
    muted: bool
    device: Optional[str] = None


class DeviceRequest(BaseModel):
    device: Optional[str] = None


def _handle_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (ValueError, FileNotFoundError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def _build_media_url(request: Request, target_ip: str, filename: str) -> str:
    lan_ip = get_lan_ip_for_target(target_ip)
    port = request.url.port or DEFAULT_PORT
    return f"http://{lan_ip}:{port}/audio/{filename}"


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/devices")
def get_devices() -> dict:
    return {"devices": manager.list_devices()}


@app.post("/api/discover")
def discover_devices() -> dict:
    try:
        return {"devices": manager.refresh(timeout=4.0)}
    except Exception as exc:
        raise _handle_error(exc) from exc


@app.get("/api/voices")
def get_voices() -> dict:
    return {
        "piper": list_piper_voices(),
        "engines": ["auto", "piper", "sapi"],
    }


@app.get("/audio/{filename}")
def serve_audio(filename: str) -> FileResponse:
    safe_name = Path(filename).name
    file_path = MEDIA_DIR / safe_name
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Audio file not found.")
    mime, _ = mimetypes.guess_type(str(file_path))
    return FileResponse(file_path, media_type=mime or "application/octet-stream")


@app.post("/api/say")
def say(req: SayRequest, request: Request) -> dict:
    try:
        _cleanup_old_media()
        cast = manager.resolve(req.device)
        filename = f"tts_{uuid.uuid4().hex[:12]}.wav"
        wav_path = MEDIA_DIR / filename
        engine_used = synthesize_wav(
            req.text, wav_path, engine=req.engine, voice=req.voice
        )
        url = _build_media_url(request, cast.cast_info.host, filename)
        device_info = manager.play_media(req.device, url, content_type="audio/wav")
        return {"ok": True, "engine": engine_used, "device": device_info, "url": url}
    except Exception as exc:
        raise _handle_error(exc) from exc


@app.post("/api/play")
def play(req: PlayRequest) -> dict:
    try:
        mime = req.content_type
        if not mime:
            mime, _ = mimetypes.guess_type(req.url)
        device_info = manager.play_media(
            req.device, req.url, content_type=mime or "audio/mpeg"
        )
        return {"ok": True, "device": device_info}
    except Exception as exc:
        raise _handle_error(exc) from exc


@app.post("/api/upload")
async def upload_and_play(
    request: Request,
    file: UploadFile = File(...),
    device: Optional[str] = Form(None),
) -> dict:
    try:
        _cleanup_old_media()
        cast = manager.resolve(device)
        ext = Path(file.filename or "audio.mp3").suffix or ".mp3"
        filename = f"upload_{uuid.uuid4().hex[:12]}{ext}"
        dest = MEDIA_DIR / filename
        content = await file.read()
        if not content:
            raise ValueError("Uploaded file is empty.")
        dest.write_bytes(content)

        mime = file.content_type
        if not mime or mime == "application/octet-stream":
            mime, _ = mimetypes.guess_type(str(dest))
        url = _build_media_url(request, cast.cast_info.host, filename)
        device_info = manager.play_media(device, url, content_type=mime or "audio/mpeg")
        return {"ok": True, "device": device_info, "url": url}
    except Exception as exc:
        raise _handle_error(exc) from exc


@app.post("/api/volume")
def set_volume(req: VolumeRequest) -> dict:
    try:
        return {"ok": True, "device": manager.set_volume(req.device, req.level)}
    except Exception as exc:
        raise _handle_error(exc) from exc


@app.post("/api/mute")
def set_mute(req: MuteRequest) -> dict:
    try:
        return {"ok": True, "device": manager.set_muted(req.device, req.muted)}
    except Exception as exc:
        raise _handle_error(exc) from exc


@app.post("/api/stop")
def stop(req: DeviceRequest) -> dict:
    try:
        return {"ok": True, "device": manager.stop(req.device)}
    except Exception as exc:
        raise _handle_error(exc) from exc
