"""FastAPI daemon and local Web UI for dumb-speaker."""

from __future__ import annotations

import asyncio
import contextlib
import mimetypes
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from pomodoro import PomodoroTimer
from speaker_manager import SpeakerManager, get_lan_ip_for_target
from telegram_bot import TelegramBotService
from tts import list_piper_voice_details, list_piper_voices, synthesize_wav

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


def _build_media_url(
    target_ip: str,
    filename: str,
    request: Optional[Request] = None,
) -> str:
    public_url = os.environ.get("DUMB_SPEAKER_PUBLIC_URL", "").strip().rstrip("/")
    if public_url:
        return f"{public_url}/audio/{filename}"
    lan_ip = get_lan_ip_for_target(target_ip)
    port = (request.url.port if request is not None else None) or DEFAULT_PORT
    return f"http://{lan_ip}:{port}/audio/{filename}"


def _speak_sync(
    text: str,
    device: Optional[str] = None,
    engine: str = "auto",
    voice: Optional[str] = None,
    request: Optional[Request] = None,
) -> dict:
    _cleanup_old_media()
    cast = manager.resolve(device)
    filename = f"tts_{uuid.uuid4().hex[:12]}.wav"
    wav_path = MEDIA_DIR / filename
    engine_used = synthesize_wav(text, wav_path, engine=engine, voice=voice)
    url = _build_media_url(cast.cast_info.host, filename, request=request)
    device_info = manager.play_media(device, url, content_type="audio/wav")
    return {"ok": True, "engine": engine_used, "device": device_info, "url": url}


async def _speak_async(
    text: str,
    device: Optional[str] = None,
    voice: Optional[str] = None,
) -> dict:
    return await asyncio.to_thread(_speak_sync, text, device, "auto", voice, None)


async def _announce_pomodoro(
    text: str,
    device: Optional[str] = None,
    voice: Optional[str] = None,
) -> None:
    await _speak_async(text, device=device, voice=voice)


pomodoro = PomodoroTimer(_announce_pomodoro)
telegram_bot = TelegramBotService(manager=manager, pomodoro=pomodoro, speak_fn=_speak_async)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        await asyncio.to_thread(manager.refresh, 4.0)
    except Exception as exc:
        print(f"Initial speaker discovery failed: {exc}")
    await telegram_bot.start()
    yield
    await telegram_bot.stop()
    await pomodoro.stop()
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


class PomodoroStartRequest(BaseModel):
    work_minutes: int = Field(25, ge=1, le=180)
    break_minutes: int = Field(5, ge=1, le=60)
    cycles: int = Field(4, ge=1, le=12)
    device: Optional[str] = None
    voice: Optional[str] = None
    lang: str = "uk"


def _handle_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (ValueError, FileNotFoundError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "devices": len(manager.list_devices()),
        "telegram_enabled": telegram_bot.enabled,
        "pomodoro_state": pomodoro.status()["state"],
    }


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
    engines = ["auto", "piper", "sapi"] if sys.platform == "win32" else ["auto", "piper"]
    return {
        "piper": list_piper_voices(),
        "voices": list_piper_voice_details(),
        "engines": engines,
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
        return _speak_sync(
            req.text,
            device=req.device,
            engine=req.engine,
            voice=req.voice,
            request=request,
        )
    except Exception as exc:
        raise _handle_error(exc) from exc


@app.get("/api/pomodoro")
def get_pomodoro() -> dict:
    return pomodoro.status()


@app.post("/api/pomodoro/start")
async def start_pomodoro(req: PomodoroStartRequest) -> dict:
    try:
        return await pomodoro.start(
            work_minutes=req.work_minutes,
            break_minutes=req.break_minutes,
            cycles=req.cycles,
            device=req.device,
            voice=req.voice,
            lang=req.lang,
        )
    except Exception as exc:
        raise _handle_error(exc) from exc


@app.post("/api/pomodoro/pause")
async def pause_pomodoro() -> dict:
    return await pomodoro.toggle_pause()


@app.post("/api/pomodoro/stop")
async def stop_pomodoro() -> dict:
    return await pomodoro.stop()


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
        url = _build_media_url(cast.cast_info.host, filename, request=request)
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
