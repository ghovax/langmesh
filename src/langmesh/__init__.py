"""LangMesh as a library: the harness in-process, driven turn by turn through `Session`."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Optional, Sequence

from langmesh.base.contracts.catalogue import Catalogue
from langmesh.base.content.attachments import AttachmentInput, PathAttachments
from langmesh.base.contracts.tools import ToolGrant, ToolLike, as_tool_grants
from langmesh.base.configuration import (
    AgentConfiguration,
    BashToolConfiguration,
    Configuration,
    FilesystemConfiguration,
    SandboxConfiguration,
    MCPConfiguration,
    MCPServerConfiguration,
    ToolboxConfiguration,
    ToolsConfiguration,
)
from langmesh.base.configuration.permission_mode import PermissionMode
from langmesh.base.primitives.errors import CompactionBlockedError
from langmesh.base.content.instructions import Instruction
from langmesh.base.persistence.observation_store import ObservationRegistryError
from langmesh.base.persistence.observations import ObservationRegistry
from langmesh.base.confinement.file_leases import FileLeaseManager
from langmesh.base.contracts.mcp_client import MCPServerManager
from langmesh.base.persistence.resources import (
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
from langmesh.base.content.skills import Skill
from langmesh.base.persistence.worktrees import SessionWorktree, SessionWorktreeManager
from langmesh.locations.executor import LocationExecutor, LocalExecutor, SshExecutor
from langmesh.runtime.compaction import (
    DirectCompactionPreparation,
    KeepRecentTurns,
    ObservationCompactionPreparation,
)
from langmesh.runtime.continuation import TuningContinuationPolicy
from langmesh.runtime.features import access as _features
from langmesh.runtime.composition import RuntimeComponents, RuntimeProfile, SessionComponents
from langmesh.runtime.hooks import MaximumToolCalls
from langmesh.runtime.locations import Location
from langmesh.runtime.session_control import PendingTurn, SessionPhase, SessionState
from langmesh.base.contracts.ports import (
    Approval,
    Approvals,
    Attachments,
    AfterTurnHook,
    BeforeModelHook,
    BeforeToolsHook,
    CatalogueLike,
    Checkpoints,
    Compaction,
    CompactionPreparation,
    CompactionState,
    CompactionSummarizer,
    CompactionSummaryState,
    ContinuationPolicy,
    Credentials,
    FileLeases,
    GoalReviewContext,
    GoalReviewJournal,
    GoalReviewOutcome,
    JobStore,
    MemoryCheckpoints,
    MemoryJobStore,
    MemoryTranscript,
    MCPServers,
    Observation,
    Observer,
    PermissionPolicy,
    PromptComposer,
    PromptLayer,
    SessionAccess,
    SuspensionGate,
    ToolMiddleware,
    ToolInvocation,
    Transcript,
    TurnHook,
    TurnSummary,
    WorkspaceManager,
    describe_unmet,
)
from langmesh.base.persistence.schedules import (
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
    "AgentRuntime",
    "BashToolConfiguration",
    "Approval",
    "Approvals",
    "AttachmentInput",
    "Attachments",
    "AfterTurnHook",
    "BeforeModelHook",
    "BeforeToolsHook",
    "Catalogue",
    "CatalogueLike",
    "Checkpoint",
    "Checkpoints",
    "Compaction",
    "CompactionPreparation",
    "CompactionBlockedError",
    "CompactionDone",
    "CompactionStarted",
    "CompactionState",
    "CompactionSummarizer",
    "CompactionSummaryState",
    "ContinuationPolicy",
    "Credentials",
    "FileLeases",
    "Configuration",
    "Done",
    "DirectCompactionPreparation",
    "EventType",
    "FilesystemConfiguration",
    "FileLeaseManager",
    "GoalReviewFinished",
    "GoalReviewProgress",
    "GoalReviewStarted",
    "GoalReviewContext",
    "GoalReviewJournal",
    "GoalReviewOutcome",
    "Instruction",
    "JobStore",
    "Mcp",
    "MCPConfiguration",
    "MCPServerConfiguration",
    "MCPServerManager",
    "MCPServers",
    "KeepRecentTurns",
    "Location",
    "LocationExecutor",
    "LocalExecutor",
    "MaximumToolCalls",
    "MemoryCheckpoints",
    "MemoryJobStore",
    "MemoryTranscript",
    "MaterializedResources",
    "Observation",
    "ObservationCompactionPreparation",
    "ObservationRegistry",
    "ObservationRegistryError",
    "OverlayResources",
    "PathAttachments",
    "Observer",
    "SandboxConfiguration",
    "ResourceChange",
    "ResourceChangeSource",
    "ResourceWatchUnsupported",
    "RuntimeComponents",
    "RuntimeProfile",
    "PermissionMode",
    "PermissionPolicy",
    "PromptComposer",
    "PromptLayer",
    "PendingTurn",
    "Session",
    "SessionAccess",
    "SessionComponents",
    "ToolGrant",
    "ToolLike",
    "as_tool_grants",
    "SessionPhase",
    "SessionState",
    "SessionWorktree",
    "SessionWorktreeManager",
    "ScheduleError",
    "Status",
    "SshExecutor",
    "Steering",
    "Suspended",
    "TextChunk",
    "Thinking",
    "ThinkingDone",
    "ToolboxConfiguration",
    "ToolCall",
    "ToolMiddleware",
    "ToolInvocation",
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
    "TuningContinuationPolicy",
    "SuspensionGate",
    "Transcript",
    "TurnSummary",
    "WorkspaceManager",
    "__version__",
]


try:  # pragma: no cover - a source checkout has no distribution metadata
    from importlib.metadata import version as _package_version

    __version__ = _package_version("langmesh")
except Exception:  # noqa: BLE001 — a missing distribution must not break an import
    __version__ = "0"


def __getattr__(name: str) -> Any:
    """Keep the heavyweight runtime lazy while making it a first-class library export."""
    if name == "AgentRuntime":
        from langmesh.runtime.runtime import AgentRuntime

        return AgentRuntime
    raise AttributeError(name)


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
        locations: Sequence[Location] | None = None,
        tools: Sequence[ToolLike] = (),
        components: SessionComponents = SessionComponents(),
        # Any fsspec-backed workspace. Non-local sources are materialized for Bash and SQLite, then synchronized at tool boundaries and close.
        resources: WorkspaceResourcesLike | None = None,
    ) -> None:
        from langmesh.base.configuration import Configuration
        from langmesh.base.primitives.identifiers import new_id

        if isinstance(agent, str):
            raise TypeError(
                "agent must be an AgentConfiguration, not a name. A name would mean this library goes looking for a profile on your machine. Build one in code, or load your own catalogue."
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
        if not isinstance(components, SessionComponents):
            raise TypeError("components must be a SessionComponents value")
        if tools:
            components = dataclasses.replace(
                components,
                tools=[*components.tools, *as_tool_grants(tools)],
            )
        self._components = components
        # Grants handed to `grant_tool` before the runtime exists, applied when it is built.
        self._pending_grants: list[ToolGrant] = []
        self._mcp_server_manager = components.mcp_servers
        self._lifecycle = AsyncExitStack()
        # Reading configuration must not leave a file in the caller's home directory.
        if configuration is not None and not isinstance(configuration, Configuration):
            raise TypeError("configuration must be a Configuration value")
        self._configuration = (
            configuration.model_copy(deep=True)
            if configuration is not None
            else Configuration(toolbox=ToolboxConfiguration(enabled=False))
        )
        if providers:
            _apply_providers(self._configuration, providers)
        self._model_identifier = model_identifier
        self._catalogue = _require(CatalogueLike, components.catalogue, "components.catalogue")
        self._checkpoints = (
            _require(Checkpoints, components.checkpoints, "components.checkpoints")
            or MemoryCheckpoints()
        )
        self._attachments = (
            _require(Attachments, components.attachments, "components.attachments")
            or PathAttachments()
        )
        self._jobs = _require(JobStore, components.jobs, "components.jobs") or MemoryJobStore()
        self._observer = _require(Observer, components.observer, "components.observer")
        self._approvals = _require(Approvals, components.approvals, "components.approvals")
        self._transcript = (
            _require(Transcript, components.transcript, "components.transcript")
            or MemoryTranscript()
        )
        self._credentials = _require(
            Credentials, components.credentials, "components.credentials"
        )
        self._compaction = _require(
            Compaction, components.compaction, "components.compaction"
        )
        _require(
            CompactionPreparation,
            components.compaction_preparation,
            "components.compaction_preparation",
        )
        _require(
            ContinuationPolicy,
            components.continuations,
            "components.continuations",
        )
        _require(PermissionPolicy, components.permissions, "components.permissions")
        _require(FileLeases, components.file_leases, "components.file_leases")
        _require(
            GoalReviewJournal,
            components.goal_review_journal,
            "components.goal_review_journal",
        )
        _require(WorkspaceManager, components.workspace, "components.workspace")
        _require(SessionAccess, components.sessions, "components.sessions")
        _require(MCPServers, components.mcp_servers, "components.mcp_servers")
        if locations is not None and not all(isinstance(location, Location) for location in locations):
            raise TypeError("locations must contain only Location values")
        self._locations = tuple(locations) if locations is not None else None
        self._workspace = components.workspace
        self._tracer_provider = components.tracer_provider
        self._observations = ObservationRegistry(self._resources, configuration=self._configuration)
        self._materialized_resources: MaterializedResources | None = None
        # Where tools actually run. Equal to `directory` unless a workspace repointed it.
        self._runtime_directory = self._directory
        self._bindings: list = []
        self._runtime: Any = None
        self._restored = False
        self._observation_metadata_loaded = False
        self._turn_lock = asyncio.Lock()
        self._phase = SessionPhase.IDLE
        self._pending: PendingTurn | None = None

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
            from langmesh.base.primitives.tuning import set_tuning, tuning_from_policy
            from langmesh.runtime.runtime import AgentRuntime

            # The tuning policy is bound per task, so binding it here scopes it to the caller rather than the interpreter.
            set_tuning(tuning_from_policy(self._configuration.tuning))
            # Both are bound per task, so two sessions in one interpreter can hold different credentials and tracers.
            if self._credentials is not None:
                from langmesh.base.identity.credentials import set_credentials

                self._bindings.append(("credentials", set_credentials(self._credentials)))
            if self._tracer_provider is not None:
                from langmesh.base.primitives.telemetry import set_tracer

                self._bindings.append(
                    ("tracer", set_tracer(self._tracer_provider.get_tracer("langmesh")))
                )
            # The directory the caller supplied plus the packaged base layer, and deliberately nothing of `$HOME`.
            if self._catalogue is None:
                from langmesh.base.contracts.catalogue import project_catalogue

                catalogue = project_catalogue(self._configuration, self._directory)
            else:
                catalogue = self._catalogue
            compaction_preparation = self._components.compaction_preparation
            if compaction_preparation is None:
                from langmesh.base.persistence.observation_store import SQLiteObservationStore
                from langmesh.runtime.compaction import ObservationCompactionPreparation

                compaction_preparation = ObservationCompactionPreparation(
                    SQLiteObservationStore(
                        self._configuration.observation_database_for(self._runtime_directory)
                    )
                )
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
                RuntimeProfile(
                    agent=agent_configuration,
                    configuration=self._configuration,
                    session_id=self._session_id,
                    working_directory=self._runtime_directory,
                    project_directory=self._directory,
                    permission_mode=self._permission_mode,
                    sandbox=self._sandbox,
                    locations=self._resolved_locations(),
                ),
                self._components.for_runtime(
                    catalogue=catalogue,
                    jobs=self._jobs,
                    observer=self._observer,
                    approvals=self._approvals,
                    transcript=self._transcript,
                    mcp_servers=self._mcp_server_manager,
                    compaction=self._compaction,
                    compaction_preparation=compaction_preparation,
                    synchronize_resources=(
                        self._materialized_resources.sync
                        if self._materialized_resources is not None
                        else None
                    ),
                    features=self._components.features or [],
                ),
            )
            for grant in self._pending_grants:
                self._runtime.grant_tool(grant.tool)
            self._pending_grants = []
        return self._runtime

    def grant_tool(self, tool: ToolLike) -> None:
        """Grant a tool to this session now: dispatchable immediately, and described to the model
        by an appended conversation message from the next model call. Append-only, so the provider
        cache prefix is untouched. Works before the first turn as well as mid-session."""
        grant = as_tool_grants([tool])[0]
        if self._runtime is None:
            self._pending_grants.append(grant)
        else:
            self._runtime.grant_tool(grant.tool)

    def _resolved_locations(self) -> Sequence[Location] | None:
        """Where this session's tools may run, with `None` meaning one local location at the working directory."""
        return self._locations

    async def prepare_worktree(self, strategy: str = "worktree") -> str:
        """Give this session its own git worktree and run its tools there; opt-in, because it writes to disk."""
        if self._runtime is not None:
            raise RuntimeError("prepare_worktree must run before the session builds its runtime")
        if not self._directory:
            raise RuntimeError("prepare_worktree requires a local directory-backed session")
        manager = self._workspace
        if manager is None:
            from langmesh.base.persistence.worktrees import SessionWorktreeManager

            manager = SessionWorktreeManager()
        prepared = await manager.prepare(self._session_id, self._directory, strategy)
        self._runtime_directory = prepared.runtime_working_directory or self._directory
        return self._runtime_directory

    def set_locations(self, locations: Sequence[Location] | None) -> SessionState:
        """Replace the addressable execution locations, reaching the next tool call in a live turn."""
        if locations is not None and not all(isinstance(location, Location) for location in locations):
            raise TypeError("locations must contain only Location values")
        self._locations = tuple(locations) if locations is not None else None
        if self._runtime is not None:
            self._runtime.set_locations(self._locations)
        return self.state

    def refresh_prompt(self) -> None:
        """Rebuild catalogue-derived static instructions at the next model boundary."""
        if self._runtime is not None:
            self._runtime.refresh_system_prompt()

    async def clear_goal(self) -> bool:
        """Call off the current goal and durably record its final state before returning."""
        async with self._turn_lock:
            if not self._restored:
                await self.restore()
            runtime = self.runtime
            goal = _features.goal(runtime)
            if goal is None:
                return False
            from langmesh.runtime.goal import Goal

            _features.write_goal(runtime, 
                goal.updated(status=Goal.CLEARED, review_message=None, review_id=None)
            )
            await self.save()
            return True

    async def refresh_observations(self) -> None:
        """Reload observational-memory metadata and rebuild the prompt at the next model boundary."""
        try:
            metadata = await self._observations.describe()
        except ObservationRegistryError as error:
            _features.note_observation_registry(self.runtime, {}, str(error))
        else:
            _features.note_observation_registry(self.runtime, metadata)
        self._observation_metadata_loaded = True
        self.runtime.refresh_system_prompt()

    async def _ensure_observation_metadata(self) -> None:
        if not self._observation_metadata_loaded:
            await self.refresh_observations()

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
                await self.refresh_observations()

    @property
    def transcript(self) -> Transcript:
        """The record of this session's turns."""
        return self._transcript

    @property
    def state(self) -> SessionState:
        """The current control surface as one coherent snapshot."""
        runtime = self._runtime
        return SessionState(
            phase=self._phase,
            pending=self._pending,
            permission_mode=str(runtime.permission_mode if runtime is not None else self._permission_mode),
            compaction_failure=_features.compaction_failure(runtime) if runtime is not None else None,
            background_jobs=tuple(_features.background_snapshots(runtime, )) if runtime is not None else (),
            unfinished_tasks=tuple(_features.unfinished_tasks(runtime, )) if runtime is not None else (),
            goal=_features.goal(runtime) if runtime is not None else None,
        )

    def interrupt(self) -> bool:
        """Request cancellation of the turn currently running."""
        if self._runtime is None or self._phase not in {
            SessionPhase.RUNNING,
            SessionPhase.COMPACTING,
            SessionPhase.RETRYING,
        }:
            return False
        self._runtime.abort()
        return True

    def interrupt_tool(self, tool_call_id: str) -> bool:
        """Cancel one foreground or detached tool call by its streamed identifier."""
        return bool(self._runtime and _features.abort_tool(self._runtime, tool_call_id))

    def background_tool(self, tool_call_id: str) -> bool:
        """Detach one eligible foreground tool call without stopping its turn."""
        return bool(self._runtime and _features.send_tool_to_background(self._runtime, tool_call_id))

    def background_jobs(self) -> tuple[Mapping[str, Any], ...]:
        """Return immutable snapshots of this session's active background work."""
        return tuple(_features.background_snapshots(self._runtime, )) if self._runtime is not None else ()

    async def steer(self, message: str, *, message_id: str = "") -> bool:
        """Append a user message at the running turn's next safe provider boundary."""
        if self._runtime is None or self._phase is not SessionPhase.RUNNING:
            return False
        accepted = self._runtime.enqueue_steering(message, message_id=message_id)
        return bool(await accepted) if accepted is not None else False

    async def respond(self, request_id: str, decision: Approval) -> SessionState:
        """Durably record one human response; call :meth:`resume` when ``state.pending.ready`` is true."""
        if not isinstance(decision, Approval):
            raise TypeError("decision must be an Approval value")
        async with self._turn_lock:
            if not self._restored:
                await self.restore()
            if self._pending is None:
                raise RuntimeError("This session has no suspended turn.")
            self._pending = self._pending.with_decision(request_id, decision)
            await self.save()
            return self.state

    async def cancel_pending(self) -> None:
        """Abandon a suspended batch without executing any of its calls."""
        async with self._turn_lock:
            if self._pending is None:
                raise RuntimeError("This session has no suspended turn.")
            self.runtime.abandon_suspension()
            self._pending = None
            self._phase = SessionPhase.IDLE
            await self.save()

    async def set_permission_mode(self, mode: str | PermissionMode) -> SessionState:
        """Change live permission policy and re-evaluate any unanswered parked gates."""
        resolved = mode if isinstance(mode, PermissionMode) else PermissionMode.resolve(mode)
        self._permission_mode = str(resolved)
        runtime = self.runtime
        runtime.set_permission_mode(resolved)
        pending = self._pending
        if pending is not None:
            for gate in pending.remaining:
                verdict = await _features.reconsider_gate(runtime, gate)
                if not verdict:
                    continue
                if gate.kind == "question":
                    decision = Approval(
                        allow=False,
                        reason=str(verdict.get("reason") or ""),
                    )
                else:
                    decision = Approval(
                        allow=verdict.get("decision") == "allow",
                        reason=str(verdict.get("reason") or ""),
                    )
                pending = pending.with_decision(gate.request_id, decision)
            self._pending = pending
            await self.save()
        return self.state

    async def restore(self) -> bool:
        """Reload this session's conversation from its checkpoint store, and say whether anything was found."""
        self._restored = True
        state = await self._checkpoints.load(self._session_id)
        if not state:
            return False
        messages = state.get("conversation") or []
        session_state = state.get("session") or {}
        pending_state = state.get("pending")
        if not messages and not session_state and not pending_state:
            return False
        from langchain_core.messages import messages_from_dict

        self.runtime.conversation[:] = messages_from_dict(messages)
        if session_state:
            self.runtime.restore_session(session_state)
        if isinstance(pending_state, Mapping):
            self._pending = PendingTurn.restore(pending_state)
            self._phase = SessionPhase.SUSPENDED
        return True

    async def save(self) -> None:
        """Write this session's conversation to its checkpoint store through LangChain's message codec."""
        from langchain_core.messages import message_to_dict

        await self._checkpoints.save(
            self._session_id,
            {
                "conversation": [message_to_dict(message) for message in self.conversation],
                "session": self.runtime.session_snapshot(),
                "pending": self._pending.snapshot() if self._pending is not None else None,
            },
        )

    def _compose(self, message: str, attachments: Sequence[str | Path]) -> object:
        """The model-facing input for a turn, including attachments, through the same composition the host uses."""
        if not attachments:
            return message
        resolved = []
        for attachment in attachments:
            path = Path(attachment)
            if not path.is_absolute():
                path = Path(self._runtime_directory) / resource_path(str(path))
            resolved.append(path)
        runtime = self.runtime
        composed = self._attachments.compose(
            message,
            tuple(resolved),
            runtime.model_identifier,
            runtime.inline_image_bytes,
        )
        runtime.note_attachments(composed.paths)
        if composed.images_not_inlined:
            logger.warning(
                "%d attached image(s) were not inlined: %s does not advertise vision support. The model has the file paths and can open them with its tools.",
                composed.images_not_inlined,
                runtime.model_identifier or "the session model",
            )
        return composed.value

    async def stream(
        self,
        message: str,
        *,
        attachments: Sequence[str | Path] = (),
    ) -> AsyncIterator[TurnEventUnion]:
        """Drive a turn, yielding each event, and keep driving turns while a goal the agent set is still open."""
        async with self._turn_lock:
            # A fresh user turn starts clean: no stop from before is owed.
            self.runtime.clear_stop()
            if not self._restored:
                await self.restore()
            await self._ensure_observation_metadata()
            if self._pending is not None:
                raise RuntimeError(
                    "This session is suspended. Respond to every pending interaction and call Session.resume(), or call Session.cancel_pending()."
                )
            self._phase = SessionPhase.RUNNING
            failure = _features.compaction_failure(self.runtime)
            if failure:
                self._phase = SessionPhase.IDLE
                raise CompactionBlockedError(
                    f"Context compaction failed: {failure} Retry Session.compact() before sending more work."
                )
            # Somebody is here again, so a goal that had used its allowance gets it back.
            self.runtime.abandon_turn_retry()
            _features.restore_goal_allowance(self.runtime)
            _features.restore_task_allowance(self.runtime)
            cancelled = False
            try:
                async for event in self.runtime.stream(self._compose(message, attachments)):
                    if isinstance(event, Done) and event.stop_reason == "cancelled":
                        cancelled = True
                    if isinstance(event, Suspended):
                        self._pending = PendingTurn(
                            interactions=tuple(event.interactions),
                            plans=event.plans,
                            decisions={},
                        )
                        self._phase = SessionPhase.SUSPENDED
                    yield event
                if _features.compaction_failure(self.runtime):
                    return
                # A turn the person stopped opens no follow-up work of its own.
                if cancelled:
                    return
                async for event in self._continue_work():
                    yield event
                self.runtime.mark_turn_succeeded()
            except Exception:
                self.runtime.mark_turn_failed()
                raise
            finally:
                await self.save()
                if self._pending is None:
                    self._phase = SessionPhase.IDLE

    async def resume(
        self,
        decisions: Mapping[str, Approval] | None = None,
    ) -> AsyncIterator[TurnEventUnion]:
        """Resume a suspended turn after explicit decisions for every interaction."""
        async with self._turn_lock:
            # A fresh user turn starts clean: no stop from before is owed.
            self.runtime.clear_stop()
            if not self._restored:
                await self.restore()
            await self._ensure_observation_metadata()
            pending = self._pending
            if pending is None:
                raise RuntimeError("This session has no suspended turn.")
            for request_id, decision in (decisions or {}).items():
                if not isinstance(decision, Approval):
                    raise TypeError(f"Decision {request_id!r} must be an Approval value.")
                pending = pending.with_decision(request_id, decision)
            self._pending = pending
            if not pending.ready:
                missing = ", ".join(gate.request_id for gate in pending.remaining)
                raise RuntimeError(f"The suspended turn still needs decisions for: {missing}.")
            answers: dict[str, Any] = {}
            for gate in pending.interactions:
                decision = pending.decisions[gate.request_id]
                if gate.kind == "question":
                    answers[gate.request_id] = (
                        dict(decision.answers)
                        if decision.allow
                        else {
                            "__declined__": True,
                            "__reason__": decision.reason,
                            "__actor__": "person",
                        }
                    )
                else:
                    answers[gate.request_id] = {
                        "allow": decision.allow,
                        "reason": decision.reason,
                        "actor": "person",
                    }
            self._pending = None
            self._phase = SessionPhase.RUNNING
            cancelled = False
            try:
                async for event in self.runtime.resume_stream(dict(pending.plans), answers):
                    if isinstance(event, Done) and event.stop_reason == "cancelled":
                        cancelled = True
                    if isinstance(event, Suspended):
                        self._pending = PendingTurn(
                            interactions=tuple(event.interactions),
                            plans=event.plans,
                            decisions={},
                        )
                        self._phase = SessionPhase.SUSPENDED
                    yield event
                if _features.compaction_failure(self.runtime):
                    return
                # A turn the person stopped opens no follow-up work of its own.
                if cancelled:
                    return
                async for event in self._continue_work():
                    yield event
                self.runtime.mark_turn_succeeded()
            except Exception:
                self.runtime.mark_turn_failed()
                raise
            finally:
                await self.save()
                if self._pending is None:
                    self._phase = SessionPhase.IDLE

    async def _continue_work(self) -> AsyncIterator[TurnEventUnion]:
        """Review goals and reopen actionable tracked work through one continuation turn."""
        while True:
            # A stop is owed (or just landed): continuation turns must not erase it.
            if _features.has_pending_jobs(self.runtime) or self.runtime.stop_requested:
                return
            goal = _features.goal(self.runtime)
            continue_goal = _features.should_continue_goal(self.runtime)
            continue_tasks = _features.should_continue_tasks(self.runtime)
            if goal is not None and goal.is_open and not continue_goal:
                _features.park_goal(self.runtime)
            if not continue_goal and not continue_tasks:
                return
            goal_review_message = ""
            task_note = ""
            opens_exchange = False
            if continue_goal:
                review_events: asyncio.Queue[TurnEventUnion] = asyncio.Queue()
                review_task = asyncio.create_task(_features.review_goal(self.runtime, review_events.put))
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
                goal = _features.apply_goal_review(self.runtime, review)
                if goal is not None and goal.is_open and goal.review_message:
                    goal_review_message = goal.review_message
                    _features.note_goal_continuation(self.runtime)
                    opens_exchange = True
            if continue_tasks and _features.should_continue_tasks(self.runtime):
                task_note = _features.task_continuation_message(self.runtime, )
                _features.note_task_continuation(self.runtime)
            if not goal_review_message and not task_note:
                return
            cancelled_continuation = False
            noop_continuation = False
            saw_tool_result = False
            async for event in self.runtime.stream(
                _features.continuation_content(self.runtime, 
                    goal_review=goal_review_message,
                    task_continuation=task_note,
                ),
                as_system_note=True,
                opens_exchange=opens_exchange,
            ):
                if isinstance(event, Done) and event.stop_reason == "cancelled":
                    cancelled_continuation = True
                if isinstance(event, ToolResult):
                    saw_tool_result = True
                if (
                    isinstance(event, Done)
                    and opens_exchange
                    and not event.text.strip()
                    and not saw_tool_result
                ):
                    # A goal continuation answered the review with nothing: do not immediately re-review, or the review loop would spin.
                    noop_continuation = True
                if isinstance(event, Suspended):
                    self._pending = PendingTurn(
                        interactions=tuple(event.interactions),
                        plans=event.plans,
                        decisions={},
                    )
                    self._phase = SessionPhase.SUSPENDED
                yield event
            if (
                self._pending is not None
                or cancelled_continuation
                or noop_continuation
                or self.runtime.stop_requested
            ):
                return

    async def ask(self, message: str, *, attachments: Sequence[str | Path] = ()) -> str:
        """Drive a turn, or a goal to its end, and answer with the agent's prose."""
        from langmesh.runtime.turn_events import Done, Suspended

        answer = ""
        async for event in self.stream(message, attachments=attachments):
            if isinstance(event, Suspended):
                raise PermissionError(
                    "This turn is suspended. Inspect `session.state.pending`, call `session.respond(...)` for each interaction, then drive `session.resume()`; or supply an approver through SessionComponents."
                )
            if isinstance(event, Done):
                answer = event.text or answer
        return answer

    async def compact(self) -> AsyncIterator[TurnEventUnion]:
        """Prepare and compact the conversation now, retrying the exact failed phase when necessary."""
        async with self._turn_lock:
            # A fresh user turn starts clean: no stop from before is owed.
            self.runtime.clear_stop()
            if not self._restored:
                await self.restore()
            await self._ensure_observation_metadata()
            if self._pending is not None:
                raise RuntimeError("A suspended turn must be resumed or cancelled before compaction.")
            self._phase = SessionPhase.COMPACTING
            runtime = self.runtime
            if _features.compaction_failure(runtime):
                retry_operation = _features.retry_compaction(runtime)
            else:
                _features.begin_compaction_preparation(runtime)
                retry_operation = "prepare"
            resume_after = _features.resumes_after_compaction(runtime)
            try:
                if retry_operation == "compact":
                    source = _features.compact(runtime, reason=_features.pending_compaction_reason(runtime))
                else:
                    source = (
                        runtime.continue_stream()
                        if _features.resumes_after_compaction(runtime)
                        else runtime.prepare_compaction_stream()
                    )
                async for event in source:
                    yield event
                if retry_operation == "compact" and resume_after and not _features.compaction_failure(runtime):
                    async for event in runtime.continue_stream():
                        yield event
            finally:
                await self.save()
                self._phase = SessionPhase.IDLE

    async def retry(self) -> AsyncIterator[TurnEventUnion]:
        """Continue the last failed turn from its durable conversation state."""
        async with self._turn_lock:
            # A fresh user turn starts clean: no stop from before is owed.
            self.runtime.clear_stop()
            if not self._restored:
                await self.restore()
            await self._ensure_observation_metadata()
            if self._pending is not None:
                raise RuntimeError("A suspended turn must be resumed or cancelled before retrying.")
            self._phase = SessionPhase.RETRYING
            if _features.compaction_failure(self.runtime):
                self._phase = SessionPhase.IDLE
                raise CompactionBlockedError(
                    "Context compaction is blocked; retry Session.compact() before retrying the turn."
                )
            if not self.runtime.begin_turn_retry():
                self._phase = SessionPhase.IDLE
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
                self._phase = SessionPhase.IDLE

    @property
    def conversation(self) -> list:
        """The model-facing message list, which accumulates across turns."""
        return self.runtime.conversation

    async def aclose(self) -> None:
        """Release resources owned by this session without disturbing another session in the process."""
        if self._runtime is not None:
            with contextlib.suppress(Exception):
                self._runtime.abort()
        # Unbind what the session bound, so a caller's credentials and tracer do not outlive it.
        for kind, token in reversed(self._bindings):
            with contextlib.suppress(Exception):
                if kind == "credentials":
                    from langmesh.base.identity.credentials import reset_credentials

                    reset_credentials(token)
                else:
                    from langmesh.base.primitives.telemetry import reset_tracer

                    reset_tracer(token)
        self._bindings.clear()
        if self._runtime is not None:
            with contextlib.suppress(Exception):
                _features.background_jobs(self._runtime).cancel_all()
        await self._lifecycle.aclose()
        self._materialized_resources = None
        self._runtime = None
        self._restored = False
        self._observation_metadata_loaded = False
        self._phase = SessionPhase.IDLE
        self._pending = None
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
                        from langmesh.base.contracts.mcp_client import MCPServerManager

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
