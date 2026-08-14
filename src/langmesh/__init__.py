"""LangMesh as a library: the harness without the daemon, driven turn by turn through `Session`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Optional, Sequence

from langmesh.base.catalogue import Catalogue
from langmesh.base.configuration import (
    AgentConfiguration,
    BashToolConfiguration,
    Configuration,
    FilesystemConfiguration,
    SandboxConfiguration,
    ToolboxConfiguration,
    ToolsConfiguration,
)
from langmesh.base.permission_mode import PermissionMode
from langmesh.base.errors import CompactionBlockedError
from langmesh.base.instructions import Instruction
from langmesh.base.observation_store import ObservationRegistryError
from langmesh.base.observations import ObservationRegistry
from langmesh.base.resources import (
    LocalResourceChanges,
    MaterializedResources,
    OverlayResources,
    ResourceChange,
    ResourceChangeSource,
    ResourceWatchUnsupported,
    WorkspaceResources,
    WorkspaceResourcesLike,
    resource_path,
)
from langmesh.base.skills import Skill
from langmesh.runtime.compaction import KeepRecentTurns
from langmesh.runtime.hooks import MaximumToolCalls
from langmesh.base.ports import (
    Approval,
    Approvals,
    CatalogueLike,
    Checkpoints,
    Compaction,
    CompactionState,
    Credentials,
    JobStore,
    MemoryCheckpoints,
    MemoryJobStore,
    MemoryTranscript,
    Observation,
    Observer,
    SuspensionGate,
    ToolMiddleware,
    Transcript,
    TurnHook,
    TurnSummary,
    describe_unmet,
)
from langmesh.base.schedules import (
    ScheduleError,
    is_due,
    next_firing,
    validate as validate_schedule,
)

# The vocabulary `stream()` speaks, exported because a caller driving a turn has to dispatch on it.
from langmesh.runtime.turn_events import (
    Checkpoint,
    CompactionDone,
    CompactionStarted,
    Done,
    EventType,
    GoalReviewFinished,
    GoalReviewProgress,
    GoalReviewStarted,
    Mcp,
    Status,
    Steering,
    Suspended,
    TextChunk,
    Thinking,
    ThinkingDone,
    ToolCall,
    ToolResult,
    TurnEvent,
    TurnEventUnion,
    Usage,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AgentConfiguration",
    "BashToolConfiguration",
    "Approval",
    "Approvals",
    "Catalogue",
    "CatalogueLike",
    "Checkpoint",
    "Checkpoints",
    "Compaction",
    "CompactionBlockedError",
    "CompactionDone",
    "CompactionStarted",
    "CompactionState",
    "Credentials",
    "Configuration",
    "Done",
    "EventType",
    "FilesystemConfiguration",
    "GoalReviewFinished",
    "GoalReviewProgress",
    "GoalReviewStarted",
    "Instruction",
    "JobStore",
    "Mcp",
    "KeepRecentTurns",
    "MaximumToolCalls",
    "MemoryCheckpoints",
    "MemoryJobStore",
    "MemoryTranscript",
    "MaterializedResources",
    "Observation",
    "ObservationRegistry",
    "ObservationRegistryError",
    "OverlayResources",
    "Observer",
    "SandboxConfiguration",
    "ResourceChange",
    "ResourceChangeSource",
    "ResourceWatchUnsupported",
    "PermissionMode",
    "Session",
    "ScheduleError",
    "Status",
    "Steering",
    "Suspended",
    "TextChunk",
    "Thinking",
    "ThinkingDone",
    "ToolboxConfiguration",
    "ToolCall",
    "ToolMiddleware",
    "ToolResult",
    "TurnEvent",
    "TurnEventUnion",
    "TurnHook",
    "Skill",
    "Usage",
    "WorkspaceResources",
    "WorkspaceResourcesLike",
    "LocalResourceChanges",
    "is_due",
    "next_firing",
    "validate_schedule",
    "ToolsConfiguration",
    "SuspensionGate",
    "Transcript",
    "TurnSummary",
    "__version__",
]


try:  # pragma: no cover - a source checkout has no distribution metadata
    from importlib.metadata import version as _package_version

    __version__ = _package_version("langmesh")
except Exception:  # noqa: BLE001 — a missing distribution must not break an import
    __version__ = "0"


def _apply_providers(configuration: Any, providers: Mapping[str, str | Mapping[str, str]]) -> None:
    """Put caller-supplied provider credentials onto a configuration, accepting the short form as well as the long."""
    from langmesh.base.configuration import ProviderCredential

    for name, value in providers.items():
        credential = configuration.providers.get(name) or ProviderCredential()
        if isinstance(value, str):
            credential = credential.model_copy(update={"api_key": value})
        else:
            credential = credential.model_copy(update=dict(value))
        configuration.providers[name] = credential


def _bind_retrieval_policy(configuration: Any) -> None:
    """Bind which models rank a screen, from the loaded configuration, beside the tuning policy rather than inside it."""
    screen = getattr(configuration, "computer_control", None)
    section = getattr(screen, "retrieval", None)
    if section is None:
        return
    from langmesh.computer.retrieval import retrieval_policy_from, set_retrieval_policy

    set_retrieval_policy(retrieval_policy_from(section))


def _require(port: type, candidate: Any, argument: str) -> Any:
    """Reject an implementation that does not satisfy its port, naming what is missing rather than failing mid-turn."""
    if candidate is None:
        return None
    unmet = describe_unmet(port, candidate)
    if unmet:
        raise TypeError(f"{argument}: {unmet}")
    return candidate


class Session:
    """One agent driven turn by turn in this process, built lazily so an unused session costs nothing."""

    def __init__(
        self,
        agent: AgentConfiguration,
        *,
        directory: str | Path | None = None,
        session_id: str = "",
        permission_mode: str = "",
        sandbox: Any = None,
        configuration: Any = None,
        # Provider credentials in code, though the environment variables still win.
        providers: Optional[Mapping[str, str | Mapping[str, str]]] = None,
        model_identifier: str = "",
        # The seams, each defaulting to the least surprising thing for a program that is not a daemon.
        model: Any = None,
        catalogue: Optional[CatalogueLike] = None,
        checkpoints: Optional[Checkpoints] = None,
        jobs: Optional[JobStore] = None,
        observer: Optional[Observer] = None,
        approvals: Optional[Approvals] = None,
        transcript: Optional[Transcript] = None,
        credentials: Optional[Credentials] = None,
        peers: Any = None,
        mcp_server_manager: Any = None,
        # Extension as distinct from configuration: tools the agent gains, and where it may run them.
        tools: Sequence[Any] = (),
        supplied_tool_gate: str = "ask",
        # The three seams around a turn, each defaulting to what the harness has always done.
        hooks: Sequence[Any] = (),
        pipeline: Sequence[Any] = (),
        compaction: Optional[Compaction] = None,
        permissions: Any = None,
        locations: Optional[list[dict]] = None,
        # A git worktree per session, off by default because it writes to disk.
        workspace: Any = None,
        tracer_provider: Any = None,
        # Any fsspec-backed workspace. Non-local sources are materialized for Bash and SQLite,
        # then synchronized at tool boundaries and close.
        resources: WorkspaceResourcesLike | None = None,
    ) -> None:
        from langmesh.base.configuration import Configuration
        from langmesh.base.identifiers import new_id

        if isinstance(agent, str):
            raise TypeError(
                "agent must be an AgentConfiguration, not a name. A name would mean this library goes looking for a profile on your machine. Build one in code, or load it yourself: `langmesh.daemon.machine.load_catalogue(...).agent(name)`."
            )
        self._agent = agent
        if directory is None and resources is None:
            raise TypeError("directory or resources is required")
        if directory is not None and resources is not None:
            raise TypeError("directory and resources describe the same workspace; pass exactly one")
        # Absolute, and not resolved against the process's directory: where tools run is a property of the run.
        if directory is not None and not Path(directory).is_absolute():
            raise ValueError(f"directory must be absolute, got {directory!r}.")
        supplied_resources = _require(WorkspaceResourcesLike, resources, "resources")
        self._resources = (
            supplied_resources
            if supplied_resources is not None
            else WorkspaceResources.local(str(directory))
        )
        local_resource_path = self._resources.local_path
        self._directory = str(local_resource_path) if local_resource_path is not None else ""
        self._session_id = session_id or new_id("session")
        self._permission_mode = permission_mode
        self._sandbox = sandbox
        self._peers = peers
        self._mcp_server_manager = mcp_server_manager
        self._lifecycle = AsyncExitStack()
        # Reading configuration must not leave a file in the caller's home directory.
        self._configuration = (
            configuration
            if configuration is not None
            else Configuration(toolbox=ToolboxConfiguration(enabled=False))
        )
        if providers:
            _apply_providers(self._configuration, providers)
        self._model_identifier = model_identifier
        self._model = model
        self._catalogue = _require(CatalogueLike, catalogue, "catalogue")
        self._checkpoints = _require(Checkpoints, checkpoints, "checkpoints") or MemoryCheckpoints()
        self._jobs = _require(JobStore, jobs, "jobs") or MemoryJobStore()
        self._observer = _require(Observer, observer, "observer")
        self._approvals = _require(Approvals, approvals, "approvals")
        self._transcript = _require(Transcript, transcript, "transcript") or MemoryTranscript()
        self._credentials = _require(Credentials, credentials, "credentials")
        self._tools = list(tools)
        self._supplied_tool_gate = supplied_tool_gate
        self._permissions = permissions
        self._hooks = list(hooks)
        self._pipeline = list(pipeline)
        self._compaction = _require(Compaction, compaction, "compaction")
        self._locations = locations
        self._workspace = workspace
        self._tracer_provider = tracer_provider
        self._observations = ObservationRegistry(self._resources, configuration=self._configuration)
        self._materialized_resources: MaterializedResources | None = None
        # Where tools actually run. Equal to `directory` unless a workspace repointed it.
        self._runtime_directory = self._directory
        self._bindings: list = []
        self._runtime: Any = None
        self._restored = False
        self._turn_lock = asyncio.Lock()

    @property
    def id(self) -> str:
        """This session's identity, which is what a checkpoint is keyed by."""
        return self._session_id

    @property
    def resources(self) -> WorkspaceResourcesLike:
        """The configured workspace resources this session materializes for path-native tools."""
        return self._resources

    @property
    def observations(self) -> ObservationRegistry:
        """A configured observational-memory reader over this session's workspace resources."""
        return self._observations

    @property
    def runtime(self) -> Any:
        """The underlying `AgentRuntime`, built on first use and exposed so no non-obvious use needs a fork."""
        if self._runtime is None:
            if self._materialized_resources is None and self._resources is not None:
                if self._resources.local_path is None:
                    raise RuntimeError(
                        "non-local resources require `async with Session(...)` so LangMesh can hold their materialized POSIX view"
                    )
            from langmesh.base.tuning import set_tuning, tuning_from_policy
            from langmesh.runtime.runtime import AgentRuntime

            # The tuning policy is bound per task, so binding it here scopes it to the caller rather than the interpreter.
            set_tuning(tuning_from_policy(self._configuration.tuning))
            _bind_retrieval_policy(self._configuration)
            # Both are bound per task, so two sessions in one interpreter can hold different credentials and tracers.
            if self._credentials is not None:
                from langmesh.base.credentials import set_credentials

                self._bindings.append(("credentials", set_credentials(self._credentials)))
            if self._tracer_provider is not None:
                from langmesh.base.telemetry import set_tracer

                self._bindings.append(
                    ("tracer", set_tracer(self._tracer_provider.get_tracer("langmesh")))
                )
            # The directory the caller supplied plus the packaged base layer, and deliberately nothing of `$HOME`.
            if self._catalogue is None:
                from langmesh.base.catalogue import project_catalogue

                catalogue = project_catalogue(self._configuration, self._directory)
            else:
                catalogue = self._catalogue
            agent_configuration = self._agent
            # A model named at the call site beats the profile's, since editing a file to express a runtime choice is the wrong seam.
            if self._model_identifier:
                if "/" not in self._model_identifier:
                    raise ValueError(
                        f"model_identifier must be `provider/model`, not {self._model_identifier!r}."
                    )
                provider, model = self._model_identifier.split("/", 1)
                agent_configuration = agent_configuration.model_copy(
                    update={"provider": provider, "model": model}
                )
            self._runtime = AgentRuntime(
                agent_configuration=agent_configuration,
                global_configuration=self._configuration,
                session_id=self._session_id,
                working_directory=self._runtime_directory,
                project_directory=self._directory,
                permission_mode=self._permission_mode,
                # Handed over as the caller gave it, including `None`, which the runtime reads as the configured default.
                sandbox=self._sandbox,
                session_access=self._peers,
                mcp_server_manager=self._mcp_server_manager,
                catalogue=catalogue,
                model=self._model,
                jobs=self._jobs,
                observer=self._observer,
                approvals=self._approvals,
                transcript=self._transcript,
                tools=self._tools,
                supplied_tool_gate=self._supplied_tool_gate,
                permissions=self._permissions,
                hooks=self._hooks,
                pipeline=self._pipeline,
                compaction=self._compaction,
                locations=self._resolved_locations(),
                resource_sync=(
                    self._materialized_resources.sync
                    if self._materialized_resources is not None
                    else None
                ),
            )
        return self._runtime

    def _resolved_locations(self) -> Optional[list[dict]]:
        """Where this session's tools may run, with `None` meaning one local location at the working directory."""
        return self._locations

    async def prepare_worktree(self, strategy: str = "worktree") -> str:
        """Give this session its own git worktree and run its tools there; opt-in, because it writes to disk."""
        if not self._directory:
            raise RuntimeError("prepare_worktree requires a local directory-backed session")
        manager = self._workspace
        if manager is None:
            from langmesh.base.worktrees import SessionWorktreeManager

            manager = SessionWorktreeManager()
        prepared = await manager.prepare(self._session_id, self._directory, strategy)
        self._runtime_directory = prepared.runtime_working_directory or self._directory
        return self._runtime_directory

    async def sync_resources(self) -> None:
        """Publish path-native changes to the configured fsspec workspace now."""
        if self._materialized_resources is None:
            raise RuntimeError("resources are materialized only inside `async with Session(...)`")
        await self._materialized_resources.sync()

    async def refresh_resources(self) -> None:
        """Refresh the materialized view from its source at a caller-chosen idle boundary."""
        async with self._turn_lock:
            if self._materialized_resources is None:
                raise RuntimeError(
                    "resources are materialized only inside `async with Session(...)`"
                )
            await self._materialized_resources.refresh()
            if self._runtime is not None:
                try:
                    metadata = await self._observations.describe()
                except ObservationRegistryError as error:
                    self._runtime.note_observation_registry({}, str(error))
                else:
                    self._runtime.note_observation_registry(metadata)
                self._runtime.refresh_system_prompt()

    @property
    def transcript(self) -> Transcript:
        """The record of this session's turns."""
        return self._transcript

    async def restore(self) -> bool:
        """Reload this session's conversation from its checkpoint store, and say whether anything was found."""
        self._restored = True
        state = await self._checkpoints.load(self._session_id)
        if not state:
            return False
        messages = state.get("conversation") or []
        session_state = state.get("session") or {}
        if not messages and not session_state:
            return False
        from langchain_core.load import loads as load_message

        self.runtime.conversation[:] = [load_message(entry) for entry in messages]
        if session_state:
            self.runtime.restore_session(session_state)
        return True

    async def save(self) -> None:
        """Write this session's conversation to its checkpoint store, through LangChain's own codec rather than by hand."""
        from langchain_core.load import dumps as dump_message

        await self._checkpoints.save(
            self._session_id,
            {
                "conversation": [dump_message(message) for message in self.conversation],
                "session": self.runtime.session_snapshot(),
            },
        )

    def _compose(self, message: str, attachments: Sequence[str | Path]) -> object:
        """The model-facing input for a turn, including attachments, through the same composition the daemon uses."""
        if not attachments:
            return message
        from langmesh.protocol.files import attachment_from_path
        from langmesh.protocol.parts import attachment_payload, compose_turn_input

        records = []
        for attachment in attachments:
            path = Path(attachment)
            if not path.is_absolute():
                path = Path(self._runtime_directory) / resource_path(str(path))
            records.append(attachment_from_path(path))
        runtime = self.runtime
        runtime.note_attachments([record["path"] for record in records])
        turn_input, images_not_inlined = compose_turn_input(
            message,
            [attachment_payload(records)],
            runtime.model_identifier,
        )
        if images_not_inlined:
            # A library caller may have no client to raise a warning event to, so this goes to the log it does have.
            logger.warning(
                "%d attached image(s) were not inlined: %s does not advertise vision support. The model has the file paths and can open them with its tools.",
                images_not_inlined,
                runtime.model_identifier or "the session model",
            )
        return turn_input

    async def stream(
        self,
        message: str,
        *,
        attachments: Sequence[str | Path] = (),
    ) -> AsyncIterator[TurnEventUnion]:
        """Drive a turn, yielding each event, and keep driving turns while a goal the agent set is still open."""
        async with self._turn_lock:
            if not self._restored:
                await self.restore()
            failure = self.runtime.compaction_failure
            if failure:
                raise CompactionBlockedError(
                    f"Context compaction failed: {failure} Retry Session.compact() before sending more work."
                )
            # Somebody is here again, so a goal that had used its allowance gets it back.
            self.runtime.abandon_turn_retry()
            self.runtime.restore_goal_allowance()
            try:
                async for event in self.runtime.stream(self._compose(message, attachments)):
                    yield event
                if self.runtime.compaction_failure:
                    return
                # A goal outlives the turn that set it, so this call is over when the goal is rather than when the model stops talking.
                async for event in self._pursue_goal():
                    yield event
                self.runtime.mark_turn_succeeded()
            except Exception:
                self.runtime.mark_turn_failed()
                raise
            finally:
                await self.save()

    async def _pursue_goal(self) -> AsyncIterator[TurnEventUnion]:
        """Keep driving turns while the review says the goal is unreached, up to its allowance."""
        from langmesh.base.tuning import Tunable, active_tuning

        allowance = active_tuning().amount(Tunable.goal_continuation_turns)
        while True:
            goal = self.runtime.goal
            if goal is None or not goal.is_open:
                return
            if goal.continuations >= allowance:
                self.runtime.park_goal()
                return
            goal = self.runtime.goal
            if goal is None or not goal.is_open:
                return
            review_events: asyncio.Queue[TurnEventUnion] = asyncio.Queue()
            review_task = asyncio.create_task(self.runtime.review_goal(review_events.put))
            try:
                while True:
                    if not review_events.empty():
                        yield review_events.get_nowait()
                        continue
                    if review_task.done():
                        break
                    next_event = asyncio.create_task(review_events.get())
                    finished, _ = await asyncio.wait(
                        {review_task, next_event}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if next_event in finished:
                        yield next_event.result()
                    else:
                        next_event.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await next_event
                while not review_events.empty():
                    yield review_events.get_nowait()
                review = await review_task
            finally:
                if not review_task.done():
                    review_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await review_task
            goal = self.runtime.apply_goal_review(review)
            if goal is None or not goal.is_open or not goal.review_message:
                return
            self.runtime.note_goal_continuation()
            async for event in self.runtime.stream(
                goal.review_message, as_system_note=True, opens_exchange=True
            ):
                yield event

    async def ask(self, message: str, *, attachments: Sequence[str | Path] = ()) -> str:
        """Drive a turn, or a goal to its end, and answer with the agent's prose."""
        from langmesh.runtime.turn_events import Done, Suspended

        answer = ""
        async for event in self.stream(message, attachments=attachments):
            if isinstance(event, Suspended):
                raise PermissionError(
                    "This turn needs a human decision, and nothing is answering gates. Pass `approvals=` to decide them in code, drive `stream()` and answer them yourself, or create the session in a permission mode that does not gate this work."
                )
            if isinstance(event, Done):
                answer = event.text or answer
        return answer

    async def compact(self) -> AsyncIterator[TurnEventUnion]:
        """Prepare and fold the conversation now, retrying the exact failed phase when necessary."""
        async with self._turn_lock:
            if not self._restored:
                await self.restore()
            runtime = self.runtime
            if runtime.compaction_failure:
                retry_operation = runtime.retry_compaction()
            else:
                runtime.begin_compaction_preparation()
                retry_operation = "prepare"
            resume_after = runtime.resumes_after_compaction
            try:
                if retry_operation == "fold":
                    source = runtime.compact(reason=runtime.pending_compaction_reason)
                else:
                    source = (
                        runtime.continue_stream()
                        if runtime.resumes_after_compaction
                        else runtime.prepare_compaction_stream()
                    )
                async for event in source:
                    yield event
                if retry_operation == "fold" and resume_after and not runtime.compaction_failure:
                    async for event in runtime.continue_stream():
                        yield event
            finally:
                await self.save()

    async def retry(self) -> AsyncIterator[TurnEventUnion]:
        """Continue the last failed turn from its durable conversation state."""
        async with self._turn_lock:
            if not self._restored:
                await self.restore()
            if self.runtime.compaction_failure:
                raise CompactionBlockedError(
                    "Context compaction is blocked; retry Session.compact() before retrying the turn."
                )
            if not self.runtime.begin_turn_retry():
                raise RuntimeError("This session has no failed turn to retry.")
            try:
                async for event in self.runtime.continue_stream():
                    yield event
                self.runtime.mark_turn_succeeded()
            except Exception:
                self.runtime.mark_turn_failed()
                raise
            finally:
                await self.save()

    @property
    def conversation(self) -> list:
        """The model-facing message list, which accumulates across turns."""
        return self.runtime.conversation

    async def aclose(self) -> None:
        """Release what the session opened: background jobs, and the browser if it was used."""
        import sys

        if self._runtime is not None:
            with contextlib.suppress(Exception):
                self._runtime.abort()
        # Unbind what the session bound, so a caller's credentials and tracer do not outlive it.
        for kind, token in reversed(self._bindings):
            with contextlib.suppress(Exception):
                if kind == "credentials":
                    from langmesh.base.credentials import reset_credentials

                    reset_credentials(token)
                else:
                    from langmesh.base.telemetry import reset_tracer

                    reset_tracer(token)
        self._bindings.clear()
        from langmesh.runtime.background import cancel_all_background_jobs

        with contextlib.suppress(Exception):
            cancel_all_background_jobs()
        if "langmesh.computer.web" in sys.modules:
            with contextlib.suppress(Exception):
                sys.modules["langmesh.computer.web"].close()
        await self._lifecycle.aclose()
        self._materialized_resources = None
        self._runtime = None
        self._restored = False
        local_resource_path = self._resources.local_path
        self._directory = str(local_resource_path) if local_resource_path is not None else ""
        self._runtime_directory = self._directory

    async def __aenter__(self) -> "Session":
        if self._materialized_resources is None:
            self._lifecycle = AsyncExitStack()
            try:
                materialized = await self._lifecycle.enter_async_context(
                    self._resources.materialize()
                )
                self._materialized_resources = materialized
                self._directory = str(materialized.path)
                self._runtime_directory = self._directory
                if self._mcp_server_manager is None:
                    from langmesh.base.configuration import MCPConfiguration

                    servers = MCPConfiguration.from_dotagents_roots(
                        [Path(self._directory) / ".agents"]
                    ).enabled_servers()
                    if servers:
                        from langmesh.base.mcp_client import MCPServerManager

                        manager = MCPServerManager(servers)
                        await manager.start()
                        self._mcp_server_manager = manager

                        async def close_manager() -> None:
                            await manager.aclose()
                            if self._mcp_server_manager is manager:
                                self._mcp_server_manager = None

                        self._lifecycle.push_async_callback(close_manager)
            except BaseException:
                await self._lifecycle.aclose()
                self._materialized_resources = None
                raise
        return self

    async def __aexit__(self, *_exception: object) -> None:
        await self.aclose()
