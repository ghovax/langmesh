"""One session's executor: the process-local half of a session that is alive."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine, Literal, Optional, cast

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers.request_handler import RequestHandler
from a2a.server.tasks import TaskUpdater
from a2a.types import DataPart, Message, MessageSendParams, Part, Role, Task, TaskState, TextPart
from langchain_core.messages import messages_from_dict

from langmeshd.worker import features_access as _features
from langmeshd.commons.agent_files import AgentFileLoader, list_agents
from langmeshd.commons.configuration_locations import agent_directories
from langmeshd.daemon.machine import machine_catalogue
from langmeshd.commons.toolboxes import toolbox_for
from langmesh.base.primitives.limits import current_limits
from langmeshd.daemon.persistence.file_leases import FileLeaseManager
from langmesh.base.configuration import Configuration
from langmeshd.daemon.persistence.background_jobs import get_background_job_store
from langmesh.base.contracts.ports import JobStore
from langmeshd.daemon.persistence.worktrees import SessionWorktree
from langmesh.protocol.metadata import (
    AUTONOMOUS_RESUME_KIND,
    COMPACTION_KIND,
    COMPACTION_PREPARE_KIND,
    COMPACTION_RESUME_KIND,
    GOAL_CONTINUATION_KIND,
    TASK_CONTINUATION_KIND,
    REPORT_REMINDER_KIND,
    RETRY_TURN_KIND,
    INPUT_RESPONSE_KIND,
    Metadata,
    PART_KIND,
    envelope_part,
    part_payload,
    turn_metadata_envelope,
)
from langmesh.protocol.events import StatusEvent
from langmesh.protocol.parts import _event_part
from langmesh.protocol.turn_record import PendingInteraction, ToolGate, TurnRecord
from langmesh.runtime.plugins.goal_review.goal import Goal, GoalReviewPhase
from langmesh.runtime.composition import RuntimeComponents, RuntimeProfile
from langmesh.runtime.runtime import AgentRuntime
from langmesh.runtime.environment import RuntimeEnvironment
from langmesh.runtime.session_control import SessionSnapshot
from langmesh.runtime.features import LocationsCapability
from langmesh.runtime.turn_events import SuspensionGate
from langmesh.runtime.values import PermissionAnswer
from langmeshd.worker.host import HostServices, NullHostServices
from langmeshd.worker.peers import PeerSessions
from langmeshd.worker.turn import _ContextState, _ContinuationPlan, _TurnRunner
from langmeshd.worker.turn_store import HostTurnStore
from langmeshd.daemon.persistence.credentials import file_credential_store
from langmesh.base.primitives.serialization import compact

logger = logging.getLogger(__name__)

# The settings-gated built-ins: only reachable when the machine actually has them.
_MCP_TOOL_NAMES = frozenset(
    {"list_mcp_tools", "call_mcp_server_tool", "list_mcp_resources", "read_mcp_resource"}
)
_REMOTE_TOOL_NAMES = frozenset({"list_remote_agents", "message_remote_agent"})


def _installed_agent_names(
    global_configuration: Configuration, working_directory: str
) -> list[str]:
    """The profiles a peer could be created with, read at build time so a bad name is unrepresentable."""
    directories = agent_directories(working_directory)
    try:
        return [entry["id"] for entry in list_agents(directories)]
    except Exception:  # noqa: BLE001 — an unreadable profile directory must not fail the runtime
        return []


def _compose_session_tools(
    configuration: Any,
    global_configuration: Configuration,
    working_directory: str = "",
    *,
    can_reach_peers: bool = False,
    permission_mode: Any = None,
    plugin_tools: dict[str, Any] | None = None,
) -> list[Any]:
    """The host's toolset for one session: the agent profile's declared built-ins, gated by
    what the machine actually has, plus the peer and remote tools the host owns. Nothing is
    forced — an agent that declares no tools runs with none. Plugin-owned tools resolve by
    name from the contributed map, so the core never names a plugin."""
    from langmesh.runtime.tools import registry
    from langmesh.runtime.tools.sessions import remote_agent_tools, session_tools

    tools = []
    for name in configuration.tools_enabled:
        schema = getattr(registry, name, None)
        if schema is None or not hasattr(schema, "name"):
            schema = (plugin_tools or {}).get(name)
        if schema is None or not hasattr(schema, "name"):
            continue
        if name == "ask_user" and not (
            permission_mode is None or cast(Any, permission_mode).asks or permission_mode == "allow"
        ):
            continue
        if name in _MCP_TOOL_NAMES and not global_configuration.mcp.enabled_servers():
            continue
        if name in _REMOTE_TOOL_NAMES and not global_configuration.remote_agents.agents:
            continue
        tools.append(schema)
    if can_reach_peers:
        tools.extend(session_tools(_installed_agent_names(global_configuration, working_directory)))
        if global_configuration.remote_agents.agents:
            tools.extend(remote_agent_tools())
    return tools


class SessionExecutor(AgentExecutor):
    """The live half of one session. The machinery still speaks in contexts, but a worker only ever has one."""

    def __init__(
        self,
        *,
        session_id: str,
        agent_name: str,
        working_directory: str,
        permission_mode: str,
        global_configuration: Configuration,
        sandbox: Optional[dict] = None,
        runtime_working_directory: str = "",
        workspace_id: str = "",
        parent: str = "",
        token: str = "",
        job_store: Optional[JobStore] = None,
        host: Optional[Any] = None,
        feature_factory: Optional[Any] = None,
    ):
        self._session_id = session_id
        self._agent_name = agent_name
        # The host services port: what a session needs from the process hosting it. The host
        # injects its implementation; a library embedding gets the null one.
        self._host: HostServices = host if host is not None else NullHostServices()
        # The host's feature composer: which plugins a session runs is the caller's decision,
        # never a library default. None means no features.
        self._feature_factory = feature_factory
        # A worker is a process a restart happens to, so its jobs want the durable store. Injectable all the same.
        self._job_store: JobStore = (
            job_store if job_store is not None else cast(JobStore, get_background_job_store())
        )
        self._working_directory = working_directory
        # Where tools actually run, resolved by the host: a worktree workspace is not the project directory.
        self._runtime_working_directory = runtime_working_directory or working_directory
        self._permission_mode = permission_mode
        # Resolved and clamped by the host before this worker existed. The worker never widens it.
        from langmesh.base.confinement import Profile

        self._sandbox = Profile.from_dict(sandbox)
        self._workspace_id = workspace_id
        self._parent = parent
        self._token = token
        self._global_configuration = global_configuration

        # The worker never opens the database: every write goes to the host, which is the sole writer.
        self._turn_store = HostTurnStore(session_id, host=self._host)

        # The same host for composing with other sessions, carrying this session's identity.
        self._peers = PeerSessions(
            session_id=session_id,
            working_directory=runtime_working_directory or working_directory,
            permission_mode=permission_mode,
            parent_session=parent,
            host=self._host,
        )

        # A2A needs a handler to drive turns; a worker serves one session, so it builds its own.
        self._registry = None
        self._handler = None

        self._on_turn_state = self._notify_turn_state
        self._on_permission_state = self._notify_permission_state
        # Structural parts take the ordinary event path; model deltas have a zero-await direct lane below.
        self._on_stream_event = self._publish_stream_event
        self._on_stream_delta = self._publish_stream_delta
        # Advisory locks so two sessions editing the same file notice each other.
        self._file_lease_manager = FileLeaseManager()

        self._contexts: dict[str, _ContextState] = {}
        # Maps an in-flight A2A task to its runtime, purely so `cancel` can abort it.
        self._aborts: dict[str, AgentRuntime] = {}
        # One session, one conversation. The map has one entry, and exists because the machinery indexes it.
        self._conversations: dict[str, list] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        # The background task that names the session after its first message, once.
        self._title_task: Optional[asyncio.Task] = None
        # The report reminder fires at most once for a session's whole life.
        self._nudged_to_report = False
        # This session's own MCP server connections, and the task connecting them.
        self._mcp_server_manager = None
        self._mcp_connect: Optional[asyncio.Task] = None
        self._goal_state_tail: Optional[asyncio.Task] = None
        self._session_state_lock = asyncio.Lock()
        # Linearizes external send decisions. Without this, two concurrent callers can both observe "idle" and start separate turns before either marks the context running.
        self._send_lock = asyncio.Lock()
        self._observation_registry_metadata: dict[str, Any] = {}
        self._observation_registry_error: str | None = None

    def _spawn_background(
        self, coroutine: Coroutine[Any, Any, Any], *, name: str
    ) -> asyncio.Task[Any]:
        """Start session-owned work and retain it until completion or shutdown."""
        task = asyncio.create_task(coroutine, name=f"langmesh:{self._session_id}:{name}")
        self._background_tasks.add(task)
        task.add_done_callback(self._finish_background)
        return task

    def _finish_background(self, task: asyncio.Task[Any]) -> None:
        """Retire completed session work and surface an otherwise unobserved failure."""
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "session background task %s failed",
                task.get_name(),
                exc_info=(type(error), error, error.__traceback__),
            )

    def _publish_stream_event(self, session_id: str, part) -> None:
        """Publish one structural part directly to the in-process live bus."""
        payload = part.model_dump(by_alias=True, exclude_none=True, mode="json")
        self._host.publish_part(session_id, payload)

    def _publish_stream_delta(
        self, session_id: str, channel: str, block_id: str, text: str
    ) -> None:
        """Publish a model delta synchronously: no task allocation, await or persistence in the hot lane."""
        self._host.publish_delta(session_id, channel, block_id, text)

    def _notify_turn_state(self, session_id: str, running: bool, turn_id: str = "") -> None:
        """Advance activity and the live bus in the calling turn, before any later delta can race it."""
        self._host.set_turn_state(session_id, running, self._has_live_background_work())

    def _begin_live_turn(self, session_id: str, turn_id: str) -> None:
        self._host.begin_turn(session_id, turn_id)

    def _end_live_turn(self, session_id: str, turn_id: str) -> None:
        self._host.end_turn(session_id, turn_id)

    async def _settle_turn_state(self) -> None:
        """Turn state publication is synchronous, so it is already settled."""
        return None

    def _has_live_background_work(self) -> bool:
        """Whether any runtime still has background work, including results that finished but never reached the model."""
        for context in self._contexts.values():
            runtime = getattr(context, "runtime", None)
            if runtime is None:
                continue
            if _features.has_pending_jobs(runtime) or _features.has_completed_undelivered_jobs(
                runtime
            ):
                return True
        return False

    def _notify_permission_state(self, session_id: str, awaiting: bool) -> None:
        """Tell the host this session is parked on a human, so `ps` shows waiting rather than working."""
        self._spawn_background(
            self._turn_store.publish_event({"session_id": session_id, "awaiting_input": awaiting}),
            name="permission-state",
        )

    async def compact_context(self, session_id: str) -> dict:
        """Compact a context's conversation and return the completed pass rather than detaching it."""
        if self._agent_handler() is None:
            return {"compacted": False}
        async with self._send_lock:
            try:
                state = self._contexts.get(session_id)
                runtime = state.runtime if state is not None else None
                if runtime is None:
                    runtime = await self._runtime_for(session_id, self._workspace())
                elif state is not None and state.running:
                    # Wait for the in-flight turn's teardown (which releases this lock) before mutating the shared runtime.
                    async with state.lock:
                        pass
                    state = self._contexts.get(session_id)
                    runtime = state.runtime if state is not None else None
                    if runtime is None:
                        runtime = await self._runtime_for(session_id, self._workspace())
                retry_operation = _features.retry_compaction(runtime)
                if retry_operation == "prepare":
                    should_resume = _features.resumes_after_compaction(runtime)
                    result = await self._drive_self_sent_turn(
                        session_id,
                        COMPACTION_RESUME_KIND if should_resume else COMPACTION_PREPARE_KIND,
                        metadata_flags={
                            Metadata.COMPACTION_RESUME
                            if should_resume
                            else Metadata.COMPACTION_PREPARE: True
                        },
                        result_event_kind="compaction",
                    )
                    return {"compacted": result is not None, **(result or {})}
                if retry_operation == "compact":
                    should_resume = _features.resumes_after_compaction(runtime)
                    result = await self._run_maintenance_turn(session_id)
                    if should_resume and result and result.get("ok") is not False:
                        await self._drive_self_sent_turn(
                            session_id,
                            COMPACTION_RESUME_KIND,
                            metadata_flags={Metadata.COMPACTION_RESUME: True},
                        )
                    return {"compacted": result is not None, **(result or {})}
                if _features.awaiting_compaction_recording(runtime):
                    result = await self._drive_self_sent_turn(
                        session_id,
                        COMPACTION_PREPARE_KIND,
                        metadata_flags={Metadata.COMPACTION_PREPARE: True},
                        result_event_kind="compaction",
                    )
                    return {"compacted": result is not None, **(result or {})}
                if not _features.compaction_failure(
                    runtime
                ) and _features.begin_compaction_preparation(runtime):
                    result = await self._drive_self_sent_turn(
                        session_id,
                        COMPACTION_PREPARE_KIND,
                        metadata_flags={Metadata.COMPACTION_PREPARE: True},
                        result_event_kind="compaction",
                    )
                    return {"compacted": result is not None, **(result or {})}
                should_resume = _features.resumes_after_compaction(runtime)
                result = await self._run_maintenance_turn(session_id)
                if should_resume and result and result.get("ok") is not False:
                    await self._drive_self_sent_turn(
                        session_id,
                        COMPACTION_RESUME_KIND,
                        metadata_flags={Metadata.COMPACTION_RESUME: True},
                    )
                return {"compacted": result is not None, **(result or {})}
            finally:
                await self._settle_turn_state()

    async def retry_turn(self, session_id: str) -> dict:
        """Continue the durable failed turn without creating or replaying a user message."""
        if self._agent_handler() is None:
            return {"retried": False, "reason": "worker_unavailable"}
        async with self._send_lock:
            try:
                state = self._contexts.get(session_id)
                runtime = state.runtime if state is not None else None
                if runtime is None:
                    runtime = await self._runtime_for(session_id, self._workspace())
                if _features.compaction_failure(runtime):
                    return {"retried": False, "reason": "compaction_required"}
                if not runtime.begin_turn_retry():
                    return {"retried": False, "reason": "not_retryable"}
                result = await self._drive_self_sent_turn(
                    session_id,
                    RETRY_TURN_KIND,
                    metadata_flags={Metadata.RETRY_TURN: True},
                )
                return {"retried": True, **(result or {})}
            finally:
                await self._settle_turn_state()

    def _agent_handler(self) -> Optional[RequestHandler]:
        """The handler that drives this session's turns, built once: there is no registry to look one up in."""
        if self._handler is None:
            from a2a.server.request_handlers import DefaultRequestHandler

            # `task_store` is a2a's keyword, not ours — the object it takes is our turn store.
            self._handler = DefaultRequestHandler(agent_executor=self, task_store=self._turn_store)
        return self._handler

    async def _drive_self_sent_turn(
        self,
        session_id: str,
        envelope_kind: str,
        *,
        metadata_flags: dict,
        text: str = "",
        result_event_kind: str = "",
    ) -> dict | None:
        """Drive one harness-initiated turn as a self-sent message, so it is real, persisted and replayable."""
        handler = self._agent_handler()
        if handler is None:
            return None
        # A wake carries nothing, but prose written for the session rides as a message a transcript can draw.
        message = Message(
            role=Role.user if text else Role.agent,
            parts=([Part(root=TextPart(text=text))] if text else [])
            + [envelope_part(envelope_kind)],
            message_id=uuid.uuid4().hex,
            context_id=session_id,
            metadata=turn_metadata_envelope(metadata_flags),
        )
        result = None
        async for event in handler.on_message_send_stream(MessageSendParams(message=message)):
            status_message = getattr(getattr(event, "status", None), "message", None)
            for part in getattr(status_message, "parts", None) or []:
                root = getattr(part, "root", part)
                payload = part_payload(root.data) if isinstance(root, DataPart) else {}
                if result_event_kind and payload.get(PART_KIND) == result_event_kind:
                    result = dict(payload)
        return result

    async def nudge_to_report(self, session_id: str) -> None:
        """Drive one turn saying the session has not answered yet. Once per session, and only the model can act on it."""
        if self._nudged_to_report:
            return
        self._nudged_to_report = True
        await self._drive_self_sent_turn(
            session_id,
            REPORT_REMINDER_KIND,
            metadata_flags={Metadata.REPORT_REMINDER: True},
        )

    def _arm_continuation(self, session_id: str, plan: _ContinuationPlan) -> None:
        """Queue independent obligations behind one serialized continuation workflow."""
        state = self._contexts.get(session_id)
        if state is None or state.aborted or not plan.any:
            return
        if state.continuation.enqueue(plan):
            self._notify_turn_state(session_id, True)
        if state.continuation.running:
            return
        workflow = asyncio.create_task(self._run_continuations(session_id))
        state.continuation.attach(workflow)
        workflow.add_done_callback(
            lambda completed: self._finish_continuation(session_id, completed)
        )

    async def _run_continuations(self, session_id: str) -> None:
        """Drain plans produced by completed turns; each plan opens at most one next turn."""
        while True:
            state = self._contexts.get(session_id)
            if state is None or state.aborted or not state.continuation.queued.any:
                return
            plan = state.continuation.take()
            await self._continue(session_id, plan)

    async def _cancel_continuation(self, session_id: str) -> bool:
        """A new outside turn supersedes queued automatic work and joins its owned workflow."""
        state = self._contexts.get(session_id)
        workflow = state.continuation.workflow if state is not None else None
        if state is not None and state.continuation.clear():
            self._notify_turn_state(session_id, False)
        if workflow is None or workflow.done() or workflow is asyncio.current_task():
            return False
        workflow.cancel()
        # The superseded review belongs to the old goal state: stop it rather than letting it run out a verdict nobody is waiting for.
        if state is not None:
            state.continuation.cancel_review()
        with contextlib.suppress(asyncio.CancelledError):
            await workflow
        if state is not None:
            state.continuation.detach(workflow)
        if state is not None and state.continuation.clear():
            self._notify_turn_state(session_id, False)
        return True

    def _finish_continuation(self, session_id: str, workflow: asyncio.Task) -> None:
        """Retire the owned workflow and report an unexpected failure."""
        state = self._contexts.get(session_id)
        owns_workflow = state is not None and state.continuation.detach(workflow)
        if owns_workflow and state is not None and state.continuation.clear():
            self._notify_turn_state(session_id, False)
        error = None if workflow.cancelled() else workflow.exception()
        if error is not None:
            logger.error(
                "continuation workflow failed",
                exc_info=(type(error), error, error.__traceback__),
            )
        self._maybe_evict(session_id)

    async def _continue(self, session_id: str, plan: _ContinuationPlan) -> None:
        """Review a goal first, then carry either or both obligations in one next turn."""
        state = self._contexts.get(session_id)
        runtime = state.runtime if state is not None else None
        if runtime is None:
            # The plan acquired an activity hold when it was queued, even if teardown won the race.
            self._notify_turn_state(session_id, False)
            return
        review_phase_active = False
        try:
            goal = _features.goal(runtime)
            if plan.goal and goal is not None and goal.is_open:
                if _features.goal_review_mode(runtime) == "self_managed":
                    # The simple goal mode: no reviewer. The session re-opens on the goal
                    # itself, and the agent owns it through the update_goal tool.
                    await self._drive_self_sent_turn(
                        session_id,
                        GOAL_CONTINUATION_KIND,
                        metadata_flags={
                            Metadata.GOAL_CONTINUATION: True,
                            **({Metadata.TASK_CONTINUATION: True} if plan.tasks else {}),
                        },
                        text=goal.text,
                    )
                else:
                    await self._continue_with_review(session_id, state, runtime, plan, goal)
            goal = _features.goal(runtime)
            if goal is not None and goal.is_open and goal.review_message:
                await self._drive_self_sent_turn(
                    session_id,
                    GOAL_CONTINUATION_KIND,
                    metadata_flags={
                        Metadata.GOAL_CONTINUATION: True,
                        Metadata.GOAL_REVIEW_ID: goal.review_id,
                        **({Metadata.TASK_CONTINUATION: True} if plan.tasks else {}),
                    },
                    text=goal.review_message,
                )
            elif plan.tasks:
                await self._drive_self_sent_turn(
                    session_id,
                    TASK_CONTINUATION_KIND,
                    metadata_flags={Metadata.TASK_CONTINUATION: True},
                )
        finally:
            if review_phase_active:
                self._notify_goal_state(session_id, _features.goal(runtime))
            # Exactly one release for the plan hold, whichever obligation opened the next turn.
            self._notify_turn_state(session_id, False)

    async def _continue_with_review(self, session_id, state, runtime, plan, goal) -> None:
        """Review an open goal first; a verdict that lands decides the next turn."""
        self._notify_goal_state(session_id, goal, review_phase=GoalReviewPhase.CHECKING)
        review = asyncio.create_task(cast(Coroutine[Any, Any, Any], _features.review_goal(runtime)))
        state.continuation.attach_review(review)
        try:
            verdict = await review
            _features.apply_goal_review(runtime, verdict)
            if verdict is None:
                # No verdict landed: the review failed or was cancelled, so an open goal
                # must not re-arm another review the moment this one ends. Park it and
                # wait for a person, exactly as a spent allowance would.
                _features.park_goal(runtime)
        except asyncio.CancelledError:
            # Clearing the goal cancels only its review; stopping cancels the owning workflow too.
            goal = _features.goal(runtime)
            if goal is not None and goal.status == Goal.CLEARED:
                pass
            else:
                raise
        except Exception:
            # A review that raised leaves the goal undecided: park it so the failure
            # cannot re-arm another review, and let the workflow end cleanly.
            logger.exception("goal review failed for %s", session_id)
            _features.park_goal(runtime)
        finally:
            state.continuation.detach_review(review)
            self._notify_goal_state(session_id, _features.goal(runtime))
        # Written before the turn opens: a verdict held only in memory is one a restart would lose.
        await self._persist_session_state(session_id, runtime)

    async def clear_goal(self, session_id: str) -> bool:
        """The person calling the goal off: a live one is stopped and kept, a resolved one is dropped from view."""
        state = self._contexts.get(session_id)
        runtime = state.runtime if state is not None else None
        if runtime is None:
            # A woken session has no runtime until its first turn, but the goal is durable beside the checkpoint. Build the runtime so calling the goal off also works from the parked/asleep state, instead of silently answering "nothing to clear".
            runtime = await self._runtime_for(session_id, self._workspace())
        goal = _features.goal(runtime) if runtime is not None else None
        if runtime is None or goal is None:
            return False
        if state is not None:
            state.continuation.cancel_review()
        _features.write_goal(
            runtime,
            goal.updated(status=Goal.CLEARED, review_message=None, review_id=None)
            if goal.is_open
            else None,
        )
        self._spawn_background(self._persist_session_state(session_id, runtime), name="clear-goal")
        return True

    async def _persist_session_state(self, session_id: str, runtime: AgentRuntime) -> None:
        """Write the durable session state outside a turn, since calling off a goal changes it between turns."""
        async with self._session_state_lock:
            snapshot = runtime.dirty_session_snapshot()
            if snapshot is None:
                return
            revision = runtime.session_revision
            try:
                await self._turn_store.save_session_state(session_id, snapshot)
            except Exception:  # noqa: BLE001 — the goal is already off in the live session
                logger.exception("could not persist the session state for %s", session_id)
                return
            runtime.clear_session_dirty(revision)

    async def _persist_turn_checkpoint(
        self,
        session_id: str,
        turn_id: str,
        messages: list[dict],
        inherited_snapshot_id: str,
        runtime: Optional[AgentRuntime],
    ) -> None:
        """Persist a turn checkpoint and its matching session revision under the shared state lock."""
        async with self._session_state_lock:
            session_state = runtime.dirty_session_snapshot() if runtime is not None else None
            revision = (
                runtime.session_revision
                if runtime is not None and session_state is not None
                else None
            )
            await self._turn_store.save_turn_state(
                session_id,
                turn_id,
                messages,
                session_state,
                inherited_snapshot_id,
            )
            if runtime is not None and revision is not None:
                runtime.clear_session_dirty(revision)

    def _notify_goal_state(
        self, session_id: str, goal, review_phase: Optional[GoalReviewPhase] = None
    ) -> None:
        """Tell the host the goal and its transient pre-continuation phase."""
        previous = self._goal_state_tail

        async def publish() -> None:
            if previous is not None:
                await asyncio.gather(previous, return_exceptions=True)
            await self._turn_store.publish_event(
                {
                    "session_id": session_id,
                    "goal": {
                        **goal.public(),
                        **(
                            {"review_phase": review_phase.value} if review_phase is not None else {}
                        ),
                    }
                    if goal is not None
                    else None,
                }
            )

        task = asyncio.create_task(publish())
        self._goal_state_tail = task
        task.add_done_callback(self._finish_goal_state_publish)

    def _finish_goal_state_publish(self, task: asyncio.Task) -> None:
        """Retire the ordered publisher and surface the last failed update."""
        if self._goal_state_tail is task:
            self._goal_state_tail = None
        error = None if task.cancelled() else task.exception()
        if error is not None:
            logger.error(
                "could not publish goal state",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _run_maintenance_turn(self, session_id: str) -> dict | None:
        """Drive one manual-maintenance turn, so it is persisted and replayable like any other."""
        result = await self._drive_self_sent_turn(
            session_id,
            COMPACTION_KIND,
            metadata_flags={Metadata.COMPACTION: True},
            result_event_kind="compaction",
        )
        return result if result and result.get("status") == "done" else None

    def abort_context(self, session_id: str) -> bool:
        # Stop is broadcast to every executor, but only the one holding this context has anything to stop.
        state = self._contexts.get(session_id)
        if state is None or (state.runtime is None and state.resume_pump is None):
            return False
        # Mark Stopped first, so no later completion can re-arm a pump that would wake the agent again.
        state.aborted = True
        handled = False
        if state.continuation.workflow is not None:
            if not state.continuation.workflow.done():
                state.continuation.workflow.cancel()
            handled = True
        # A stop is a halt for the review too: cancel it outright rather than waiting for the reviewer's current model call to finish, so the linked review session stops now.
        if state.continuation.cancel_review():
            handled = True
        if state.continuation.clear():
            self._notify_turn_state(session_id, False)
            handled = True
        if state.resume_pump is not None:
            if not state.resume_pump.done():
                state.resume_pump.cancel()
            state.resume_pump = None
            handled = True
        if state.runtime is not None:
            state.runtime.abort()
            goal = _features.goal(state.runtime)
            if goal is not None and goal.is_open:
                _features.park_goal(state.runtime)
                self._spawn_background(
                    self._persist_session_state(session_id, state.runtime), name="park-goal"
                )
            handled = True
        return handled

    def abort_tool(self, session_id: str, tool_call_identifier: str) -> bool:
        state = self._contexts.get(session_id)
        if state is None or state.runtime is None:
            return False
        return state.runtime.abort_tool(tool_call_identifier)

    def send_tool_to_background(self, session_id: str, tool_call_identifier: str) -> bool:
        state = self._contexts.get(session_id)
        if state is None or state.runtime is None:
            return False
        return _features.send_tool_to_background(state.runtime, tool_call_identifier)

    def background_snapshots(self, session_id: str) -> list[dict]:
        state = self._contexts.get(session_id)
        if state is None or state.runtime is None:
            return []
        return _features.background_snapshots(state.runtime)

    def set_locations(self, locations: Optional[list[dict]]) -> int:
        """Adopt the workspace's environments after an edit, so a session already open sees the new set."""
        from langmeshd.features import attach_location_executors

        resolved_locations = attach_location_executors(locations)
        for state in self._contexts.values():
            if state.runtime is not None:
                # The locations plugin owns the map; a session without the plugin ignores this.
                capability = state.runtime.features.capability(LocationsCapability)
                if capability is not None:
                    capability.set_locations(resolved_locations)
        return len(resolved_locations or [])

    async def set_permission_mode(self, mode: str) -> str:
        """Adopt a new permission mode now rather than on the next turn, since the turn to reach is the running one."""
        from langmesh.base.configuration.permission_mode import PermissionMode

        resolved = PermissionMode.resolve(mode)
        self._permission_mode = str(resolved)
        # What a later runtime starts from, and what this session asks for when it creates a peer.
        self._peers.permission_mode = self._permission_mode
        for state in self._contexts.values():
            if state.runtime is not None:
                state.runtime.set_permission_mode(resolved)
        await self._reconsider_parked_gates()
        return self._permission_mode

    async def _reconsider_parked_gates(self) -> None:
        """Re-decide the approvals this session is already stopped on, since changing the mode does not unask them."""
        records = await self._turn_store.control_records_for_session(self._session_id)
        for _turn_id, record in records:
            pending = record.pending
            if pending is None or not pending.gates:
                continue
            state = self._contexts.get(self._session_id)
            runtime = state.runtime if state is not None else None
            if runtime is None:
                # No live runtime: the record carries the new mode, so the gate is re-decided on resume.
                continue
            for gate in list(pending.gates):
                if gate.request_id in pending.answers:
                    continue
                verdict = await cast(
                    Awaitable[dict[str, Any]], _features.reconsider_gate(runtime, gate)
                )
                if not verdict:
                    continue
                await self.resolve_pending_input({"request_id": gate.request_id, **verdict})

    def reset_runtimes(self) -> None:
        """Drop cached runtimes so the next turn rebuilds them, deferring any context with work in flight."""
        for session_id, state in list(self._contexts.items()):
            if state.runtime is not None:
                state.pending_reset = True
                self._maybe_evict(session_id)

    def _record_pending_answer(self, task, payload: dict) -> Optional[tuple[dict, dict]]:
        """Record one answer durably and report whether the batch is now fully answered and ready to resume."""
        record = TurnRecord.from_metadata(task.metadata)
        pending = record.pending
        if pending is None:
            return None
        request_id = str(payload.get("request_id", ""))
        gate = pending.gate_for(request_id)
        if gate is None:
            return None
        if gate.is_question:
            pending.answers[request_id] = (
                {
                    "__declined__": True,
                    "__reason__": str(payload.get("reason", "") or ""),
                    "__actor__": str(payload.get("actor", "person") or "person"),
                }
                if payload.get("declined")
                else payload.get("answers", [])
            )
        else:
            decision = str(payload.get("decision", "deny"))
            reason = str(payload.get("reason", "") or "")
            if decision not in {"allow", "deny"}:
                reason = reason or "The response did not contain an explicit approval."
            actor = str(payload.get("actor", "person"))
            if actor not in {"person", "reviewer", "approver"}:
                actor = "person"
            pending.answers[request_id] = PermissionAnswer(
                allow=decision == "allow",
                reason=reason,
                actor=cast(Literal["person", "reviewer", "approver"], actor),
            ).model_dump()
        task.metadata = record.apply_to(task.metadata)
        if pending.fully_answered:
            return pending.plans, pending.answers
        return None

    def steer_context(self, session_id: str, message: str) -> bool:
        state = self._contexts.get(session_id)
        if state is None or not state.running or state.runtime is None:
            return False
        return state.runtime.enqueue_steering(message)

    def reset_runtime(self, session_id: str) -> None:
        """Drop one context's cached runtime so the next turn rebuilds it, deferred while work is in flight."""
        state = self._contexts.get(session_id)
        if state is None:
            return
        state.pending_reset = True
        self._maybe_evict(session_id)

    def _maybe_evict(self, session_id: str) -> None:
        """Apply a deferred reset once the runtime is idle, so evicting one never strands another session's result."""
        state = self._contexts.get(session_id)
        if state is None or not state.pending_reset:
            return
        continuation_running = state.continuation.running
        runtime_busy = state.runtime is not None and _features.has_pending_jobs(state.runtime)
        if state.running or continuation_running or runtime_busy:
            return
        state.runtime = None
        state.pending_reset = False

    def _context(self, session_id: str) -> _ContextState:
        """The per-context state, created on first access. Use `_contexts.get` when merely inspecting."""
        state = self._contexts.get(session_id)
        if state is None:
            state = _ContextState()
            self._contexts[session_id] = state
        return state

    def teardown_context(self, session_id: str, *, preserve_background_jobs: bool = False) -> None:
        """Release a context while optionally preserving interrupted jobs for startup recovery."""
        state = self._contexts.pop(session_id, None)
        if state is not None:
            if state.resume_pump is not None and not state.resume_pump.done():
                state.resume_pump.cancel()
            continuation_workflow = state.continuation.workflow
            if continuation_workflow is not None and not continuation_workflow.done():
                continuation_workflow.cancel()
            if state.continuation.clear():
                self._notify_turn_state(session_id, False)
            if state.runtime is not None:
                if preserve_background_jobs:
                    state.runtime.interrupt_for_restart()
                else:
                    state.runtime.abort()
        self._conversations.pop(session_id, None)

    def _arm_resume_pump(self, session_id: str, runtime: Optional[AgentRuntime] = None) -> None:
        """Ensure a resume pump watches this context while it has work in flight. Idempotent."""
        state = self._contexts.get(session_id)
        # A Stopped context stays quiet; pending work is replayed on the next user turn.
        if state is None or state.aborted:
            return
        runtime = runtime or state.runtime
        if runtime is None or not _features.has_pending_jobs(runtime):
            return
        if state.runtime is None:
            state.runtime = runtime
            state.pending_reset = True
        if state.resume_pump is not None and not state.resume_pump.done():
            return
        state.resume_pump = asyncio.create_task(self._resume_pump(session_id))

    async def _resume_pump(self, session_id: str) -> None:
        """Wait for each background result while the context is idle, driving a turn to deliver it, then retire."""
        try:
            while True:
                state = self._contexts.get(session_id)
                runtime = state.runtime if state is not None else None
                if runtime is None or not _features.has_pending_jobs(runtime):
                    return
                await _features.wait_for_jobs(runtime)
                # A result landed: drive a turn to deliver it. A concurrent user turn that drained it makes this a no-op.
                await self._run_autonomous_turn(session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("background-resume pump failed for context %s", session_id)
        finally:
            # Clear the slot only if it still points at this pump, so a freshly armed one is not dropped.
            state = self._contexts.get(session_id)
            if state is not None and state.resume_pump is asyncio.current_task():
                state.resume_pump = None
            # The context is idle, so a reset deferred while it had work in flight can finally take effect.
            self._maybe_evict(session_id)

    async def _run_autonomous_turn(self, session_id: str) -> None:
        """Start a turn the user did not, to deliver a finished background result, through the ordinary turn path."""
        if self._agent_handler() is None:
            return
        # Nothing left to deliver, so mint no task; the executor re-checks under the lock either way.
        state = self._contexts.get(session_id)
        runtime = state.runtime if state is not None else None
        has_live_result = runtime is not None and _features.has_completed_undelivered_jobs(runtime)
        has_stored_result = self._job_store.has_undelivered_jobs(session_id, self._agent_name)
        if not has_live_result and not has_stored_result:
            return
        if runtime is not None and _features.goal(runtime) is not None:
            self._notify_goal_state(session_id, _features.goal(runtime))
        # An agent-authored resume, so no consumer sees a user message the user never sent.
        await self._drive_self_sent_turn(
            session_id, AUTONOMOUS_RESUME_KIND, metadata_flags={Metadata.AUTONOMOUS_RESUME: True}
        )

    def _build_runtime(
        self,
        session_id: str,
        working_directory: str,
        project_directory: str,
        conversation: Optional[list] = None,
    ) -> AgentRuntime:
        # A worker serves a person's machine, so it gets the machine's catalogue; a library session gets less.
        catalogue = machine_catalogue(
            self._global_configuration, project_directory, agent_loader=AgentFileLoader()
        )
        configuration = catalogue.agent(self._agent_name)
        if configuration is None:
            raise FileNotFoundError(
                catalogue.prompt(
                    "agent_configuration_not_found",
                    {
                        "agent_name": self._agent_name,
                        "available_agents": compact(list(catalogue.agents())),
                    },
                )
            )
        runtime_directory = working_directory or project_directory or str(Path.cwd())
        # The host composes the session's tools: the agent profile's declared set, mapped onto the shipped built-ins, plus the settings-gated and peer tools it owns. The library forces nothing; this is the host assembling the toolset. Plugin-owned tools come from the plugins' own contribute_tools, keyed by name.
        composed = _compose_session_tools(
            configuration,
            self._global_configuration,
            working_directory,
            can_reach_peers=self._peers is not None,
            permission_mode=self._permission_mode,
            plugin_tools=self._host.plugin_tools(),
        )
        # The permission evaluator refuses what the profile did not declare, so the declared set is exactly the composed set.
        configuration = configuration.model_copy(
            update={"tools_enabled": sorted({tool.name for tool in composed})}
        )
        # The host's plugin bundle: which features run and the ports they need.
        bundle = self._compose_plugins(session_id, runtime_directory, configuration, catalogue)
        toolbox = toolbox_for(
            session_id, enabled=self._global_configuration.toolbox.enabled
        )
        if toolbox is not None:
            toolbox.prepare()
        runtime = AgentRuntime(
            RuntimeProfile(
                agent=configuration,
                configuration=self._global_configuration,
                session_id=session_id,
                working_directory=runtime_directory,
                project_directory=project_directory or runtime_directory,
                parent_session=self._parent,
                permission_mode=self._permission_mode,
                sandbox=self._sandbox,
            ),
            RuntimeComponents(
                catalogue=catalogue,
                file_leases=self._file_lease_manager,
                sessions=self._peers,
                mcp_servers=self._mcp_server_manager,
                jobs=self._job_store,
                toolset=composed,
                related_turns=self._build_turn_reader(),
                features=(bundle.get("features") or []),
                services=bundle.get("services"),
                toolbox=toolbox,
                # The host probes the machine and the user's context; the library never does.
                machine_snapshot=self._machine_snapshot(),
                user_context=self._user_context_snapshot(),
                environment=RuntimeEnvironment(credentials=file_credential_store()),
            ),
            conversation=conversation,
        )
        if self._observation_registry_metadata or self._observation_registry_error:
            _features.note_observation_registry(
                runtime,
                self._observation_registry_metadata,
                self._observation_registry_error,
            )
        return runtime

    def _machine_snapshot(self) -> dict:
        """The machine snapshot for this session, probed by the host and passed into the runtime."""
        from langmeshd.daemon.machine_environment import probe_local_environment

        import json as _json

        try:
            parsed = _json.loads(probe_local_environment())
        except (ValueError, TypeError):
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    def _user_context_snapshot(self) -> dict:
        """The user-context snapshot for this session, probed by the host when it is enabled."""
        user_context = getattr(self._global_configuration, "user_context", None)
        if user_context is None or not user_context.enabled:
            return {}
        from langmeshd.daemon.machine_environment import probe_user_context

        import json as _json

        try:
            parsed = _json.loads(probe_user_context(user_context.refresh_hours))
        except (ValueError, TypeError):
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    def _compose_plugins(self, session_id: str, runtime_directory: str, configuration, catalogue):
        """The session's plugin bundle (features and their ports), from the host's injected composer."""
        if self._feature_factory is None:
            return {}
        return self._feature_factory(
            session_id=session_id,
            runtime_directory=runtime_directory,
            configuration=configuration,
            catalogue=catalogue,
            job_store=self._job_store,
            goal_listener=lambda goal: self._notify_goal_state(session_id, goal),
            goal_review_journal=self._host.build_goal_review_journal(self._turn_store),
            global_configuration=self._global_configuration,
        )

    def _build_turn_reader(self):
        async def read_turn(turn_id: str):
            if not turn_id:
                return None
            task = await self._turn_store.get(turn_id)
            # No `history`: read_turn feeds a task into the caller's context, and a full transcript would overflow it.
            return (
                task.model_dump(by_alias=True, exclude_none=True, mode="json", exclude={"history"})
                if task
                else None
            )

        return read_turn

    async def _runtime_for(self, session_id: str, workspace: SessionWorktree) -> AgentRuntime:
        # Apply a reset deferred while work was in flight, so this turn rebuilds with the new configuration.
        self._maybe_evict(session_id)
        state = self._context(session_id)
        runtime = state.runtime
        if runtime is None:
            session_state = SessionSnapshot()
            # Restore a persisted conversation the first time a context is seen, so the agent resumes with its history.
            if session_id not in self._conversations:
                # The two independent rows are read together, so a cold turn pays one database round trip.
                checkpoint, session_state = await asyncio.gather(
                    self._turn_store.load_checkpoint(session_id),
                    self._turn_store.load_session_state(session_id),
                )
                state.inherited_snapshot_id = str(checkpoint.get("inherited_snapshot_id") or "")
                state.inherited_message_count = int(checkpoint.get("inherited_message_count") or 0)
                restored = messages_from_dict(checkpoint.get("messages") or [])
                if restored:
                    self._conversations[session_id] = restored
            else:
                session_state = await self._turn_store.load_session_state(session_id)
            # Bound to the process-wide history for this context, so a turn picks up where the last left off.
            conversation = self._conversations.setdefault(session_id, [])
            runtime = self._build_runtime(
                session_id,
                workspace.runtime_working_directory,
                workspace.source_working_directory,
                conversation=conversation,
            )
            # Restore the durable objective alongside the conversation, so a marathon run never loses what it was for.
            if session_state != SessionSnapshot():
                runtime.restore_session(session_state)
                # Announce the restored goal here: `restore_session` deliberately does not, being the write that changes nothing.
                self._notify_goal_state(session_id, _features.goal(runtime))
            state.runtime = runtime
            state.runtime_ready.set()
            # Replay background results the store holds but never delivered, so the model sees them at once.
            self._replay_stored_background_results(session_id, runtime)
        # A context's working directory is fixed at creation, so later turns never repoint it.
        return runtime

    def _replay_stored_background_results(self, session_id: str, runtime: AgentRuntime) -> None:
        store = self._job_store
        for job in store.undelivered_jobs(session_id, self._agent_name):
            _features.inject_stored_background_result(
                runtime,
                kind=job["kind"],
                identifier=job["job_id"],
                tool_call_identifier=job["tool_call_id"],
                result=job["result"] or "",
            )
            store.mark_delivered(job["job_id"])

    async def resume_pending_jobs(self) -> None:
        """Record this session's interrupted jobs and replay results its model never saw."""
        store = self._job_store
        for job in store.running_jobs(self._agent_name):
            if job["session_id"] != self._session_id:
                continue
            store.mark_abandoned(
                job["job_id"],
                compact(
                    {
                        "code": f"{job['kind']}_interrupted",
                        "job_id": job["job_id"],
                        "arguments": job["arguments"],
                    }
                ),
            )
        if not store.has_undelivered_jobs(self._session_id, self._agent_name):
            return
        self._spawn_background(
            self._run_autonomous_turn(self._session_id), name="resume-autonomous-turn"
        )

    def _workspace(self, requested_working_directory: str = "") -> SessionWorktree:
        """Where this session's work happens, resolved once by the host rather than renegotiated per turn."""
        source = requested_working_directory or self._working_directory or ""
        runtime = self._runtime_working_directory or source
        return SessionWorktree(
            source_working_directory=source,
            runtime_working_directory=runtime,
            strategy="worktree" if runtime and runtime != source else "none",
        )

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Run one turn. The per-turn state machine lives in :class:`_TurnRunner`, fresh for each request."""
        await _TurnRunner(self, context, event_queue).run()

    async def _suspend_durable_segment(
        self,
        task: Task,
        updater: TaskUpdater,
        interactions: list[SuspensionGate],
        plans: dict,
        save_conversation: Callable[[], Awaitable[None]],
    ) -> bool:
        """Close a turn's segment as a durable suspend, recording its gates and checkpoint into task metadata."""
        suspended = TurnRecord.from_metadata(task.metadata)
        suspended.pending = PendingInteraction(
            gates=[ToolGate.model_validate(dataclasses.asdict(gate)) for gate in interactions],
            plans=plans,
            agent=self._agent_name,
        )
        task.metadata = suspended.apply_to(task.metadata)
        if self._on_permission_state is not None:
            self._on_permission_state(task.context_id, True)
        await save_conversation()
        await self._turn_store.save(task)
        await updater.update_status(
            TaskState.input_required,
            updater.new_agent_message([_event_part(StatusEvent(code="input_required"))]),
            final=True,
        )
        return True

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # `context.task_id` is a2a's attribute, not ours to rename.
        turn_id = context.task_id or (context.current_task.id if context.current_task else "")
        runtime = self._aborts.get(turn_id)
        if runtime is not None:
            runtime.abort()
        if context.current_task:
            updater = TaskUpdater(
                event_queue, context.current_task.id, context.current_task.context_id
            )
            await updater.cancel()

    # The facade the session's socket serves, binding the turn machinery to this one session.

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def token(self) -> str:
        return self._token

    @property
    def is_running(self) -> bool:
        state = self._contexts.get(self._session_id)
        return bool(state is not None and state.running)

    def serialized_send(self):
        """The session-wide acceptance lock used by every external message send."""
        return self._send_lock

    async def start(self) -> None:
        """Prepare the session before its socket opens, without building the runtime it may never need."""
        from langmesh.base.primitives.limits import limits_from_configuration, set_limits
        from langmesh.base.primitives.telemetry import configure as configure_telemetry

        configuration = self._global_configuration
        set_limits(limits_from_configuration(configuration.tuning))

        telemetry = configuration.telemetry
        configure_telemetry(
            enabled=telemetry.enabled,
            endpoint=telemetry.exporter.endpoint,
            headers=telemetry.resolved_headers(),
            sample_ratio=telemetry.sample_ratio,
        )

        # Each session connects its own MCP servers: connections are stateful and belong to this process.
        servers = configuration.mcp.enabled_servers()
        if servers:
            from langmesh.base.contracts.mcp_client import MCPServerManager

            self._mcp_server_manager = MCPServerManager(servers)
            # Connected in the background, so a hung server does not delay the session's socket.
            self._mcp_connect = asyncio.create_task(self._mcp_server_manager.start())

        self._context(self._session_id)
        await self.resume_pending_jobs()

    async def start_turn(self, parts: list, metadata: dict) -> str:
        """Start a turn and answer with its task id as soon as it has one, rather than when the turn is over."""
        handler = self._agent_handler()
        if handler is None:
            raise RuntimeError("This session has no request handler.")
        self._title_from_first_message(parts)
        message = Message(
            role=Role.user,
            parts=parts,
            message_id=str(metadata.get("messageId") or uuid.uuid4().hex),
            context_id=self._session_id,
            metadata=turn_metadata_envelope(metadata) if metadata else None,
        )
        identified: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        async def drive() -> None:
            try:
                async for event in handler.on_message_send_stream(
                    MessageSendParams(message=message)
                ):
                    if isinstance(event, Task) and not identified.done():
                        identified.set_result(event.id)
            except Exception as error:  # noqa: BLE001 — a failed turn must still answer the send
                if not identified.done():
                    identified.set_exception(error)
                else:
                    logger.exception("the turn raised after it had started")
            finally:
                if not identified.done():
                    identified.set_result("")

        self._spawn_background(drive(), name="relay-turn")
        return await identified

    def _title_from_first_message(self, parts: list) -> None:
        """Name the session after what it was first asked to do, in the background."""
        if self._title_task is not None:
            return
        prose = " ".join(
            str(getattr(getattr(part, "root", part), "text", "") or "") for part in parts
        ).strip()
        if not prose:
            return
        self._title_task = asyncio.create_task(self._generate_title(prose))

    async def _generate_title(self, first_message: str) -> None:
        """Ask the session's features for a title and publish it; the naming feature owns the cap."""
        # The runtime (and with it the composed features) is built by the first turn, which starts
        # after this background task does, so wait on its readiness signal.
        state = self._contexts.get(self._session_id)
        if state is None:
            return
        await state.runtime_ready.wait()
        runtime = state.runtime
        if runtime is None:
            return
        try:
            title = await runtime._features.assign_title(first_message)
        except Exception:  # noqa: BLE001 — a session is not worth failing over its own name
            # Not `debug`: failing to name one session is cosmetic, failing to name every session is a fault.
            logger.warning(
                "could not generate a title for session %s", self._session_id, exc_info=True
            )
            return
        if title:
            await self._turn_store.publish_title(title)

    async def inject(self, text: str, message_id: str = "", peer_sender: str = "") -> bool:
        """Wait until a running turn places this message at a safe point, or say it ended first."""
        state = self._contexts.get(self._session_id)
        if state is None or not state.running or state.runtime is None:
            return False
        accepted = state.runtime.enqueue_steering(text, message_id, peer_sender)
        return bool(accepted is not None and await accepted)

    def note_observation_registry(self, metadata: dict[str, Any], error: str | None) -> None:
        """Pass watcher metadata and feedback to the warm runtime without a synthetic turn."""
        self._observation_registry_metadata = dict(metadata)
        self._observation_registry_error = error.strip() if error else None
        state = self._contexts.get(self._session_id)
        if state is not None and state.runtime is not None:
            _features.note_observation_registry(
                state.runtime,
                self._observation_registry_metadata,
                self._observation_registry_error,
            )

    async def resolve_pending_input(self, payload: dict) -> bool:
        """Record an answer to a parked gate and resume the turn once every gate in the batch has one."""
        handler = self._agent_handler()
        if handler is None:
            return False
        records = await self._turn_store.control_records_for_session(self._session_id)
        for turn_id, record in records:
            pending = record.pending
            if pending is None or pending.gate_for(str(payload.get("request_id", ""))) is None:
                continue
            message = Message(
                role=Role.user,
                parts=[envelope_part(INPUT_RESPONSE_KIND, **payload)],
                message_id=uuid.uuid4().hex,
                task_id=turn_id,
                context_id=self._session_id,
            )
            self._spawn_background(
                self._drive_input_response(handler, message), name="resume-input"
            )
            return True
        return False

    async def _drive_input_response(self, handler, message: Message) -> None:
        try:
            async for _event in handler.on_message_send_stream(MessageSendParams(message=message)):
                pass
        except Exception:  # noqa: BLE001 — a failed resume must not take the session down
            logger.exception("resuming session %s after an answer failed", self._session_id)

    def abort(self) -> bool:
        return self.abort_context(self._session_id)

    def abort_tool_call(self, tool_call_identifier: str) -> bool:
        return self.abort_tool(self._session_id, tool_call_identifier)

    async def pending_decision(self) -> dict:
        """What this session is parked on, as locale-independent data."""
        records = await self._turn_store.control_records_for_session(self._session_id)
        for _turn_id, record in records:
            pending = record.pending
            if pending is None or not pending.gates:
                continue
            unanswered = [gate for gate in pending.gates if gate.request_id not in pending.answers]
            if not unanswered:
                continue
            gate = unanswered[0]
            if gate.is_question:
                return {"kind": "question"}
            command = (gate.command or "").strip()
            return {"kind": "permission", **({"command": command} if command else {})}
        return {}

    async def compaction_failure(self) -> str | None:
        """The durable compaction failure that forbids accepting another user message."""
        state = self._contexts.get(self._session_id)
        if state is not None and state.runtime is not None:
            return _features.compaction_failure(state.runtime)
        snapshot = await self._turn_store.load_session_state(self._session_id)
        compaction = snapshot.feature("langmesh.runtime.plugins.compaction.Compaction")
        if not isinstance(compaction, dict) or not compaction.get("failure"):
            return None
        return str(compaction["failure"])

    async def abort_pending_input(self) -> bool:
        """Deny every gate this session is parked on; the last denial resumes the turn and records each refusal."""
        records = await self._turn_store.control_records_for_session(self._session_id)
        for _turn_id, record in records:
            pending = record.pending
            if pending is None or not pending.gates:
                continue
            for gate in pending.gates:
                payload = (
                    {"request_id": gate.request_id, "declined": True}
                    if gate.is_question
                    else {"request_id": gate.request_id, "decision": "deny"}
                )
                await self.resolve_pending_input(payload)
            return True
        return False

    async def compact(self) -> dict:
        """Compact this session's conversation and return its completed result."""
        return await self.compact_context(self._session_id)

    def background_tool_call(self, tool_call_identifier: str) -> bool:
        """Detach a still-blocking foreground command so the turn can continue."""
        return self.send_tool_to_background(self._session_id, tool_call_identifier)

    def background_jobs(self) -> list[dict]:
        """The background work this session currently has in flight."""
        return self.background_snapshots(self._session_id)

    def card_payload(self) -> dict:
        """What this session advertises at its well-known path, degrading to a minimal card rather than failing."""
        try:
            return self._build_card_payload()
        except Exception:  # noqa: BLE001 — a card is descriptive, never load-bearing
            logger.exception("building the agent card for session %s failed", self._session_id)
            return {
                "name": self._agent_name,
                "description": f"LangMesh session {self._session_id}.",
                "version": "1.0.0",
                "protocolVersion": "0.3.0",
                "url": f"unix:{self._session_id}",
                "defaultInputModes": ["text/plain"],
                "defaultOutputModes": ["text/plain"],
                "capabilities": {"streaming": True},
                "skills": [],
            }

    def _build_card_payload(self) -> dict:
        from langmesh.base.content.skills import skills_for_agent
        from langmesh.protocol.card import build_agent_card

        catalogue = machine_catalogue(
            self._global_configuration, self._working_directory, agent_loader=AgentFileLoader()
        )
        configuration = catalogue.agent(self._agent_name)
        if configuration is None:
            raise FileNotFoundError(f"Agent configuration not found: {self._agent_name}")
        skills = skills_for_agent(list(catalogue.skills()), configuration.skills)
        card = build_agent_card(configuration, skills, f"unix:{self._session_id}")
        return card.model_dump(by_alias=True, exclude_none=True, mode="json")

    def status_payload(self) -> dict:
        state = self._contexts.get(self._session_id)
        return {
            "session_id": self._session_id,
            "agent": self._agent_name,
            "running": bool(state is not None and state.running),
            "permission_mode": self._permission_mode,
        }

    async def aclose(self, *, preserve_background_jobs: bool = False) -> None:
        """Stop cleanly, in an order where nothing is torn down while something else still needs it."""
        state = self._contexts.get(self._session_id)
        owned_tasks = [
            task
            for task in (
                state.resume_pump if state is not None else None,
                state.continuation.workflow if state is not None else None,
            )
            if task is not None and not task.done()
        ]
        self.teardown_context(self._session_id, preserve_background_jobs=preserve_background_jobs)
        if owned_tasks:
            completed, pending = await asyncio.wait(
                owned_tasks,
                timeout=current_limits().sigterm_grace,
            )
            if completed:
                await asyncio.gather(*completed, return_exceptions=True)
            if pending:
                logger.warning(
                    "session %s left %d cancelled task(s) that did not settle",
                    self._session_id,
                    len(pending),
                )
        state_publishers = [
            task for task in (self._goal_state_tail,) if task is not None and not task.done()
        ]
        if state_publishers:
            await asyncio.gather(*state_publishers, return_exceptions=True)
        # Title generation is async relative to the turn, but owned by the session: an immediate idle sleep may not discard a model call that is already producing the durable title.
        if self._title_task is not None and not self._title_task.done():
            with contextlib.suppress(Exception):
                await self._title_task
        background_tasks = [task for task in self._background_tasks if not task.done()]
        if background_tasks:
            completed, pending = await asyncio.wait(
                background_tasks, timeout=current_limits().sigterm_grace
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*completed, *pending, return_exceptions=True)
        if self._mcp_connect is not None and not self._mcp_connect.done():
            self._mcp_connect.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._mcp_connect
        if self._mcp_server_manager is not None:
            with contextlib.suppress(Exception):
                await self._mcp_server_manager.aclose()
        close = getattr(self._turn_store, "aclose", None)
        if close is not None:
            await close()
