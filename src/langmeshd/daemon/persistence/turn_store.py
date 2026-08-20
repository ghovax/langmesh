"""An append-only A2A task store, so a save costs the new messages rather than the whole turn."""

from __future__ import annotations

import json
import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator, Optional, Sequence

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.engine import RowMapping

from pydantic import ValidationError

from a2a.server.context import ServerCallContext
from a2a.server.tasks import TaskStore
from a2a.types import Message, Role, Task, TaskState, TaskStatus

from langmesh.base.content.message_content import content_block_identifier
from langmesh.base.primitives.serialization import compact, conversation_snapshot_id
from langmesh.protocol.turn_record import ReconcileAction, TurnRecord, reconcile_action
from langmesh.protocol.events import ErrorEvent
from langmesh.protocol.parts import _event_part
from langmesh.protocol.metadata import part_payload, wrap_part_payload
from langmesh.runtime.plugins.goal_review.goal import Goal


def _dump(model) -> str:
    """Serialize a model to JSON by field names, mirroring how the SDK's own store round-trips."""
    return json.dumps(model.model_dump(mode="json"))


def _goal_from_persisted_state(raw: str | None) -> dict | None:
    """The goal a persisted session state carries, as the interface shows it, or None when it has none."""
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    stored = state.get("goal") if isinstance(state, dict) else None
    if not isinstance(stored, dict) or not str(stored.get("text", "") or "").strip():
        return None
    try:
        return Goal.model_validate(stored).public()
    except ValidationError:
        # A stored goal that no longer validates is dropped rather than guessed at.
        return None


# Adjacent same-kind deltas merge into one message, so a replay re-reduces far fewer and larger rows.


def _sole_part(message: object) -> dict | None:
    parts = message.get("parts") if isinstance(message, dict) else None  # type: ignore[union-attr]
    if not parts or not isinstance(parts, list) or len(parts) != 1:
        return None
    part = parts[0]
    return part if isinstance(part, dict) else None


def _agent_text_part(message: object) -> dict | None:
    if not isinstance(message, dict) or message.get("role") != "agent":
        return None
    part = _sole_part(message)
    if not part or part.get("kind") != "text":
        return None
    return part


def _agent_text(message: object) -> str | None:
    part = _agent_text_part(message)
    return str(part.get("text", "")) if part is not None else None


def _sole_data(message: object, kind: str) -> dict | None:
    """The data dict if the message is a single data-part agent message of `kind`."""
    if not isinstance(message, dict) or message.get("role") != "agent":
        return None
    part = _sole_part(message)
    if not part or part.get("kind") != "data":
        return None
    data = part.get("data")
    if not isinstance(data, dict):
        return None
    payload = part_payload(data)
    # Read the short-lived flat representation too, so histories written by either version converge.
    if not payload and data.get("kind"):
        payload = data
    return payload if payload.get("kind") == kind else None


def _path_key(data: dict) -> tuple:
    """A hashable identity for the agent that produced an event, so merging only happens within one agent."""
    path = data.get("path") or []
    return tuple((segment.get("group_id"), segment.get("step_id")) for segment in path)


@dataclass
class _AdjacentTextRun:
    """One semantic text run, owning both its identity and its one-time materialization."""

    identity: tuple
    part: dict
    payload: dict | None
    chunks: list[str] = field(default_factory=list)

    def absorbs(self, other: "_AdjacentTextRun") -> bool:
        if self.identity != other.identity:
            return False
        self.chunks.extend(other.chunks)
        return True

    def materialize(self) -> None:
        text = "".join(self.chunks)
        if self.payload is None:
            self.part["text"] = text
        else:
            self.part["data"] = wrap_part_payload({**self.payload, "text": text})


def _text_run(message: object) -> _AdjacentTextRun | None:
    """The mergeable text identity carried by one stored message, if it has one."""
    text = _agent_text(message)
    if text is not None:
        part = _agent_text_part(message)
        block_identifier = (
            content_block_identifier(part.get("metadata")) if part is not None else None
        )
        if part is not None and block_identifier is not None:
            return _AdjacentTextRun(("agent", block_identifier), part, None, [text])
        return None

    for category in ("text", "thinking"):
        payload = _sole_data(message, category)
        if payload is None:
            continue
        part = _sole_part(message)
        if part is None:
            return None
        identity = (category, *_path_key(payload), str(payload.get("block_id", "")))
        return _AdjacentTextRun(identity, part, payload, [str(payload.get("text", ""))])
    return None


def _compact_history(messages: list) -> list:
    """Merge adjacent same-kind messages in linear time, joining each semantic run once."""
    compacted: list = []
    active_run: _AdjacentTextRun | None = None

    for message in messages:
        incoming_run = _text_run(message)
        if incoming_run is not None and active_run is not None and active_run.absorbs(incoming_run):
            continue
        if active_run is not None:
            active_run.materialize()
        compacted.append(message)
        active_run = incoming_run
    if active_run is not None:
        active_run.materialize()
    return compacted


