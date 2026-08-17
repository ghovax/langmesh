"""The port a worker uses to reach its host.

The worker (library) never imports the daemon: the daemon implements this
interface and injects it into each session it hosts. A library-only embedding
gets the null implementation, whose calls are no-ops or raise.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional, Protocol, runtime_checkable


@runtime_checkable
class HostServices(Protocol):
    """Everything a session needs from the process hosting it."""

    # The live event bus: structural parts, model deltas, and turn framing.
    def publish_part(self, session_id: str, part: dict) -> None: ...
    def publish_delta(self, session_id: str, channel: str, block_id: str, text: str) -> None: ...
    def begin_turn(self, session_id: str, turn_id: str) -> None: ...
    def end_turn(self, session_id: str, turn_id: str) -> None: ...

    # Aggregate busy/idle state for the turn.
    def set_turn_state(self, session_id: str, running: bool, has_background: bool) -> None: ...

    # A session's own control-plane verbs (peer composition).
    def peer_call(self, session_id: str, method: str, params: dict) -> Awaitable[dict]: ...
    def peer_refuse(
        self, caller: str, method: str, params: dict
    ) -> Optional[Exception]: ...

    # Durability writes, performed by the host because it is the sole database writer.
    def ingest_call(self, session_id: str, method: str, params: dict) -> Awaitable[Any]: ...

    # A product adapter the host provides for a session's goal-review events; None turns the feature off.
    def build_goal_review_journal(self, turn_store: Any) -> Optional[Any]: ...


class NullHostServices:
    """The host services of a library embedding with no daemon behind it."""

    def publish_part(self, session_id: str, part: dict) -> None:
        return None

    def publish_delta(self, session_id: str, channel: str, block_id: str, text: str) -> None:
        return None

    def begin_turn(self, session_id: str, turn_id: str) -> None:
        return None

    def end_turn(self, session_id: str, turn_id: str) -> None:
        return None

    def set_turn_state(self, session_id: str, running: bool, has_background: bool) -> None:
        return None

    async def peer_call(self, session_id: str, method: str, params: dict) -> dict:
        raise RuntimeError(f"no host to serve {method!r} in a library-only embedding")

    def peer_refuse(
        self, caller: str, method: str, params: dict
    ) -> Optional[Exception]:
        return None

    async def ingest_call(self, session_id: str, method: str, params: dict) -> Any:
        return None

    def build_goal_review_journal(self, turn_store: Any) -> Optional[Any]:
        return None
