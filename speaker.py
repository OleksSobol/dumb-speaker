"""Simple local controller to use Google Home/Nest speakers as dumb LAN speakers."""

from __future__ import annotations

import argparse
import contextlib
import functools
import http.server
import mimetypes
import os
import socket
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Optional

import pychromecast

from tts import synthesize_wav


def get_lan_ip_for_target(target_ip: str) -> str:
    """Return the local interface IP used to reach target_ip."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((target_ip, 8009))
        return sock.getsockname()[0]


@contextlib.contextmanager
def open_speakers(timeout: float = 4.0) -> Iterator[list[pychromecast.Chromecast]]:
    """Discover Google Cast devices and keep zeroconf alive while connecting."""
    casts, browser = pychromecast.get_chromecasts(timeout=timeout)
    try:
        yield casts
    finally:
        browser.stop_discovery()


@contextlib.contextmanager
def connect_speaker(
    device: Optional[str], timeout: float = 4.0
) -> Iterator[pychromecast.Chromecast]:
    """Find and connect to a speaker by name, partial name, or IP address."""
    target = device or os.environ.get("DUMB_SPEAKER")
    with open_speakers(timeout=timeout) as casts:
        if not casts:
            raise RuntimeError("No Google Cast speakers found on the local network.")

        selected: Optional[pychromecast.Chromecast] = None
        if not target:
            if len(casts) == 1:
                selected = casts[0]
            else:
                names = ", ".join(
                    f"{c.cast_info.friendly_name!r} ({c.cast_info.host})" for c in casts
                )
                raise RuntimeError(
                    f"Multiple speakers found ({names}). Pass --device <name-or-ip> or set DUMB_SPEAKER."
                )
        else:
            needle = target.strip().lower()
            for c in casts:
                if (
                    c.cast_info.host == target.strip()
                    or c.cast_info.friendly_name.strip().lower() == needle
                ):
                    selected = c
                    break
            if selected is None:
                for c in casts:
                    if needle in c.cast_info.friendly_name.lower():
                        selected = c
                        break

        if selected is None:
            names = ", ".join(
                f"{c.cast_info.friendly_name!r} ({c.cast_info.host})" for c in casts
            )
            raise RuntimeError(f"Speaker {target!r} not found. Available: {names}")

        selected.wait(timeout=5.0)
        yield selected


class QuietFileHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP handler serving a directory without noisy console logs."""

    def log_message(self, format: str, *args: object) -> None:
        return


def serve_file_and_play(
    cast: pychromecast.Chromecast,
    file_path: Path,
    content_type: str,
    wait: bool = True,
) -> None:
    """Serve a local audio file over LAN HTTP and play it on the Cast speaker."""
    if not file_path.is_file():
        raise FileNotFoundError(f"Audio file not found: {file_path}")

    lan_ip = get_lan_ip_for_target(cast.cast_info.host)
    handler = functools.partial(QuietFileHandler, directory=str(file_path.parent))
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), handler)
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    url = f"http://{lan_ip}:{port}/{file_path.name}"
    try:
        mc = cast.media_controller
        mc.play_media(url, content_type)
        mc.block_until_active(timeout=10.0)

        if wait:
            # Wait briefly for playback to start, then poll until it finishes or times out.
            saw_playing = False
            deadline = time.time() + 120.0
            while time.time() < deadline:
                mc.update_status()
                state = mc.status.player_state
                if state in ("PLAYING", "BUFFERING"):
                    saw_playing = True
                elif saw_playing and state in ("IDLE", "UNKNOWN", None):
                    break
                time.sleep(0.4)
    finally:
        server.shutdown()
        server.server_close()


def cmd_discover(args: argparse.Namespace) -> int:
    with open_speakers(timeout=args.timeout) as casts:
        if not casts:
            print("No Google Cast speakers found.")
            return 1
        for c in casts:
            c.wait(timeout=3.0)
            vol = round(c.status.volume_level * 100) if c.status else "?"
            muted = c.status.volume_muted if c.status else "?"
            print(
                f"- {c.cast_info.friendly_name.strip()} | "
                f"model={c.cast_info.model_name} | "
                f"ip={c.cast_info.host} | "
                f"vol={vol}% | muted={muted}"
            )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    with connect_speaker(args.device, timeout=args.timeout) as cast:
        status = cast.status
        mc = cast.media_controller
        print(f"Name:   {cast.cast_info.friendly_name.strip()}")
        print(f"Model:  {cast.cast_info.model_name}")
        print(f"IP:     {cast.cast_info.host}:{cast.cast_info.port}")
        if status:
            print(f"Volume: {round(status.volume_level * 100)}% (muted={status.volume_muted})")
            print(f"App:    {status.display_name or 'Idle'}")
        if mc and mc.status:
            print(f"Player: {mc.status.player_state}")
    return 0


