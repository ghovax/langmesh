"""Per-agent background job runner. Each job is one asyncio task; a finished task is read directly."""

from __future__ import annotations

import asyncio
import contextvars
import weakref
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from langmesh.base.primitives.identifiers import new_id
from langmesh.base.primitives.serialization import compact
from langmesh.base.contracts.ports import JobStore, MemoryJobStore

STATUS_COMPLETED = "completed"
STATUS_DELIVERED = "delivered"


# Per-kind presentation: how a completed job is announced and how in-flight ones are grouped in the turn context.
BACKGROUND_PRESENTATION: dict[str, dict[str, Any]] = {
    "bash": {
        "active_context_key": "pending_bash_commands",
        "completed_event": "background_bash_completed",
        "include_result": False,
    },
    "search_web": {
        "active_context_key": "pending_web_searches",
        "completed_event": "background_web_search_completed",
        "include_result": False,
    },
}

# Identifier prefix per kind, so a job id is self-describing (e.g. ``bg-…``).
_KIND_IDENTIFIER_PREFIX: dict[str, str] = {
    "bash": "bg",
    "search_web": "search",
}


def background_completion_event(kind: str) -> str:
    return BACKGROUND_PRESENTATION.get(kind, {}).get("completed_event", f"{kind}_completed")


def background_include_result(kind: str) -> bool:
    return bool(BACKGROUND_PRESENTATION.get(kind, {}).get("include_result", True))


@dataclass
class _BackgroundJobRecord:
    identifier: str
    kind: str
    task: asyncio.Task
    cancel_callback: Callable[[], None] | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    tool_call_identifier: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    # A detached job outlives the turn that started it and Stop must not cancel it.
    detached: bool = False
    # Fires when the user pushes a still-blocking command to the background, releasing the inline settle wait.
    detach_requested: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class BackgroundCompletion:
    """A finished job, ready to be delivered to the model."""

    kind: str
    identifier: str
    result: str
    started_at: datetime
    completed_at: datetime
    tool_call_identifier: str


# Weak references to every live runner, so exit handlers can cancel outstanding work without owning the tasks.
_active_job_runners: weakref.WeakSet[BackgroundJobs] = weakref.WeakSet()


