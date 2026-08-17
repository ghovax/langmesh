"""The daemon's implementation of the worker's host-services port."""

from __future__ import annotations

from typing import Any, Optional

from langmesh.worker.host import HostServices  # noqa: F401 — the protocol this implements
from langmeshd.daemon import state
from langmeshd.daemon.api import METHODS, RpcError, _refuse_session_caller
from langmeshd.daemon.goal_review_journal import HostGoalReviewJournal
from langmeshd.daemon.ingest import _METHODS, set_turn_state


class DaemonHostServices:
    """The worker's window into the daemon that hosts it: the live bus, ingest, and peer verbs."""

    def publish_part(self, session_id: str, part: dict) -> None:
        state.event_bus.publish_part(session_id, part)

    def publish_delta(self, session_id: str, channel: str, block_id: str, text: str) -> None:
        state.event_bus.publish_delta(session_id, channel, block_id, text)

    def begin_turn(self, session_id: str, turn_id: str) -> None:
        state.event_bus.begin_turn(session_id, turn_id)

    def end_turn(self, session_id: str, turn_id: str) -> None:
        state.event_bus.end_turn(session_id, turn_id)

    def set_turn_state(self, session_id: str, running: bool, has_background: bool) -> None:
        set_turn_state(session_id, running, has_background)

    async def peer_call(self, session_id: str, method: str, params: dict) -> dict:
        handler = METHODS.get(method)
        if handler is None:
            raise RpcError(f"the daemon has no verb {method!r}")
        return (await handler({**params, "calling_session": session_id})) or {}

    def peer_refuse(self, caller: str, method: str, params: dict) -> Optional[Exception]:
        refusal = _refuse_session_caller(caller, method, params)
        return refusal

    async def ingest_call(self, session_id: str, method: str, params: dict) -> Any:
        handler = _METHODS.get(method)
        if handler is None:
            return None
        return await handler({"session_id": session_id, **params})

    def build_goal_review_journal(self, turn_store: Any) -> Any:
        """The A2A goal-review adapter, bound to the session's own turn store."""
        return HostGoalReviewJournal(turn_store, lambda: "", host=self)

    def plugin_tools(self) -> dict[str, Any]:
        """Every tool the daemon's composed plugins contribute, keyed by name."""
        from langmeshd.features import contributed_tools

        return contributed_tools()
