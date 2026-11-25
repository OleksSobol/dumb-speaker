"""Pomodoro timer service with speaker TTS announcements."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

AnnounceFn = Callable[[str, Optional[str], Optional[str]], Awaitable[None]]

PHRASES = {
    "en": {
        "work_start": "Pomodoro started. {work} minutes of focus. Cycle {cycle} of {total}.",
        "break_start": "Focus complete. Take a {brk} minute break.",
        "next_work": "Break is over. Back to focus for {work} minutes. Cycle {cycle} of {total}.",
        "done": "All {total} Pomodoro cycles are complete. Great job!",
    },
    "uk": {
        "work_start": "Помодоро запущено. {work} хвилин фокусу. Цикл {cycle} з {total}.",
        "break_start": "Фокус завершено. Зробіть перерву на {brk} хвилин.",
        "next_work": "Перерва закінчилася. Повертаємось до роботи на {work} хвилин. Цикл {cycle} з {total}.",
        "done": "Усі {total} цикли помодоро завершено. Чудова робота!",
    },
}


@dataclass
class PomodoroConfig:
    work_minutes: int = 25
    break_minutes: int = 5
    cycles: int = 4
    device: Optional[str] = None
    voice: Optional[str] = None
    lang: str = "uk"


class PomodoroTimer:
    """Asyncio-backed Pomodoro timer that announces phase changes on a speaker."""

    def __init__(self, announce_fn: AnnounceFn) -> None:
        self._announce = announce_fn
        self._config = PomodoroConfig()
        self._state: str = "idle"  # idle | work | break | paused
        self._paused_from: Optional[str] = None
        self._cycle: int = 0
        self._remaining: float = 0.0
        self._deadline: Optional[float] = None
        self._task: Optional[asyncio.Task[None]] = None

    def status(self) -> dict:
        remaining = self._remaining
        if self._state in ("work", "break") and self._deadline is not None:
            remaining = max(0.0, self._deadline - time.monotonic())
        return {
            "state": self._state,
            "paused_from": self._paused_from,
            "cycle": self._cycle,
            "total_cycles": self._config.cycles,
            "remaining_seconds": int(round(remaining)),
            "work_minutes": self._config.work_minutes,
            "break_minutes": self._config.break_minutes,
            "device": self._config.device,
            "voice": self._config.voice,
            "lang": self._config.lang,
        }

    def _msg(self, key: str) -> str:
        lang = self._config.lang if self._config.lang in PHRASES else "en"
        return PHRASES[lang][key].format(
            work=self._config.work_minutes,
            brk=self._config.break_minutes,
            cycle=self._cycle,
            total=self._config.cycles,
        )

    async def _safe_announce(self, text: str) -> None:
        try:
            await self._announce(text, self._config.device, self._config.voice)
        except Exception as exc:
            print(f"[pomodoro] Announcement failed: {exc}")

    async def start(
        self,
        work_minutes: int = 25,
        break_minutes: int = 5,
        cycles: int = 4,
        device: Optional[str] = None,
        voice: Optional[str] = None,
        lang: str = "uk",
    ) -> dict:
        if work_minutes < 1 or work_minutes > 180:
            raise ValueError("work_minutes must be between 1 and 180.")
        if break_minutes < 1 or break_minutes > 60:
            raise ValueError("break_minutes must be between 1 and 60.")
        if cycles < 1 or cycles > 12:
            raise ValueError("cycles must be between 1 and 12.")

        await self.stop()

        self._config = PomodoroConfig(
            work_minutes=work_minutes,
            break_minutes=break_minutes,
            cycles=cycles,
            device=device,
            voice=voice,
            lang="uk" if lang.lower().startswith("uk") else "en",
        )
        self._cycle = 1
        self._state = "work"
        self._paused_from = None
        self._remaining = float(work_minutes * 60)
        self._deadline = time.monotonic() + self._remaining

        self._task = asyncio.create_task(self._run_loop())
        asyncio.create_task(self._safe_announce(self._msg("work_start")))
        return self.status()

    async def toggle_pause(self) -> dict:
        if self._state in ("work", "break"):
            if self._deadline is not None:
                self._remaining = max(0.0, self._deadline - time.monotonic())
            self._paused_from = self._state
            self._state = "paused"
            self._deadline = None
        elif self._state == "paused" and self._paused_from:
            self._state = self._paused_from
            self._paused_from = None
            self._deadline = time.monotonic() + self._remaining
        return self.status()

    async def stop(self) -> dict:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._state = "idle"
        self._paused_from = None
        self._cycle = 0
        self._remaining = 0.0
        self._deadline = None
        return self.status()

    async def _run_loop(self) -> None:
        try:
            while self._state != "idle":
                if self._state == "paused":
                    await asyncio.sleep(0.5)
                    continue

                if self._deadline is None:
                    break
                remaining = self._deadline - time.monotonic()
                if remaining > 0:
                    await asyncio.sleep(min(remaining, 1.0))
                    continue

                if self._state == "work":
                    if self._cycle >= self._config.cycles:
                        await self._safe_announce(self._msg("done"))
                        self._state = "idle"
                        self._deadline = None
                        self._remaining = 0.0
                        break
                    self._state = "break"
                    self._remaining = float(self._config.break_minutes * 60)
                    self._deadline = time.monotonic() + self._remaining
                    await self._safe_announce(self._msg("break_start"))
                elif self._state == "break":
                    self._cycle += 1
                    self._state = "work"
                    self._remaining = float(self._config.work_minutes * 60)
                    self._deadline = time.monotonic() + self._remaining
                    await self._safe_announce(self._msg("next_work"))
        except asyncio.CancelledError:
            raise
