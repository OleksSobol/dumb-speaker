"""Offline TTS engines: Piper neural TTS (primary) and Windows SAPI (fallback)."""

from __future__ import annotations

import os
import subprocess
import threading
import wave
from pathlib import Path
from typing import Optional

VOICES_DIR = Path(__file__).resolve().parent / "voices"
DEFAULT_PIPER_VOICE = "en_US-lessac-medium"

_piper_cache: dict[Path, object] = {}
_piper_lock = threading.Lock()


def list_piper_voices() -> list[str]:
    """Return names of installed Piper .onnx voice models in voices/."""
    if not VOICES_DIR.is_dir():
        return []
    return sorted(p.stem for p in VOICES_DIR.glob("*.onnx") if p.is_file())


def _resolve_piper_model(voice: Optional[str] = None) -> Optional[Path]:
    if not VOICES_DIR.is_dir():
        return None
    if voice:
        candidate = VOICES_DIR / f"{voice}.onnx"
        if candidate.is_file():
            return candidate
        raise FileNotFoundError(f"Piper voice {voice!r} not found in {VOICES_DIR}")
    default_path = VOICES_DIR / f"{DEFAULT_PIPER_VOICE}.onnx"
    if default_path.is_file():
        return default_path
    models = sorted(VOICES_DIR.glob("*.onnx"))
    return models[0] if models else None


def synthesize_piper_wav(text: str, output_wav: Path, voice: Optional[str] = None) -> str:
    """Synthesize speech to output_wav using a cached PiperVoice model."""
    from piper import PiperVoice

    model_path = _resolve_piper_model(voice)
    if model_path is None:
        raise FileNotFoundError(
            f"No Piper .onnx models found in {VOICES_DIR}. "
            f"Run: python -m piper.download_voices --download-dir voices {DEFAULT_PIPER_VOICE}"
        )

    with _piper_lock:
        loaded = _piper_cache.get(model_path)
        if loaded is None:
            loaded = PiperVoice.load(model_path)
            _piper_cache[model_path] = loaded

    with wave.open(str(output_wav), "wb") as wav_file:
        loaded.synthesize_wav(text, wav_file)

    if not output_wav.exists() or output_wav.stat().st_size == 0:
        raise RuntimeError("Piper TTS produced an empty audio file.")
    return f"piper:{model_path.stem}"


def synthesize_sapi_wav(text: str, output_wav: Path) -> str:
    """Synthesize speech to output_wav using Windows built-in System.Speech."""
    ps_script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.SetOutputToWaveFile($env:DUMB_TTS_OUT); "
        "$s.Speak($env:DUMB_TTS_TEXT); "
        "$s.Dispose()"
    )
    env = os.environ.copy()
    env["DUMB_TTS_OUT"] = str(output_wav)
    env["DUMB_TTS_TEXT"] = text
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Windows TTS failed: {result.stderr.strip() or result.stdout.strip()}")
    if not output_wav.exists() or output_wav.stat().st_size == 0:
        raise RuntimeError("Windows TTS produced an empty audio file.")
    return "sapi"


def synthesize_wav(
    text: str,
    output_wav: Path,
    engine: str = "auto",
    voice: Optional[str] = None,
) -> str:
    """Generate a WAV file using Piper (preferred) or Windows SAPI fallback."""
    if not text or not text.strip():
        raise ValueError("Text to speak cannot be empty.")

    chosen = engine.lower().strip()
    if chosen == "sapi":
        return synthesize_sapi_wav(text, output_wav)
    if chosen == "piper":
        return synthesize_piper_wav(text, output_wav, voice=voice)
    if chosen == "auto":
        if _resolve_piper_model(voice) is not None:
            return synthesize_piper_wav(text, output_wav, voice=voice)
        return synthesize_sapi_wav(text, output_wav)
    raise ValueError(f"Unknown TTS engine {engine!r}. Expected 'auto', 'piper', or 'sapi'.")