class BackgroundJobs:
    """One background-job runner, owned by a single agent runtime."""

    def __init__(
        self,
        session_id: str = "",
        agent_name: str = "",
        store: JobStore | None = None,
        note_state_changed: Callable[[], None] | None = None,
    ) -> None:
        self._jobs: dict[str, _BackgroundJobRecord] = {}
        self._pending_deliveries: set[str] = set()
        # Identity for the durable mirror; without a context, durability is simply skipped.
        self._session_id = session_id
        self._agent_name = agent_name
        # Where durability goes, supplied rather than found, so a library session writes no database of its own.
        self._store: JobStore = store if store is not None else MemoryJobStore()
        self._note_state_changed = note_state_changed or (lambda: None)
        _active_job_runners.add(self)

    @property
    def store(self) -> JobStore:
        """The durable record behind this runner, for the tools that must write to it too."""
        return self._store

    def spawn(
        self,
        kind: str,
        coroutine: Coroutine[Any, Any, str],
        *,
        identifier: str | None = None,
        cancel_callback: Callable[[], None] | None = None,
        arguments: dict[str, Any] | None = None,
        tool_call_identifier: str = "",
        detached: bool = False,
    ) -> str:
        """Start `coroutine` as a background job and return its identifier."""
        if identifier is None:
            identifier = new_id(_KIND_IDENTIFIER_PREFIX.get(kind, kind))
        if self._session_id:
            try:
                recorded = self._store.record_started(
                    job_id=identifier,
                    session_id=self._session_id,
                    agent_name=self._agent_name,
                    kind=kind,
                    arguments=arguments or {},
                    tool_call_id=tool_call_identifier,
                )
                if not recorded:
                    raise ValueError(f"Background job {identifier!r} already exists.")
            except Exception:
                coroutine.close()
                raise
        task = asyncio.create_task(coroutine)
        self._jobs[identifier] = _BackgroundJobRecord(
            identifier=identifier,
            kind=kind,
            task=task,
            cancel_callback=cancel_callback,
            tool_call_identifier=tool_call_identifier,
            arguments=arguments or {},
            detached=detached,
        )
        # Persist the finished result the moment the task completes, so a restart sees it undelivered.
        task.add_done_callback(
            lambda _task, job_identifier=identifier: self._persist_completed(job_identifier)
        )
        return identifier

    def _persist_completed(self, identifier: str) -> None:
        record = self._jobs.get(identifier)
        if record is not None and self._session_id:
            self._store.record_completed(
                identifier, self._result_string(record), status=STATUS_COMPLETED
            )

    def bind_tool_call(self, identifier: str, tool_call_identifier: str) -> None:
        """Correlate a job with the tool call that started it, so its completion can reference that call."""
        record = self._jobs.get(identifier)
        if record is not None:
            record.tool_call_identifier = tool_call_identifier

    def add_done_callback(self, identifier: str, callback: Callable[[str], None]) -> bool:
        """Attach a side-effect callback that fires with the job identifier when its task finishes."""
        record = self._jobs.get(identifier)
        if record is None:
            return False
        record.task.add_done_callback(
            lambda _task, job_identifier=identifier: callback(job_identifier)
        )
        return True

    def has_pending(self) -> bool:
        """True while any job has not yet been drained (running or finished-but-undelivered)."""
        return bool(self._jobs)

    def has_completed_undelivered(self) -> bool:
        """True when a job has finished but its result has not been drained, so an autonomous wake has something to deliver."""
        return any(record.task.done() for record in self._jobs.values())

    def active_count(self) -> int:
        return sum(1 for record in self._jobs.values() if not record.task.done())

    def active_snapshots(self) -> list[dict[str, Any]]:
        """Current in-memory jobs, for UI status surfaces."""
        snapshots: list[dict[str, Any]] = []
        for record in self._jobs.values():
            if record.task.done():
                continue
            snapshots.append(
                {
                    "job_id": record.identifier,
                    "kind": record.kind,
                    "tool_call_id": record.tool_call_identifier,
                    "arguments": dict(record.arguments),
                    "started_at": record.started_at.isoformat(),
                    "detached": record.detached,
                }
            )
        return snapshots

    def active_by_context_key(self) -> dict[str, list[str]]:
        """In-flight job identifiers grouped by their turn-context key, with empty groups omitted."""
        grouped: dict[str, list[str]] = {}
        for record in self._jobs.values():
            if record.task.done():
                continue
            context_key = BACKGROUND_PRESENTATION.get(record.kind, {}).get(
                "active_context_key", f"pending_{record.kind}"
            )
            grouped.setdefault(context_key, []).append(record.identifier)
        return grouped

    async def wait_for_completion(
        self,
        *wake_events: asyncio.Event,
        timeout: float | None = None,
    ) -> None:
        """Block until at least one pending job finishes or a wake event fires."""
        if not self._jobs:
            return
        waiters = [
            asyncio.ensure_future(asyncio.shield(record.task)) for record in self._jobs.values()
        ]
        for wake_event in wake_events:
            waiters.append(asyncio.ensure_future(wake_event.wait()))
        try:
            await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()

    async def settle_inline(self, identifier: str, timeout: float) -> BackgroundCompletion | None:
        """Give a just-spawned job a brief window to finish inline, draining and returning it if it does."""
        record = self._jobs.get(identifier)
        if record is None:
            return None
        # Race the job against the ceiling and a manual background request, whichever fires first.
        task_waiter = asyncio.ensure_future(asyncio.shield(record.task))
        detach_waiter = asyncio.ensure_future(record.detach_requested.wait())
        try:
            await asyncio.wait(
                {task_waiter, detach_waiter},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            detach_waiter.cancel()
            if not task_waiter.done():
                task_waiter.cancel()
            elif not task_waiter.cancelled():
                task_waiter.exception()
        if record.detach_requested.is_set():
            # Backgrounded by the user: mark it detached so a Stop leaves it running, and hand back the started placeholder.
            record.detached = True
            return None
        if not record.task.done():
            return None
        self._jobs.pop(identifier, None)
        self._stage_delivery(identifier)
        return self._build_completion(record)

    def drain_completed(self) -> list[BackgroundCompletion]:
        """Remove and return every finished job, read straight off the finished tasks."""
        completions: list[BackgroundCompletion] = []
        for identifier, record in list(self._jobs.items()):
            if not record.task.done():
                continue
            self._jobs.pop(identifier, None)
            self._stage_delivery(identifier)
            completions.append(self._build_completion(record))
        return completions

    @property
    def pending_deliveries(self) -> tuple[str, ...]:
        """The results represented in memory but not yet acknowledged in their durable store."""
        return tuple(sorted(self._pending_deliveries))

    def stage_delivery(self, identifier: str) -> None:
        """Defer a restored result's acknowledgement until its conversation checkpoint commits."""
        self._stage_delivery(identifier)

    def _stage_delivery(self, identifier: str) -> None:
        if identifier not in self._pending_deliveries:
            self._pending_deliveries.add(identifier)
            self._note_state_changed()

    def acknowledge_deliveries(self) -> None:
        """Mark only results whose model-visible conversation checkpoint has committed."""
        acknowledged = bool(self._pending_deliveries)
        for identifier in tuple(self._pending_deliveries):
            if self._session_id:
                self._store.mark_delivered(identifier)
            self._pending_deliveries.discard(identifier)
        if acknowledged:
            self._note_state_changed()

    def cancel_all(self) -> None:
        for record in list(self._jobs.values()):
            if record.cancel_callback is not None:
                try:
                    record.cancel_callback()
                except Exception:
                    pass
            record.task.cancel()
        self._jobs.clear()

    def cancel_foreground(self) -> None:
        """Cancel only jobs tied to the current turn, leaving detached ones running."""
        for identifier, record in list(self._jobs.items()):
            if record.detached:
                continue
            if record.cancel_callback is not None:
                try:
                    record.cancel_callback()
                except Exception:
                    pass
            record.task.cancel()
            if self._session_id:
                result = compact({"code": f"{record.kind}_cancelled", "job_id": record.identifier})
                self._store.record_completed(identifier, result, status=STATUS_DELIVERED)
            self._jobs.pop(identifier, None)

    def cancel_by_tool_call(self, tool_call_identifier: str) -> bool:
        """Cancel the live background job associated with a tool card."""
        for identifier, record in list(self._jobs.items()):
            if record.tool_call_identifier != tool_call_identifier:
                continue
            if record.task.done():
                return False
            if record.cancel_callback is not None:
                try:
                    record.cancel_callback()
                except Exception:
                    pass
            record.task.cancel()
            if self._session_id:
                result = compact({"code": f"{record.kind}_cancelled", "job_id": record.identifier})
                self._store.record_completed(identifier, result, status=STATUS_DELIVERED)
            self._jobs.pop(identifier, None)
            return True
        return False

    def cancel_by_identifier(self, identifier: str, *, kind: str | None = None) -> bool:
        """Cancel one live job by its handle, optionally constrained by kind so one class cannot cancel another."""
        record = self._jobs.get(identifier)
        if record is None or record.task.done() or (kind is not None and record.kind != kind):
            return False
        if record.cancel_callback is not None:
            try:
                record.cancel_callback()
            except Exception:
                pass
        record.task.cancel()
        if self._session_id:
            result = compact({"code": f"{record.kind}_cancelled", "job_id": record.identifier})
            self._store.record_completed(identifier, result, status=STATUS_DELIVERED)
        self._jobs.pop(identifier, None)
        return True

    def request_background(self, tool_call_identifier: str) -> bool:
        """Push a still-running foreground job to the background on the user's behalf."""
        for record in self._jobs.values():
            if record.tool_call_identifier != tool_call_identifier:
                continue
            if record.task.done() or record.detached or record.detach_requested.is_set():
                return False
            record.detach_requested.set()
            return True
        return False

    def _result_string(self, record: _BackgroundJobRecord) -> str:
        """The finished task's result as a string, with a cancelled or failed job becoming an error payload."""
        try:
            result: Any = record.task.result()
        except asyncio.CancelledError:
            result = compact({"code": f"{record.kind}_cancelled", "job_id": record.identifier})
        except Exception as exception:
            result = compact(
                {
                    "code": f"{record.kind}_error",
                    "job_id": record.identifier,
                    "message": str(exception),
                }
            )
        if not isinstance(result, str):
            result = compact(result)
        return result

    def _build_completion(self, record: _BackgroundJobRecord) -> BackgroundCompletion:
        return BackgroundCompletion(
            kind=record.kind,
            identifier=record.identifier,
            result=self._result_string(record),
            started_at=record.started_at,
            completed_at=datetime.now(timezone.utc),
            tool_call_identifier=record.tool_call_identifier,
        )


def cancel_all_background_jobs() -> None:
    """Cancel outstanding jobs across every live runner, used only by the exit and signal handlers."""
    for runner in list(_active_job_runners):
        runner.cancel_all()


# Bind the current runner during dispatch, since background-producing tools cannot take it as a parameter.

_current_background_jobs: contextvars.ContextVar[BackgroundJobs | None] = contextvars.ContextVar(
    "current_background_jobs", default=None
)


def bind_background_jobs(jobs: BackgroundJobs) -> contextvars.Token:
    return _current_background_jobs.set(jobs)


def unbind_background_jobs(token: contextvars.Token) -> None:
    _current_background_jobs.reset(token)


def current_background_jobs() -> BackgroundJobs:
    jobs = _current_background_jobs.get()
    if jobs is None:
        raise RuntimeError("No background job runner is bound to the current context.")
    return jobs


# The tool call currently executing, so a spawned job is correlated with it from the start.
_current_tool_call_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_tool_call_id", default=""
)


def bind_tool_call_id(tool_call_identifier: str) -> contextvars.Token:
    return _current_tool_call_id.set(tool_call_identifier)


def unbind_tool_call_id(token: contextvars.Token) -> None:
    _current_tool_call_id.reset(token)


def current_tool_call_id() -> str:
    return _current_tool_call_id.get()


#: Set by whoever hosts sessions, so a tool child's process group can be attributed back to its session.
note_child_group = None


def record_child_group(session_id: str, group: int) -> None:
    """Tell the host which group a session's tool child leads, if anything is listening."""
    if note_child_group is not None and session_id and group:
        note_child_group(session_id, group)
