"""Lightweight Telegram bot (long-polling) to control dumb-speaker from anywhere."""

from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable, Optional

import requests

from pomodoro import PomodoroTimer
from speaker_manager import SpeakerManager

SpeakFn = Callable[[str, Optional[str], Optional[str]], Awaitable[dict[str, Any]]]


def _parse_allowed_users() -> set[int]:
    raw = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


class TelegramBotService:
    """Runs Telegram Bot API getUpdates long-polling in a background task."""

    def __init__(
        self,
        manager: SpeakerManager,
        pomodoro: PomodoroTimer,
        speak_fn: SpeakFn,
    ) -> None:
        self._token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self._allowed_users = _parse_allowed_users()
        self._manager = manager
        self._pomodoro = pomodoro
        self._speak = speak_fn
        self._active_device: Optional[str] = (
            os.environ.get("TELEGRAM_DEFAULT_DEVICE")
            or os.environ.get("DUMB_SPEAKER")
            or None
        )
        self._task: Optional[asyncio.Task[None]] = None
        self._offset: int = 0
        self._stop_event = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return bool(self._token)

    def _api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self._token}/{method}"

    def _send_message_sync(self, chat_id: int, text: str) -> None:
        try:
            requests.post(
                self._api_url("sendMessage"),
                json={"chat_id": chat_id, "text": text},
                timeout=10,
            )
        except Exception as exc:
            print(f"[telegram] sendMessage failed: {exc}")

    def _get_updates_sync(self) -> list[dict[str, Any]]:
        resp = requests.get(
            self._api_url("getUpdates"),
            params={"offset": self._offset, "timeout": 25},
            timeout=35,
        )
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")
        return payload.get("result", [])

    async def start(self) -> None:
        if not self.enabled:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._poll_loop())
        print("[telegram] Bot long-polling started.")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    def _resolve_target_device(self) -> Optional[str]:
        if self._active_device:
            return self._active_device
        devices = self._manager.list_devices()
        if devices:
            return devices[0]["name"]
        return None

    async def _reply(self, chat_id: int, text: str) -> None:
        await asyncio.to_thread(self._send_message_sync, chat_id, text)

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        sender = msg.get("from") or {}
        user_id = sender.get("id")
        text = (msg.get("text") or "").strip()
        if not chat_id or not text:
            return

        if text.startswith("/myid"):
            await self._reply(chat_id, f"Your Telegram user ID: {user_id}")
            return

        if self._allowed_users and user_id not in self._allowed_users:
            await self._reply(chat_id, "Unauthorized. Add your user ID to TELEGRAM_ALLOWED_USERS.")
            return

        if text.startswith(("/start", "/help")):
            await self._reply(
                chat_id,
                "Dumb Speaker commands:\n"
                "• Send any text to speak it (auto EN/UK)\n"
                "• /say <text> — speak text\n"
                "• /speakers — list speakers\n"
                "• /use <name|ip> — select speaker\n"
                "• /vol <0-100> — set volume\n"
                "• /stop — stop playback\n"
                "• /pomo [work] [break] [cycles] [uk|en] — start Pomodoro\n"
                "• /pomopause — pause/resume Pomodoro\n"
                "• /pomostop — stop Pomodoro\n"
                "• /status — show speaker & Pomodoro status\n"
                "• /myid — show your Telegram user ID",
            )
            return

        try:
            if text.startswith("/speakers"):
                devices = await asyncio.to_thread(self._manager.list_devices)
                if not devices:
                    await self._reply(chat_id, "No speakers found.")
                    return
                active = self._resolve_target_device()
                lines = []
                for d in devices:
                    mark = " (active)" if active and active.lower() in (d["name"].lower(), d["ip"]) else ""
                    lines.append(f"• {d['name']} ({d['model']}) — {d['ip']} — vol {d['volume']}%{mark}")
                await self._reply(chat_id, "\n".join(lines))
                return

            if text.startswith("/use"):
                target = text[4:].strip()
                if not target:
                    await self._reply(chat_id, "Usage: /use <speaker name or IP>")
                    return
                cast = await asyncio.to_thread(self._manager.resolve, target)
                self._active_device = cast.cast_info.friendly_name.strip()
                await self._reply(chat_id, f"Active speaker set to: {self._active_device}")
                return

            if text.startswith("/vol"):
                arg = text[4:].strip()
                if not arg.isdigit():
                    await self._reply(chat_id, "Usage: /vol <0-100>")
                    return
                level = int(arg)
                dev = await asyncio.to_thread(
                    self._manager.set_volume, self._resolve_target_device(), level
                )
                await self._reply(chat_id, f"{dev['name']} volume set to {level}%.")
                return

            if text.startswith("/stop"):
                dev = await asyncio.to_thread(
                    self._manager.stop, self._resolve_target_device()
                )
                await self._reply(chat_id, f"Stopped playback on {dev['name']}.")
                return

            if text.startswith("/pomostop"):
                await self._pomodoro.stop()
                await self._reply(chat_id, "Pomodoro stopped.")
                return

            if text.startswith("/pomopause"):
                st = await self._pomodoro.toggle_pause()
                await self._reply(chat_id, f"Pomodoro state: {st['state']}.")
                return

            if text.startswith("/pomo"):
                parts = text.split()[1:]
                work = int(parts[0]) if len(parts) >= 1 and parts[0].isdigit() else 25
                brk = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 5
                cycles = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 4
                lang = "uk"
                for p in parts:
                    if p.lower() in ("en", "uk"):
                        lang = p.lower()
                st = await self._pomodoro.start(
                    work_minutes=work,
                    break_minutes=brk,
                    cycles=cycles,
                    device=self._resolve_target_device(),
                    lang=lang,
                )
                await self._reply(
                    chat_id,
                    f"Pomodoro started ({work}m work / {brk}m break × {cycles}, {lang.upper()}) "
                    f"on {st['device'] or 'default speaker'}.",
                )
                return

            if text.startswith("/status"):
                st = self._pomodoro.status()
                mins, secs = divmod(st["remaining_seconds"], 60)
                active = self._resolve_target_device() or "None"
                pomo_line = (
                    f"Pomodoro: {st['state']} (cycle {st['cycle']}/{st['total_cycles']}, {mins:02d}:{secs:02d} left)"
                    if st["state"] != "idle"
                    else "Pomodoro: idle"
                )
                await self._reply(chat_id, f"Active speaker: {active}\n{pomo_line}")
                return

            spoken = text[4:].strip() if text.startswith("/say") else text
            if not spoken:
                await self._reply(chat_id, "Usage: /say <text>")
                return
            result = await self._speak(spoken, self._resolve_target_device(), None)
            await self._reply(
                chat_id,
                f"Spoke on {result['device']['name']} ({result['engine']}).",
            )
        except Exception as exc:
            await self._reply(chat_id, f"Error: {exc}")

    async def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                updates = await asyncio.to_thread(self._get_updates_sync)
                for update in updates:
                    self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                    msg = update.get("message")
                    if msg:
                        await self._handle_message(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[telegram] Polling error: {exc}")
                await asyncio.sleep(5.0)
