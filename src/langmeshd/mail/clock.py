"""Detect a host that slept or was frozen long enough that TCP and IMAP IDLE are lies."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


class StaleConnection(RuntimeError):
    """A socket that was live before suspend/pause cannot be trusted."""


def _boot_ahead() -> float:
    """How far CLOCK_BOOTTIME is ahead of CLOCK_MONOTONIC: this gap grows across suspend."""
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME) - time.clock_gettime(time.CLOCK_MONOTONIC)
    except (AttributeError, OSError):
        return 0.0


class ResumeClock:
    """Watch for a VPS suspend or a clock jump that means every open socket should be dropped."""

    def __init__(self) -> None:
        self._boot_ahead = _boot_ahead()
        self._wall = time.time()
        self._mono = time.monotonic()

    def jumped(self, *, grace_seconds: float = 2.0) -> bool:
        """True when wall time advanced more than monotonic time, or boot-time gap grew (suspend)."""
        boot = _boot_ahead()
        wall = time.time()
        mono = time.monotonic()
        suspend = boot - self._boot_ahead > grace_seconds
        skew = (wall - self._wall) - (mono - self._mono) > grace_seconds
        self._boot_ahead = boot
        self._wall = wall
        self._mono = mono
        return suspend or skew

    def note(self) -> None:
        """Set the baseline after a fresh IMAP connect. Do not call this to 'clear' a suspend you have not handled."""
        self._boot_ahead = _boot_ahead()
        self._wall = time.time()
        self._mono = time.monotonic()

    async def await_fresh(self, awaitable: Awaitable[T], *, interval: float = 2.0) -> T:
        """Await `awaitable`, but drop it if the host slept in the meantime.

        asyncio timeouts use CLOCK_MONOTONIC, which often does not include the freeze.
        Checking CLOCK_BOOTTIME on this interval is what notices a paused container.
        """
        task = asyncio.ensure_future(awaitable)
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=interval)
                if task in done:
                    return task.result()
                if self.jumped():
                    raise StaleConnection("the host slept or the clock jumped")
        except (StaleConnection, asyncio.CancelledError):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            raise
