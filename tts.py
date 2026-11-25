"""Offline TTS engines: Piper neural TTS (primary) and Windows SAPI (fallback)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import wave
from pathlib import Path
from typing import Optional

VOICES_DIR = Path(__file__).resolve().parent / "voices"
DEFAULT_EN_VOICE = "en_US-lessac-medium"
DEFAULT_UK_VOICE = "uk_UA-ukrainian_tts-medium"

_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
_LANG_NAMES = {
    "uk": "Ukrainian",
    "en": "English",
}

_piper_cache: dict[Path, object] = {}
_piper_lock = threading.Lock()


def detect_language(text: str) -> str:
    """Detect 'uk' if text contains Cyrillic characters, otherwise 'en'."""
    return "uk" if _CYRILLIC_RE.search(text) else "en"


def list_piper_voices() -> list[str]:
    """Return names of installed Piper .onnx voice models in voices/."""
    if not VOICES_DIR.is_dir():
        return []
    return sorted(p.stem for p in VOICES_DIR.glob("*.onnx") if p.is_file())


def list_piper_voice_details() -> list[dict[str, str]]:
    """Return structured metadata for installed Piper voices."""
    details: list[dict[str, str]] = []
    for stem in list_piper_voices():
        parts = stem.split("-")
        locale = parts[0] if parts else ""
        lang = locale.split("_")[0].lower() if locale else "unknown"
        name = parts[1] if len(parts) > 1 else stem
        quality = parts[2] if len(parts) > 2 else ""
        lang_label = _LANG_NAMES.get(lang, locale or "Other")
        label = f"{lang_label} — {name}" + (f" ({quality})" if quality else "")
        details.append({"id": stem, "lang": lang, "label": label})
    return details


def _resolve_piper_model(
    voice: Optional[str] = None,
    text: str = "",
) -> Optional[Path]:
    if not VOICES_DIR.is_dir():
        return None

    if voice and voice.strip().lower() != "auto":
        candidate = VOICES_DIR / f"{voice.strip()}.onnx"
        if candidate.is_file():
            return candidate
        raise FileNotFoundError(f"Piper voice {voice!r} not found in {VOICES_DIR}")

    lang = detect_language(text) if text else "en"
    preferred = DEFAULT_UK_VOICE if lang == "uk" else DEFAULT_EN_VOICE
    preferred_path = VOICES_DIR / f"{preferred}.onnx"
    if preferred_path.is_file():
        return preferred_path

    models = sorted(VOICES_DIR.glob("*.onnx"))
    if not models:
        return None

    prefix = f"{lang}_"
    for model in models:
        if model.stem.lower().startswith(prefix):
            return model
    return models[0]


def synthesize_piper_wav(text: str, output_wav: Path, voice: Optional[str] = None) -> str:
    """Synthesize speech to output_wav using a cached PiperVoice model."""
    from piper import PhonemeType, PiperVoice

    model_path = _resolve_piper_model(voice=voice, text=text)
    if model_path is None:
        raise FileNotFoundError(
            f"No Piper .onnx models found in {VOICES_DIR}. "
            f"Run: python -m piper.download_voices --download-dir voices {DEFAULT_EN_VOICE}"
        )

    with _piper_lock:
        loaded = _piper_cache.get(model_path)
        if loaded is None:
            loaded = PiperVoice.load(model_path)
            _piper_cache[model_path] = loaded

    synth_text = text.lower() if loaded.config.phoneme_type == PhonemeType.TEXT else text

    with wave.open(str(output_wav), "wb") as wav_file:
        loaded.synthesize_wav(synth_text, wav_file)

    if not output_wav.exists() or output_wav.stat().st_size == 0:
        raise RuntimeError("Piper TTS produced an empty audio file.")
    return f"piper:{model_path.stem}"


def synthesize_sapi_wav(text: str, output_wav: Path) -> str:
    """Synthesize speech to output_wav using Windows built-in System.Speech."""
    if sys.platform != "win32":
        raise RuntimeError("Windows SAPI TTS is only available on Windows. Use Piper instead.")
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
        if _resolve_piper_model(voice=voice, text=text) is not None:
            return synthesize_piper_wav(text, output_wav, voice=voice)
        return synthesize_sapi_wav(text, output_wav)
    raise ValueError(f"Unknown TTS engine {engine!r}. Expected 'auto', 'piper', or 'sapi'.")
