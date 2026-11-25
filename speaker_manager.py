"""Persistent Google Cast speaker manager for the dumb-speaker daemon."""

from __future__ import annotations

import os
import socket
import threading
from typing import Any, Optional

import pychromecast


def get_lan_ip_for_target(target_ip: str) -> str:
    """Return the local interface IP used to reach target_ip."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((target_ip, 8009))
        return sock.getsockname()[0]


class SpeakerManager:
    """Keeps persistent Chromecast connections so commands execute immediately."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._casts: list[pychromecast.Chromecast] = []
        self._browser: Any = None

    def refresh(self, timeout: float = 4.0) -> list[dict[str, Any]]:
        """Discover Cast devices on the LAN and cache their connections."""
        raw_hosts = os.environ.get("DUMB_SPEAKER_HOSTS", "")
        known_hosts = [h.strip() for h in raw_hosts.split(",") if h.strip()] or None
        with self._lock:
            self.close()
            casts, browser = pychromecast.get_chromecasts(
                timeout=timeout,
                known_hosts=known_hosts,
            )
            self._casts = casts
            self._browser = browser
            for cast in self._casts:
                try:
                    cast.wait(timeout=3.0)
                except Exception:
                    pass
            return self.list_devices()

    def close(self) -> None:
        """Stop discovery and disconnect cached speakers."""
        with self._lock:
            for cast in self._casts:
                try:
                    cast.disconnect(timeout=1.0)
                except Exception:
                    pass
            self._casts = []
            if self._browser is not None:
                try:
                    self._browser.stop_discovery()
                except Exception:
                    pass
                self._browser = None

    def _serialize(self, cast: pychromecast.Chromecast) -> dict[str, Any]:
        status = cast.status
        mc = cast.media_controller
        return {
            "id": str(cast.uuid),
            "name": cast.cast_info.friendly_name.strip(),
            "model": cast.cast_info.model_name,
            "ip": cast.cast_info.host,
            "port": cast.cast_info.port,
            "volume": round(status.volume_level * 100) if status else 0,
            "muted": bool(status.volume_muted) if status else False,
            "app": (status.display_name or "Idle") if status else "Unknown",
            "player_state": mc.status.player_state if (mc and mc.status) else "UNKNOWN",
        }

    def list_devices(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._serialize(c) for c in self._casts]

    def resolve(self, device: Optional[str] = None) -> pychromecast.Chromecast:
        with self._lock:
            if not self._casts:
                self.refresh(timeout=4.0)
            if not self._casts:
                raise RuntimeError("No Google Cast speakers found on the local network.")

            target = device or os.environ.get("DUMB_SPEAKER")
            if not target:
                if len(self._casts) == 1:
                    return self._casts[0]
                names = ", ".join(
                    f"{c.cast_info.friendly_name.strip()!r} ({c.cast_info.host})"
                    for c in self._casts
                )
                raise ValueError(
                    f"Multiple speakers found ({names}). Specify a device."
                )

            needle = target.strip().lower()
            for c in self._casts:
                if (
                    str(c.uuid).lower() == needle
                    or c.cast_info.host == target.strip()
                    or c.cast_info.friendly_name.strip().lower() == needle
                ):
                    return c
            for c in self._casts:
                if needle in c.cast_info.friendly_name.lower():
                    return c

            names = ", ".join(
                f"{c.cast_info.friendly_name.strip()!r} ({c.cast_info.host})"
                for c in self._casts
            )
            raise LookupError(f"Speaker {target!r} not found. Available: {names}")

    def set_volume(self, device: Optional[str], level: int) -> dict[str, Any]:
        if not 0 <= level <= 100:
            raise ValueError("Volume level must be between 0 and 100.")
        cast = self.resolve(device)
        cast.set_volume(level / 100.0)
        return self._serialize(cast)

    def set_muted(self, device: Optional[str], muted: bool) -> dict[str, Any]:
        cast = self.resolve(device)
        cast.set_volume_muted(muted)
        return self._serialize(cast)

    def stop(self, device: Optional[str]) -> dict[str, Any]:
        cast = self.resolve(device)
        cast.media_controller.stop()
        cast.quit_app()
        return self._serialize(cast)

    def play_media(
        self,
        device: Optional[str],
        url: str,
        content_type: str = "audio/mpeg",
    ) -> dict[str, Any]:
        cast = self.resolve(device)
        mc = cast.media_controller
        mc.play_media(url, content_type)
        mc.block_until_active(timeout=10.0)
        return self._serialize(cast)
