"""The shared bus and the core's own turn events.

A feature publishes typed events on the bus; features that are interested subscribe and react
to what arrives. Nobody knows who publishes or who consumes, so features neither import nor
reference each other. The core also emits its own lifecycle events here, so a feature can react
to a turn without the core knowing it.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, Awaitable, Callable


class TurnStarted:
    """A user turn has begun and no work has happened yet."""


class TurnEnded:
    """A turn finished, with its final text and how it stopped."""

    def __init__(self, text: str, stop_reason: str) -> None:
        self.text = text
        self.stop_reason = stop_reason


class PluginBus:
    """The decoupled channel between features.

    A feature publishes events with `emit`; features that are interested subscribe with
    `subscribe` and act on what arrives. Nobody here knows who publishes or who consumes,
    so features neither import nor reference each other. Awaitable handlers are scheduled
    on the current loop when one is running and skipped otherwise.
    """

    def __init__(self) -> None:
        self._subscribers: dict[type, list[Callable[[Any], Awaitable[None] | None]]] = {}

    def subscribe(self, event_type: type, handler: Callable[[Any], Awaitable[None] | None]) -> None:
        """Hear every event of ``event_type``, whatever feature produced it."""
        self._subscribers.setdefault(event_type, []).append(handler)

    def emit(self, event: Any) -> None:
        """Hand one event to every subscriber of its type."""
        for handler in self._subscribers.get(type(event), ()):
            result = handler(event)
            if result is None:
                continue
            try:
                asyncio.get_running_loop().create_task(result)
            except RuntimeError:
                # No loop: an awaitable handler outside a turn has nothing to schedule it on.
                with suppress(BaseException):
                    asyncio.new_event_loop().run_until_complete(result)


__all__ = ["PluginBus", "TurnEnded", "TurnStarted"]