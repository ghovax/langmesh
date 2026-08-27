"""A session's view of the durable store: the host's own handlers, called where the session runs."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from a2a.server.tasks import TaskUpdater
from a2a.server.tasks import TaskStore
from a2a.types import Message, Part, Task, TaskState
from langmesh.protocol.turn_record import TurnRecord
from langmesh.runtime.session_control import SessionSnapshot
from langmeshd.worker.host import HostServices, NullHostServices


class HostTurnStore(TaskStore):
    """A :class:`TaskStore` whose writes are performed by the host that hosts this session."""

    def __init__(self, session_id: str, host: Any = None) -> None:
        self._session_id = session_id
        self._host: HostServices = host if host is not None else NullHostServices()
        self._persistence_changed = asyncio.Condition()
        self._persisted_statuses: set[tuple[str, str]] = set()
        self._persisted_artifacts: set[tuple[str, str]] = set()

    async def _call(self, method: str, **params: Any) -> Any:
        """Run one ingest verb directly: the host that would have answered over a socket is this process."""
        return await self._host.ingest_call(self._session_id, method, params)

    # The TaskStore interface a2a expects.

    # Part of the interface the A2A handler calls through, and unused here.
    async def save(self, task: Task, context: Any = None) -> None:
        await self._call("turn.save", task=task)
        status_timestamp = str(task.status.timestamp or "")
        artifacts = tuple(task.artifacts or ())
        async with self._persistence_changed:
            if status_timestamp:
                self._persisted_statuses.add((task.id, status_timestamp))
            self._persisted_artifacts.update(
                (task.id, artifact.artifact_id) for artifact in artifacts
            )
            self._persistence_changed.notify_all()

    async def commit_status(
        self,
        updater: TaskUpdater,
        state: TaskState,
        message: Message | None = None,
        *,
        final: bool = False,
    ) -> None:
        """Publish a status only after the task store acknowledges its exact timestamp."""
        timestamp = datetime.now(timezone.utc).isoformat()
        await updater.update_status(
            state,
            message,
            final=final,
            timestamp=timestamp,
        )
        key = (updater.task_id, timestamp)
        try:
            async with asyncio.timeout(30):
                async with self._persistence_changed:
                    await self._persistence_changed.wait_for(
                        lambda: key in self._persisted_statuses
                    )
                    self._persisted_statuses.discard(key)
        except TimeoutError as error:
            raise RuntimeError(
                f"The {state.value} state for turn {updater.task_id} was not committed."
            ) from error

    async def commit_artifact(
        self,
        updater: TaskUpdater,
        parts: list[Part],
        *,
        artifact_id: str,
        name: str,
        last_chunk: bool,
    ) -> None:
        """Publish an artifact only after the task store acknowledges its stable identity."""
        await updater.add_artifact(
            parts,
            artifact_id=artifact_id,
            name=name,
            last_chunk=last_chunk,
        )
        key = (updater.task_id, artifact_id)
        try:
            async with asyncio.timeout(30):
                async with self._persistence_changed:
                    await self._persistence_changed.wait_for(
                        lambda: key in self._persisted_artifacts
                    )
                    self._persisted_artifacts.discard(key)
        except TimeoutError as error:
            raise RuntimeError(
                f"Artifact {artifact_id} for turn {updater.task_id} was not committed."
            ) from error

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

    async def complete_goal_review(
        self,
        session_id: str,
        review_id: str,
        status: str,
        standing: str | None,
        completed_at: str,
    ) -> None:
        await self._call(
            "goal_review.complete",
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