def cmd_volume(args: argparse.Namespace) -> int:
    if not 0 <= args.level <= 100:
        raise ValueError("Volume level must be between 0 and 100.")
    with connect_speaker(args.device, timeout=args.timeout) as cast:
        cast.set_volume(args.level / 100.0)
        print(f"Set {cast.cast_info.friendly_name.strip()} volume to {args.level}%.")
    return 0


def cmd_mute(args: argparse.Namespace, muted: bool) -> int:
    with connect_speaker(args.device, timeout=args.timeout) as cast:
        cast.set_volume_muted(muted)
        state = "muted" if muted else "unmuted"
        print(f"{cast.cast_info.friendly_name.strip()} is now {state}.")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    with connect_speaker(args.device, timeout=args.timeout) as cast:
        cast.media_controller.stop()
        cast.quit_app()
        print(f"Stopped playback on {cast.cast_info.friendly_name.strip()}.")
    return 0


def cmd_play(args: argparse.Namespace) -> int:
    with connect_speaker(args.device, timeout=args.timeout) as cast:
        target = args.source
        if target.startswith(("http://", "https://")):
            mime, _ = mimetypes.guess_type(target)
            mc = cast.media_controller
            mc.play_media(target, mime or "audio/mpeg")
            mc.block_until_active(timeout=10.0)
            print(f"Playing stream on {cast.cast_info.friendly_name.strip()}: {target}")
            return 0

        file_path = Path(target).resolve()
        mime, _ = mimetypes.guess_type(str(file_path))
        print(f"Serving and playing {file_path.name} on {cast.cast_info.friendly_name.strip()}...")
        serve_file_and_play(cast, file_path, mime or "audio/mpeg", wait=not args.no_wait)
    return 0


def cmd_say(args: argparse.Namespace) -> int:
    with connect_speaker(args.device, timeout=args.timeout) as cast:
        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = Path(tmpdir) / "tts.wav"
            used = synthesize_wav(args.text, wav_path, engine=args.engine, voice=args.voice)
            print(f"Speaking ({used}) on {cast.cast_info.friendly_name.strip()}: {args.text!r}")
            serve_file_and_play(cast, wav_path, "audio/wav", wait=True)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    os.environ["DUMB_SPEAKER_PORT"] = str(args.port)
    print(f"Web UI (this PC): http://localhost:{args.port}")
    try:
        lan_ip = get_lan_ip_for_target("1.1.1.1")
        print(f"Web UI (LAN):     http://{lan_ip}:{args.port}")
    except OSError:
        pass
    uvicorn.run("server:app", host=args.host, port=args.port, reload=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control Google speakers as dumb LAN speakers.")
    parser.add_argument("--timeout", type=float, default=4.0, help="Discovery timeout in seconds.")

    sub = parser.add_subparsers(dest="command", required=True)

    p_disc = sub.add_parser("discover", help="List Google Cast speakers on the LAN.")
    p_disc.set_defaults(func=cmd_discover)

    for name, fn, help_text in [
        ("status", cmd_status, "Show speaker status."),
        ("mute", lambda a: cmd_mute(a, True), "Mute speaker."),
        ("unmute", lambda a: cmd_mute(a, False), "Unmute speaker."),
        ("stop", cmd_stop, "Stop playback and exit Cast app."),
    ]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--device", "-d", help="Speaker name (or partial) or IP.")
        p.set_defaults(func=fn)

    p_vol = sub.add_parser("volume", help="Set speaker volume (0-100).")
    p_vol.add_argument("level", type=int, help="Volume percentage (0-100).")
    p_vol.add_argument("--device", "-d", help="Speaker name (or partial) or IP.")
    p_vol.set_defaults(func=cmd_volume)

    p_play = sub.add_parser("play", help="Play a local audio file or HTTP stream URL.")
    p_play.add_argument("source", help="Path to local audio file or http(s) URL.")
    p_play.add_argument("--device", "-d", help="Speaker name (or partial) or IP.")
    p_play.add_argument("--no-wait", action="store_true", help="Exit immediately (streams only; local files stop serving on exit).")
    p_play.set_defaults(func=cmd_play)

    p_say = sub.add_parser("say", help="Speak text using offline TTS (Piper or Windows SAPI).")
    p_say.add_argument("text", help="Text to speak.")
    p_say.add_argument("--device", "-d", help="Speaker name (or partial) or IP.")
    p_say.add_argument("--engine", default="auto", choices=["auto", "piper", "sapi"], help="TTS engine.")
    p_say.add_argument("--voice", help="Piper voice model name in voices/ (without .onnx).")
    p_say.set_defaults(func=cmd_say)

    p_serve = sub.add_parser("serve", help="Start the persistent Web UI and HTTP API server.")
    p_serve.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0).")
    p_serve.add_argument("--port", type=int, default=8765, help="Port (default: 8765).")
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