_TERMINAL_TASK_STATES = {
    TaskState.completed.value,
    TaskState.canceled.value,
    TaskState.failed.value,
    TaskState.rejected.value,
}


# The turn kind carried in a task's head metadata, which restart reconciliation reads to decide its fate.
def _task_state_value(task: Task) -> str:
    state = task.status.state
    return state.value if isinstance(state, TaskState) else str(state)


def _is_terminal_task(task: Task) -> bool:
    return _task_state_value(task) in _TERMINAL_TASK_STATES


def _stored_task(head_row, history_rows: list[str], artifact_rows: list[str]) -> Task:
    """Decode and compact one immutable turn off the daemon's latency-critical event loop."""
    return Task.model_validate(
        {
            "id": str(head_row["id"]),
            "context_id": head_row["session_id"],
            "kind": head_row["kind"] or "task",
            "status": json.loads(head_row["status"]),
            "metadata": json.loads(head_row["turn_metadata"])
            if head_row["turn_metadata"]
            else None,
            "history": _compact_history([json.loads(message) for message in history_rows]),
            "artifacts": [json.loads(artifact) for artifact in artifact_rows] or None,
        }
    )


class AppendOnlyTaskStore(TaskStore):
    """An A2A task store that persists history and artifacts incrementally, across three tables."""

    def __init__(self, engine: AsyncEngine):
        self._engine = engine
        self._metadata = MetaData()
        # The task head is small and mutable; history remains append-only below it.
        self._head = Table(
            "turn_head",
            self._metadata,
            Column("id", String, primary_key=True),
            Column("session_id", String),
            Column("kind", String),
            Column("status", Text),
            Column("turn_metadata", Text),
        )
        # Append-only history ordered by the database's own autoincrement, so concurrent appends cannot collide.
        self._history = Table(
            "turn_history",
            self._metadata,
            Column("row_id", Integer, primary_key=True, autoincrement=True),
            Column("turn_id", String),
            Column("message", Text),
            sqlite_autoincrement=True,
        )
        # Artifacts are few and may be revised, so they upsert by id.
        self._artifacts = Table(
            "turn_artifacts",
            self._metadata,
            Column("row_id", Integer, primary_key=True, autoincrement=True),
            Column("turn_id", String),
            Column("artifact_id", String),
            Column("artifact", Text),
            UniqueConstraint("turn_id", "artifact_id", name="uq_task_artifact_id"),
        )
        self._checkpoint = Table(
            "turn_checkpoint",
            self._metadata,
            Column("session_id", String, primary_key=True),
            Column("turn_id", String),
            Column("messages", Text),
            Column("updated_at", String),
        )
        # An inherited prefix is content-addressed so identical forks share one row, and the checkpoint above holds only what the child wrote after the fork.
        self._conversation_snapshot = Table(
            "conversation_snapshot",
            self._metadata,
            Column("snapshot_id", String, primary_key=True),
            Column("messages", Text),
        )
        self._conversation_inheritance = Table(
            "conversation_inheritance",
            self._metadata,
            Column("session_id", String, primary_key=True),
            Column("snapshot_id", String),
        )
        # A context's durable goal and task list, beside the checkpoint so a restart restores the objective too.
        self._session_state = Table(
            "session_state",
            self._metadata,
            Column("session_id", String, primary_key=True),
            Column("state", Text),
            Column("updated_at", String),
        )
        self._goal_reviews = Table(
            "goal_review_sessions",
            self._metadata,
            Column("review_id", String, primary_key=True),
            Column("session_id", String, nullable=False),
            Column("goal", Text, nullable=False),
            Column("status", String, nullable=False),
            Column("standing", String),
            Column("created_at", String, nullable=False),
            Column("completed_at", String),
        )
        # User message history scoped to the working directory, for arrow-key recall within a project.
        self._user_messages = Table(
            "user_message_history",
            self._metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("working_directory", String),
            Column("message", Text),
            Column("created_at", DateTime, server_default=func.now()),
        )
        self._initialized = False
        # How many history rows are persisted per task, kept in memory rather than re-counted.
        self._persisted_counts: dict[str, int] = {}
        # Tasks whose history has been terminally compacted, so a stray later save is caught rather than duplicating rows.
        self._terminal_turns: set[str] = set()

    async def initialize(self) -> None:
        async with self._engine.begin() as connection:
            await connection.run_sync(self._metadata.create_all)
            await connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS idx_turn_head_session_id_id ON turn_head(session_id, id)"
            )
            await connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS idx_turn_history_turn_id_row_id ON turn_history(turn_id, row_id)"
            )
            await connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS idx_turn_artifacts_turn_id_row_id ON turn_artifacts(turn_id, row_id)"
            )
            await connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS idx_conversation_inheritance_snapshot_id ON conversation_inheritance(snapshot_id)"
            )
            await connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS idx_user_message_history_working_directory_created_at ON user_message_history(working_directory, created_at DESC)"
            )
            await connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS idx_goal_review_sessions_session_created ON goal_review_sessions(session_id, created_at DESC)"
            )
        self._initialized = True

    async def _ensure_initialized(self) -> None:
        if not self._initialized:
            await self.initialize()

    async def reconcile_orphaned_turns(self) -> list[str]:
        """Restart reconciliation: a durable pause is restored, and anything else non-terminal is marked failed."""
        await self._ensure_initialized()
        failed_task_ids: list[str] = []
        input_required = TaskState.input_required.value
        async with self._engine.begin() as connection:
            head_rows = (await connection.execute(select(self._head))).mappings().all()
            for head_row in head_rows:
                current_state = str(json.loads(head_row["status"]).get("state", ""))
                if current_state in _TERMINAL_TASK_STATES:
                    continue
                metadata = (
                    json.loads(head_row["turn_metadata"]) if head_row["turn_metadata"] else {}
                )
                kind = TurnRecord.from_metadata(metadata).kind
                if (
                    reconcile_action(kind, current_state, input_required=input_required)
                    is ReconcileAction.PRESERVE
                ):
                    continue
                turn_id = str(head_row["id"])
                session_id = str(head_row["session_id"] or "")
                interrupted_message = Message(
                    role=Role.agent,
                    parts=[_event_part(ErrorEvent(code="turn_interrupted"))],
                    message_id=uuid.uuid4().hex,
                    task_id=turn_id,
                    context_id=session_id or None,
                )
                interrupted_at = datetime.now(timezone.utc).isoformat()
                interrupted_status = TaskStatus(
                    state=TaskState.failed,
                    message=interrupted_message,
                    timestamp=interrupted_at,
                )
                await connection.execute(
                    update(self._head)
                    .where(self._head.c.id == turn_id)
                    .values(status=_dump(interrupted_status))
                )
                await connection.execute(
                    update(self._goal_reviews)
                    .where(self._goal_reviews.c.review_id == session_id)
                    .values(status=TaskState.failed.value, completed_at=interrupted_at)
                )
                stored_state = (
                    await connection.execute(
                        select(self._session_state.c.state).where(
                            self._session_state.c.session_id == session_id
                        )
                    )
                ).scalar()
                retryable_state = json.loads(stored_state) if stored_state else {}
                retryable_state["turn_recovery"] = "retryable"
                state_insert = sqlite_insert(self._session_state).values(
                    session_id=session_id,
                    state=json.dumps(retryable_state),
                    updated_at=interrupted_at,
                )
                await connection.execute(
                    state_insert.on_conflict_do_update(
                        index_elements=[self._session_state.c.session_id],
                        set_={"state": state_insert.excluded.state, "updated_at": interrupted_at},
                    )
                )
                failed_task_ids.append(turn_id)
        return failed_task_ids

    async def save_turn_state(
        self,
        session_id: str,
        turn_id: str,
        messages: list,
        session_state: dict | None = None,
        inherited_snapshot_id: str = "",
    ) -> None:
        """Atomically snapshot a context's conversation checkpoint and, when it changed, its goal and task state — a child writing only the suffix beyond ``inherited_snapshot_id``, and omitting that pointer detaching the prefix into a whole snapshot."""
        await self._ensure_initialized()
        if not session_id:
            return
        now = datetime.now(timezone.utc).isoformat()
        async with self._engine.begin() as connection:
            checkpoint_insert = sqlite_insert(self._checkpoint).values(
                session_id=session_id,
                turn_id=turn_id,
                messages=json.dumps(messages),
                updated_at=now,
            )
            await connection.execute(
                checkpoint_insert.on_conflict_do_update(
                    index_elements=[self._checkpoint.c.session_id],
                    set_={
                        "turn_id": turn_id,
                        "messages": checkpoint_insert.excluded.messages,
                        "updated_at": now,
                    },
                )
            )
            detached = False
            if inherited_snapshot_id:
                snapshot_exists = (
                    await connection.execute(
                        select(self._conversation_snapshot.c.snapshot_id).where(
                            self._conversation_snapshot.c.snapshot_id == inherited_snapshot_id
                        )
                    )
                ).scalar()
                if snapshot_exists is None:
                    raise ValueError(
                        f"Unknown inherited conversation snapshot {inherited_snapshot_id!r}"
                    )
                inheritance_insert = sqlite_insert(self._conversation_inheritance).values(
                    session_id=session_id,
                    snapshot_id=inherited_snapshot_id,
                )
                await connection.execute(
                    inheritance_insert.on_conflict_do_update(
                        index_elements=[self._conversation_inheritance.c.session_id],
                        set_={"snapshot_id": inheritance_insert.excluded.snapshot_id},
                    )
                )
            else:
                result = await connection.execute(
                    delete(self._conversation_inheritance).where(
                        self._conversation_inheritance.c.session_id == session_id
                    )
                )
                detached = bool(result.rowcount)
            if session_state is not None:
                state_insert = sqlite_insert(self._session_state).values(
                    session_id=session_id,
                    state=json.dumps(session_state),
                    updated_at=now,
                )
                await connection.execute(
                    state_insert.on_conflict_do_update(
                        index_elements=[self._session_state.c.session_id],
                        set_={"state": state_insert.excluded.state, "updated_at": now},
                    )
                )
            if detached:
                await self._delete_unreferenced_conversation_snapshots(connection)

    async def seed_inherited_conversation(self, session_id: str, messages: list[dict]) -> str:
        """Point a new child at a shared snapshot and start its local checkpoint empty."""
        await self._ensure_initialized()
        if not session_id or not messages:
            return ""
        snapshot_id = conversation_snapshot_id(messages)
        now = datetime.now(timezone.utc).isoformat()
        async with self._engine.begin() as connection:
            await connection.execute(
                sqlite_insert(self._conversation_snapshot)
                .values(snapshot_id=snapshot_id, messages=compact(messages))
                .on_conflict_do_nothing(index_elements=[self._conversation_snapshot.c.snapshot_id])
            )
            checkpoint_insert = sqlite_insert(self._checkpoint).values(
                session_id=session_id,
                turn_id="",
                messages="[]",
                updated_at=now,
            )
            await connection.execute(
                checkpoint_insert.on_conflict_do_update(
                    index_elements=[self._checkpoint.c.session_id],
                    set_={"turn_id": "", "messages": "[]", "updated_at": now},
                )
            )
            inheritance_insert = sqlite_insert(self._conversation_inheritance).values(
                session_id=session_id,
                snapshot_id=snapshot_id,
            )
            await connection.execute(
                inheritance_insert.on_conflict_do_update(
                    index_elements=[self._conversation_inheritance.c.session_id],
                    set_={"snapshot_id": snapshot_id},
                )
            )
        return snapshot_id

    async def _delete_unreferenced_conversation_snapshots(self, connection) -> None:
        referenced = select(self._conversation_inheritance.c.snapshot_id)
        await connection.execute(
            delete(self._conversation_snapshot).where(
                self._conversation_snapshot.c.snapshot_id.not_in(referenced)
            )
        )

    async def save_session_state(self, session_id: str, session_state: dict) -> None:
        """Write a context's goal and task state alone, for the changes that happen outside a turn."""
        await self._ensure_initialized()
        if not session_id:
            return
        now = datetime.now(timezone.utc).isoformat()
        async with self._engine.begin() as connection:
            state_insert = sqlite_insert(self._session_state).values(
                session_id=session_id,
                state=json.dumps(session_state),
                updated_at=now,
            )
            await connection.execute(
                state_insert.on_conflict_do_update(
                    index_elements=[self._session_state.c.session_id],
                    set_={"state": state_insert.excluded.state, "updated_at": now},
                )
            )

    async def create_goal_review(
        self, review_id: str, session_id: str, goal: str, created_at: str
    ) -> None:
        """Register one review session before its first transcript event is saved."""
        await self._ensure_initialized()
        async with self._engine.begin() as connection:
            await connection.execute(
                sqlite_insert(self._goal_reviews).values(
                    review_id=review_id,
                    session_id=session_id,
                    goal=goal,
                    status="working",
                    standing=None,
                    created_at=created_at,
                    completed_at=None,
                )
            )

    async def finish_goal_review(
        self, review_id: str, status: str, standing: str | None, completed_at: str
    ) -> None:
        """Settle one review session while preserving its linked transcript."""
        await self._ensure_initialized()
        async with self._engine.begin() as connection:
            await connection.execute(
                update(self._goal_reviews)
                .where(self._goal_reviews.c.review_id == review_id)
                .values(status=status, standing=standing, completed_at=completed_at)
            )

    async def goal_reviews_for_session(self, session_id: str) -> list[dict]:
        """List a session's review sessions newest first."""
        await self._ensure_initialized()
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(self._goal_reviews)
                        .where(self._goal_reviews.c.session_id == session_id)
                        .order_by(self._goal_reviews.c.created_at.desc())
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def goal_review(self, review_id: str) -> dict | None:
        """Read one review-session descriptor by its stable identifier."""
        await self._ensure_initialized()
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        select(self._goal_reviews).where(
                            self._goal_reviews.c.review_id == review_id
                        )
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row is not None else None

    async def load_checkpoint(self, session_id: str) -> dict:
        """The context's whole conversation and its shared-prefix boundary, for the caller to rehydrate and repair."""
        await self._ensure_initialized()
        if not session_id:
            return {"messages": [], "inherited_snapshot_id": "", "inherited_message_count": 0}
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(
                        self._checkpoint.c.messages,
                        self._conversation_inheritance.c.snapshot_id,
                        self._conversation_snapshot.c.messages,
                    )
                    .select_from(
                        self._checkpoint.outerjoin(
                            self._conversation_inheritance,
                            self._conversation_inheritance.c.session_id
                            == self._checkpoint.c.session_id,
                        ).outerjoin(
                            self._conversation_snapshot,
                            self._conversation_snapshot.c.snapshot_id
                            == self._conversation_inheritance.c.snapshot_id,
                        )
                    )
                    .where(self._checkpoint.c.session_id == session_id)
                )
            ).first()
        if not row:
            return {"messages": [], "inherited_snapshot_id": "", "inherited_message_count": 0}
        try:
            local_messages = json.loads(row[0])
            inherited_messages = json.loads(row[2]) if row[1] and row[2] else []
            if not isinstance(local_messages, list) or not isinstance(inherited_messages, list):
                raise TypeError("Conversation checkpoints must contain message lists")
            return {
                "messages": [*inherited_messages, *local_messages],
                "inherited_snapshot_id": str(row[1] or ""),
                "inherited_message_count": len(inherited_messages),
            }
        except (json.JSONDecodeError, TypeError):
            return {"messages": [], "inherited_snapshot_id": "", "inherited_message_count": 0}

    async def load_session_state(self, session_id: str) -> dict:
        """The context's persisted goal and task state, or an empty dict for a fresh context."""
        await self._ensure_initialized()
        if not session_id:
            return {}
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(self._session_state.c.state).where(
                        self._session_state.c.session_id == session_id
                    )
                )
            ).scalar()
        if not row:
            return {}
        try:
            data = json.loads(row)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    async def session_goals(self, session_ids: Sequence[str]) -> dict[str, dict]:
        """The goals persisted beside each session's checkpoint, keyed by session, for those that have one."""
        await self._ensure_initialized()
        session_ids = [identifier for identifier in session_ids if identifier]
        if not session_ids:
            return {}
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(self._session_state.c.session_id, self._session_state.c.state).where(
                        self._session_state.c.session_id.in_(session_ids)
                    )
                )
            ).all()
        goals: dict[str, dict] = {}
        for session_id, raw in rows:
            goal = _goal_from_persisted_state(raw)
            if goal is not None:
                goals[session_id] = goal
        return goals

    async def _persisted_count(self, connection, turn_id: str) -> int:
        """How many history rows are already persisted, so a save appends only the suffix not yet stored."""
        cached = self._persisted_counts.get(turn_id)
        if cached is not None:
            return cached
        result = await connection.execute(
            select(func.count())
            .select_from(self._history)
            .where(self._history.c.turn_id == turn_id)
        )
        seeded = int(result.scalar() or 0)
        self._persisted_counts[turn_id] = seeded
        return seeded

    async def _compact_persisted_history(self, connection, turn_id: str) -> int:
        """Rewrite a task's already-persisted history in place with its compacted form, keeping row order."""
        existing_rows = (
            await connection.execute(
                select(self._history.c.row_id, self._history.c.message)
                .where(self._history.c.turn_id == turn_id)
                .order_by(self._history.c.row_id)
            )
        ).all()
        compacted_messages = _compact_history([json.loads(row.message) for row in existing_rows])
        if len(compacted_messages) > len(existing_rows):  # pragma: no cover - invariant guard
            raise AssertionError(
                f"compact grew history for {turn_id}: {len(existing_rows)} -> {len(compacted_messages)}"
            )
        for message_index, message in enumerate(compacted_messages):
            await connection.execute(
                update(self._history)
                .where(self._history.c.row_id == existing_rows[message_index].row_id)
                .values(message=json.dumps(message))
            )
        surplus_row_ids = [row.row_id for row in existing_rows[len(compacted_messages) :]]
        if surplus_row_ids:
            await connection.execute(
                delete(self._history).where(self._history.c.row_id.in_(surplus_row_ids))
            )
        return len(compacted_messages)

    async def save(self, task: Task, context: ServerCallContext | None = None) -> None:
        await self._ensure_initialized()
        history = task.history or []
        artifacts = task.artifacts or []
        terminal = _is_terminal_task(task)
        if task.id in self._terminal_turns and not terminal:
            # The persisted rows are already the compacted merge, so a later non-terminal save would duplicate them.
            raise ValueError(
                f"non-terminal save for already-terminal task {task.id}: a terminal save must be the last save for a task"
            )
        async with self._engine.begin() as connection:
            # The in-memory terminal guard does not survive a restart, so the durable head is consulted when the task is new to this process: a non-terminal save of a task whose head is already terminal must still be refused.
            if (
                not terminal
                and task.id not in self._terminal_turns
                and self._persisted_counts.get(task.id) is None
            ):
                stored_status = (
                    await connection.execute(
                        select(self._head.c.status).where(self._head.c.id == task.id)
                    )
                ).scalar()
                if stored_status is not None and _is_terminal_task(
                    Task.model_validate(
                        {
                            "id": task.id,
                            "context_id": task.context_id,
                            "status": json.loads(stored_status),
                            "history": [],
                        }
                    )
                ):
                    raise ValueError(
                        f"non-terminal save for already-terminal task {task.id}: a terminal save must be the last save for a task"
                    )
            # Head: tiny upsert of the latest status + metadata.
            head_values = {
                "id": task.id,
                "session_id": task.context_id,
                "kind": task.kind,
                "status": _dump(task.status),
                "turn_metadata": json.dumps(task.metadata) if task.metadata is not None else None,
            }
            head_insert = sqlite_insert(self._head).values(**head_values)
            await connection.execute(
                head_insert.on_conflict_do_update(
                    index_elements=[self._head.c.id],
                    set_={
                        "session_id": head_values["session_id"],
                        "kind": head_values["kind"],
                        "status": head_values["status"],
                        "turn_metadata": head_values["turn_metadata"],
                    },
                )
            )

            # Insert only the messages not yet persisted, since the list only ever grows.
            persisted = await self._persisted_count(connection, task.id)
            new_messages = history[persisted:]
            if new_messages:
                await connection.execute(
                    self._history.insert(),
                    [{"turn_id": task.id, "message": _dump(message)} for message in new_messages],
                )
                self._persisted_counts[task.id] = persisted + len(new_messages)

            if terminal:
                # The terminal history is the canonical compacted form. Rewriting the rows wholesale keeps a repeated terminal save idempotent even after a restart, when the in-memory terminal guard and persisted-count cache are cold: a fresh store would otherwise re-insert the messages the merge folded away.
                await connection.execute(
                    delete(self._history).where(self._history.c.turn_id == task.id)
                )
                # `history` holds A2A Message objects; compaction merges the serialized form.
                compacted = _compact_history([json.loads(_dump(message)) for message in history])
                if compacted:
                    await connection.execute(
                        self._history.insert(),
                        # The compacted items are already serialized dicts.
                        [
                            {"turn_id": task.id, "message": json.dumps(message)}
                            for message in compacted
                        ],
                    )
                self._persisted_counts[task.id] = len(compacted)
                self._terminal_turns.add(task.id)

            # Artifacts: upsert each by id (replace-in-place is safe and bounded).
            for artifact in artifacts:
                artifact_json = _dump(artifact)
                artifact_insert = sqlite_insert(self._artifacts).values(
                    turn_id=task.id,
                    artifact_id=artifact.artifact_id,
                    artifact=artifact_json,
                )
                await connection.execute(
                    artifact_insert.on_conflict_do_update(
                        index_elements=[
                            self._artifacts.c.turn_id,
                            self._artifacts.c.artifact_id,
                        ],
                        set_={"artifact": artifact_json},
                    )
                )

    async def get(self, turn_id: str, context: ServerCallContext | None = None) -> Optional[Task]:
        await self._ensure_initialized()
        async with self._engine.connect() as connection:
            head_row = (
                (await connection.execute(select(self._head).where(self._head.c.id == turn_id)))
                .mappings()
                .first()
            )
            if head_row is None:
                return None
            history_rows = (
                (
                    await connection.execute(
                        select(self._history.c.message)
                        .where(self._history.c.turn_id == turn_id)
                        .order_by(self._history.c.row_id)
                    )
                )
                .scalars()
                .all()
            )
            artifact_rows = (
                (
                    await connection.execute(
                        select(self._artifacts.c.artifact).where(
                            self._artifacts.c.turn_id == turn_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        data = {
            "id": head_row["id"],
            "context_id": head_row["session_id"],
            "kind": head_row["kind"] or "task",
            "status": json.loads(head_row["status"]),
            "metadata": json.loads(head_row["turn_metadata"])
            if head_row["turn_metadata"]
            else None,
            "history": _compact_history([json.loads(message) for message in history_rows]),
            "artifacts": [json.loads(artifact) for artifact in artifact_rows] or None,
        }
        return Task.model_validate(data)

    async def turns_for_session(self, session_id: str) -> list[Task]:
        """Every task in a context, batched into one head, history and artifact pass rather than three queries per task."""
        await self._ensure_initialized()
        async with self._engine.connect() as connection:
            head_rows = (
                (
                    await connection.execute(
                        select(self._head)
                        .where(self._head.c.session_id == session_id)
                        # Ordered by when each turn actually started, since a turn's id is a random UUID and sorts arbitrarily.
                        .order_by(self._head.c.id)
                    )
                )
                .mappings()
                .all()
            )
            turn_ids = [str(row["id"]) for row in head_rows]
            if not turn_ids:
                return []

            history_rows = (
                await connection.execute(
                    select(self._history.c.turn_id, self._history.c.message)
                    .where(self._history.c.turn_id.in_(turn_ids))
                    # Globally by row id rather than grouped, because the append order is the chronology.
                    .order_by(self._history.c.row_id)
                )
            ).all()
            artifact_rows = (
                await connection.execute(
                    select(self._artifacts.c.turn_id, self._artifacts.c.artifact)
                    .where(self._artifacts.c.turn_id.in_(turn_ids))
                    .order_by(self._artifacts.c.turn_id, self._artifacts.c.row_id)
                )
            ).all()

        histories: dict[str, list[str]] = {turn_id: [] for turn_id in turn_ids}
        artifacts: dict[str, list[str]] = {turn_id: [] for turn_id in turn_ids}
        for turn_id, message in history_rows:
            histories[str(turn_id)].append(message)
        for turn_id, artifact in artifact_rows:
            artifacts[str(turn_id)].append(artifact)

        # When each turn began, from its first appended message; a turn with no history yet sorts last.
        started: dict[str, int] = {}
        for position, (turn_id, _message) in enumerate(history_rows):
            started.setdefault(str(turn_id), position)

        turns: list[Task] = []
        for head_row in sorted(
            head_rows, key=lambda row: started.get(str(row["id"]), len(history_rows))
        ):
            turn_id = str(head_row["id"])
            data = {
                "id": turn_id,
                "context_id": head_row["session_id"],
                "kind": head_row["kind"] or "task",
                "status": json.loads(head_row["status"]),
                "metadata": json.loads(head_row["turn_metadata"])
                if head_row["turn_metadata"]
                else None,
                "history": _compact_history(
                    [json.loads(message) for message in histories[turn_id]]
                ),
                "artifacts": [json.loads(artifact) for artifact in artifacts[turn_id]] or None,
            }
            turns.append(Task.model_validate(data))
        return turns

    async def control_records_for_session(self, session_id: str) -> list[tuple[str, TurnRecord]]:
        """Turn control metadata newest-first, without loading transcript or artifact payloads."""
        await self._ensure_initialized()
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(self._head.c.id, self._head.c.turn_metadata)
                    .where(self._head.c.session_id == session_id)
                    .order_by(self._head.c.id.desc())
                )
            ).all()
        return [
            (
                str(turn_id),
                TurnRecord.from_metadata(json.loads(metadata) if metadata else None),
            )
            for turn_id, metadata in rows
        ]

    async def latest_history_row_id(self, session_id: str) -> int:
        """The durable high-water mark for one context, used to cut history cleanly from its live suffix."""
        await self._ensure_initialized()
        turn_ids = select(self._head.c.id).where(self._head.c.session_id == session_id)
        async with self._engine.connect() as connection:
            value = await connection.scalar(
                select(func.max(self._history.c.row_id)).where(
                    self._history.c.turn_id.in_(turn_ids)
                )
            )
        return int(value or 0)

    async def stream_turns_for_session(
        self,
        session_id: str,
        *,
        through_row_id: int,
        exclude_turn_id: str = "",
    ) -> AsyncIterator[Task]:
        """Yield immutable complete turns newest-to-oldest through one durable high-water mark.

        A turn is the protocol's natural consistency unit: yielding whole compacted turns means the
        browser can prepend each result without holding raw history or repairing half a tool lifecycle.
        No conversation-size policy appears here; iteration continues until the durable cut is exhausted.
        """
        await self._ensure_initialized()
        async with self._engine.connect() as connection:
            head_rows = (
                (
                    await connection.execute(
                        select(
                            self._head,
                            func.max(self._history.c.row_id).label("last_row_id"),
                        )
                        .outerjoin(self._history, self._history.c.turn_id == self._head.c.id)
                        .where(self._head.c.session_id == session_id)
                        .group_by(self._head.c.id)
                        .order_by(func.max(self._history.c.row_id).desc())
                    )
                )
                .mappings()
                .all()
            )
            main_turn_ids: list[str] = []
            related_rows: list[RowMapping] = []
            for row in head_rows:
                turn_id = str(row["id"])
                if turn_id == exclude_turn_id:
                    continue
                metadata = json.loads(row["turn_metadata"]) if row["turn_metadata"] else None
                if TurnRecord.from_metadata(metadata).reference_task_ids:
                    related_rows.append(row)
                else:
                    main_turn_ids.append(turn_id)
            head_by_id = {str(row["id"]): row for row in head_rows}
            ordered_turn_ids = [
                turn_id
                for turn_id in main_turn_ids
                if head_by_id[turn_id]["last_row_id"] is not None
                and int(head_by_id[turn_id]["last_row_id"]) <= through_row_id
            ]
            if ordered_turn_ids:
                history_rows = (
                    await connection.execute(
                        select(self._history.c.turn_id, self._history.c.message)
                        .where(
                            self._history.c.turn_id.in_(ordered_turn_ids),
                            self._history.c.row_id <= through_row_id,
                        )
                        .order_by(self._history.c.turn_id, self._history.c.row_id)
                    )
                ).all()
                artifact_rows = (
                    await connection.execute(
                        select(self._artifacts.c.turn_id, self._artifacts.c.artifact)
                        .where(self._artifacts.c.turn_id.in_(ordered_turn_ids))
                        .order_by(self._artifacts.c.turn_id, self._artifacts.c.row_id)
                    )
                ).all()
            else:
                history_rows = []
                artifact_rows = []

        histories: dict[str, list[str]] = {turn_id: [] for turn_id in ordered_turn_ids}
        artifacts: dict[str, list[str]] = {turn_id: [] for turn_id in ordered_turn_ids}
        for turn_id, message in history_rows:
            histories[str(turn_id)].append(message)
        for turn_id, artifact in artifact_rows:
            artifacts[str(turn_id)].append(artifact)

        # All database work is complete before the SSE consumer receives the first turn, so browser backpressure cannot pin a connection. Each turn's CPU-heavy decode still leaves the event loop.
        for turn_id in ordered_turn_ids:
            yield await asyncio.to_thread(
                _stored_task,
                head_by_id[turn_id],
                histories[turn_id],
                artifacts[turn_id],
            )

        # Reference-only task heads carry metadata but no transcript rows. They follow the durable turns and are harmless to renderers that intentionally filter them from the main timeline.
        for row in related_rows:
            yield Task.model_validate(
                {
                    "id": str(row["id"]),
                    "context_id": row["session_id"],
                    "kind": row["kind"] or "task",
                    "status": json.loads(row["status"]),
                    "metadata": json.loads(row["turn_metadata"]) if row["turn_metadata"] else None,
                    "history": [],
                    "artifacts": None,
                }
            )

    async def delete(self, turn_id: str, context: ServerCallContext | None = None) -> None:
        await self._ensure_initialized()
        async with self._engine.begin() as connection:
            await connection.execute(
                delete(self._history).where(self._history.c.turn_id == turn_id)
            )
            await connection.execute(
                delete(self._artifacts).where(self._artifacts.c.turn_id == turn_id)
            )
            await connection.execute(delete(self._head).where(self._head.c.id == turn_id))
        self._persisted_counts.pop(turn_id, None)
        self._terminal_turns.discard(turn_id)

    async def delete_session(self, session_id: str) -> None:
        """Drop every durable trace of a context, so session deletion does not need to know this store's tables."""
        await self._ensure_initialized()
        async with self._engine.begin() as connection:
            review_ids = list(
                (
                    await connection.execute(
                        select(self._goal_reviews.c.review_id).where(
                            self._goal_reviews.c.session_id == session_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            context_ids = [session_id, *review_ids]
            turn_ids = (
                (
                    await connection.execute(
                        select(self._head.c.id).where(self._head.c.session_id.in_(context_ids))
                    )
                )
                .scalars()
                .all()
            )
            for turn_id in turn_ids:
                await connection.execute(
                    delete(self._history).where(self._history.c.turn_id == turn_id)
                )
                await connection.execute(
                    delete(self._artifacts).where(self._artifacts.c.turn_id == turn_id)
                )
            await connection.execute(
                delete(self._head).where(self._head.c.session_id.in_(context_ids))
            )
            await connection.execute(
                delete(self._checkpoint).where(self._checkpoint.c.session_id.in_(context_ids))
            )
            await connection.execute(
                delete(self._conversation_inheritance).where(
                    self._conversation_inheritance.c.session_id.in_(context_ids)
                )
            )
            await self._delete_unreferenced_conversation_snapshots(connection)
            await connection.execute(
                delete(self._session_state).where(self._session_state.c.session_id.in_(context_ids))
            )
            await connection.execute(
                delete(self._goal_reviews).where(self._goal_reviews.c.session_id == session_id)
            )
        for turn_id in turn_ids:
            self._persisted_counts.pop(str(turn_id), None)
            self._terminal_turns.discard(str(turn_id))

    async def input_required_session_ids(self) -> list[str]:
        """Context ids whose persisted task is input-required, so the awaiting-input marker survives a restart."""
        await self._ensure_initialized()
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(select(self._head.c.session_id, self._head.c.status))
            ).all()
        contexts: list[str] = []
        for session_id, status in rows:
            try:
                state = str(json.loads(status).get("state", ""))
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue
            if state == TaskState.input_required.value and session_id:
                contexts.append(str(session_id))
        return contexts

    async def turn_ids_for_session(self, session_id: str) -> list[str]:
        """The ids of every task in a context — for replaying a session."""
        await self._ensure_initialized()
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(self._head.c.id).where(self._head.c.session_id == session_id)
                    )
                )
                .scalars()
                .all()
            )
        return list(rows)

    async def session_message_texts(self, session_id: str) -> list[str]:
        """Raw history JSON for every task in a context, used to find the uploads a session references."""
        await self._ensure_initialized()
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(self._history.c.message)
                        .select_from(
                            self._history.join(
                                self._head, self._history.c.turn_id == self._head.c.id
                            )
                        )
                        .where(self._head.c.session_id == session_id)
                    )
                )
                .scalars()
                .all()
            )
        return list(rows)

    async def any_history_references(self, needle: str) -> bool:
        """Whether any persisted history mentions `needle`, so a shared upload survives one session's deletion."""
        await self._ensure_initialized()
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(self._history.c.row_id)
                    .where(self._history.c.message.like(f"%{escaped}%", escape="\\"))
                    .limit(1)
                )
            ).first()
        return row is not None

    async def add_user_message(self, working_directory: str, message: str) -> None:
        """Store a user message in the project-scoped history."""
        await self._ensure_initialized()
        async with self._engine.begin() as connection:
            await connection.execute(
                self._user_messages.insert().values(
                    working_directory=working_directory,
                    message=message,
                )
            )

    async def get_user_messages(self, working_directory: str, limit: int = 100) -> list[str]:
        """Retrieve the most recent user messages for a project, newest first."""
        await self._ensure_initialized()
        async with self._engine.begin() as connection:
            result = await connection.execute(
                select(self._user_messages.c.message)
                .where(self._user_messages.c.working_directory == working_directory)
                .order_by(self._user_messages.c.created_at.desc())
                .limit(limit)
            )
        return [row[0] for row in result]
