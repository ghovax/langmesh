"""A session's view of the durable store: the host's own handlers, called where the session runs."""

from __future__ import annotations

import logging
from typing import Any, Optional

from a2a.server.tasks import TaskStore
from a2a.types import Task
from langmesh.protocol.turn_record import TurnRecord
from langmesh.runtime.session_control import SessionSnapshot
from langmeshd.worker.host import HostServices, NullHostServices

logger = logging.getLogger(__name__)


class HostTurnStore(TaskStore):
    """A :class:`TaskStore` whose writes are performed by the host that hosts this session."""

    def __init__(self, session_id: str, host: Any = None) -> None:
        self._session_id = session_id
        self._host: HostServices = host if host is not None else NullHostServices()

    async def _call(self, method: str, **params: Any) -> Any:
        """Run one ingest verb directly: the host that would have answered over a socket is this process."""
        try:
            return await self._host.ingest_call(self._session_id, method, params)
        except Exception:  # noqa: BLE001 — losing durability must not lose the turn
            logger.warning("persistence call %s failed", method, exc_info=True)
            return None

    # The TaskStore interface a2a expects.

    # Part of the interface the A2A handler calls through, and unused here.
    async def save(self, task: Task, context: Any = None) -> None:
        await self._call("turn.save", task=task)

    async def get(self, turn_id: str, context: Any = None) -> Optional[Task]:
        raw = await self._call("turn.get", turn_id=turn_id)
        return Task.model_validate(raw) if raw else None

    async def delete(self, turn_id: str, context: Any = None) -> None:
        await self._call("turn.delete", turn_id=turn_id)

    # The extra surface a turn uses, beyond what a2a asks for.

    async def save_turn_state(
        self,
        session_id: str,
        turn_id: str,
        messages: list,
        session_state: Optional[SessionSnapshot] = None,
        inherited_snapshot_id: str = "",
    ) -> None:
        await self._call(
            "turn.save_state",
            session_id=session_id,
            turn_id=turn_id,
            messages=messages,
            session_state=session_state.to_data() if session_state is not None else None,
            inherited_snapshot_id=inherited_snapshot_id,
        )

    async def save_session_state(self, session_id: str, session_state: SessionSnapshot) -> None:
        """Write the durable goal/task state alone, for a change that happened between turns."""
        await self._call(
            "turn.save_session_state",
            session_id=session_id,
            session_state=session_state.to_data(),
        )

    async def load_checkpoint(self, session_id: str) -> dict:
        return await self._call("turn.load_checkpoint", session_id=session_id) or {
            "messages": [],
            "inherited_snapshot_id": "",
            "inherited_message_count": 0,
        }

    async def load_session_state(self, session_id: str) -> SessionSnapshot:
        raw = await self._call("turn.load_session_state", session_id=session_id) or {}
        return SessionSnapshot.from_data(raw)

    async def create_goal_review(
        self, review_id: str, session_id: str, goal: str, created_at: str
    ) -> None:
        await self._call(
            "goal_review.create",
            review_id=review_id,
            session_id=session_id,
            goal=goal,
            created_at=created_at,
        )

    async def save_goal_review(
        self, session_id: str, review_id: str, task: Task, part: Any
    ) -> None:
        await self._call(
            "goal_review.save",
            session_id=session_id,
            review_id=review_id,
            task=task,
            part=part,
        )

    async def finish_goal_review(
        self,
        session_id: str,
        review_id: str,
        status: str,
        standing: str | None,
        completed_at: str,
    ) -> None:
        await self._call(
            "goal_review.finish",
            session_id=session_id,
            review_id=review_id,
            status=status,
            standing=standing,
            completed_at=completed_at,
        )

    async def turns_for_session(self, session_id: str) -> list[Task]:
        raw = await self._call("turn.list_for_session", session_id=session_id) or []
        return [Task.model_validate(entry) for entry in raw]

    async def control_records_for_session(self, session_id: str) -> list[tuple[str, TurnRecord]]:
        raw = await self._call("turn.list_control_records", session_id=session_id) or []
        return [
            (str(entry.get("id") or ""), TurnRecord.model_validate(entry.get("record") or {}))
            for entry in raw
        ]

    async def publish_event(self, event: dict) -> None:
        """Hand a live turn event to the host, so whoever is attached sees it now."""
        await self._call("session.event", event=event)

    async def publish_usage(self, usage: dict) -> None:
        """Hand the host the rate-limit snapshot, captured here and read by the process serving settings."""
        await self._call("session.usage", usage=usage)

    async def publish_title(self, title: str) -> None:
        """Hand the host a title this session generated for itself."""
        await self._call("session.title", title=title)

    async def aclose(self) -> None:
        """Nothing is held open: the store is the host this session runs inside."""
        return None
