"""The synchronous event channel shared by runtime features."""

from __future__ import annotations

from typing import Any, Callable


class PluginBus:
    """Deliver feature events immediately, in subscription order, without owning background tasks."""

    def __init__(self) -> None:
        self._subscribers: dict[type, list[Callable[[Any], None]]] = {}

    def subscribe(self, event_type: type, handler: Callable[[Any], None]) -> None:
        """Hear every event of ``event_type``, whatever feature produced it."""
        self._subscribers.setdefault(event_type, []).append(handler)

    def emit(self, event: Any) -> None:
        """Hand one event to every subscriber of its type."""
        for handler in self._subscribers.get(type(event), ()):
            handler(event)


__all__ = ["PluginBus"]
