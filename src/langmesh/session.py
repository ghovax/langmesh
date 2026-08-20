"""The embedded session lifecycle and its owned resources."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Mapping, Sequence

from langmesh.base.configuration import (
    AgentConfiguration,
    Configuration,
    SandboxConfiguration,
    ToolboxConfiguration,
)
from langmesh.base.configuration.permission_mode import PermissionMode
from langmesh.base.confinement import Profile
from langmesh.base.content.attachments import PathAttachments
from langmesh.base.contracts.ports import (
    Approval,
    Approvals,
    Attachments,
    CatalogueLike,
    Checkpoints,
    Credentials,
    FileLeases,
    JobStore,
    MCPServers,
    MemoryCheckpoints,
    MemoryJobStore,
    MemoryTranscript,
    Observer,
    PermissionPolicy,
    SessionAccess,
    Transcript,
    WorkspaceManager,
    describe_unmet,
)
from langmesh.base.contracts.tools import ToolGrant, ToolLike, as_tool_grants
from langmesh.base.persistence.observations import ObservationRegistry
from langmesh.base.persistence.resources import (
    MaterializedResources,
    WorkspaceResources,
    WorkspaceResourcesLike,
    resource_path,
)
from langmesh.base.persistence.worktrees import (
    WorktreeStrategy,
)
from langmesh.runtime.composition import RuntimeProfile, SessionComponents
from langmesh.runtime.features import PermissionsCapability
from langmesh.runtime.session_control import PendingTurn, SessionPhase, SessionState

# The vocabulary `stream()` speaks, exported because a caller driving a turn has to dispatch on it.
if TYPE_CHECKING:
    from langmesh.runtime.runtime import AgentRuntime

from langmesh.runtime.turn_events import (
    Done,
    Suspended,
    TurnEventUnion,
)

logger = logging.getLogger(__name__)


def _apply_providers(
    configuration: Configuration, providers: Mapping[str, str | Mapping[str, str]]
) -> None:
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
        sandbox: Profile | SandboxConfiguration | Mapping[str, object] | None = None,
        configuration: Configuration | None = None,
        # Provider credentials in code, though the environment variables still win.
        providers: Mapping[str, str | Mapping[str, str]] | None = None,
        model_identifier: str = "",
        tools: Sequence[ToolLike] = (),
        components: SessionComponents | None = None,
        # Any fsspec-backed workspace. Non-local sources are materialized for Bash and SQLite, then synchronized at tool boundaries and close.
        resources: WorkspaceResourcesLike | None = None,
    ) -> None:
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
        if components is None:
            components = SessionComponents()
        elif not isinstance(components, SessionComponents):
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
        self._credentials = _require(Credentials, components.credentials, "components.credentials")
        _require(PermissionPolicy, components.permissions, "components.permissions")
        _require(FileLeases, components.file_leases, "components.file_leases")
        _require(WorkspaceManager, components.workspace, "components.workspace")
        _require(SessionAccess, components.sessions, "components.sessions")
        _require(MCPServers, components.mcp_servers, "components.mcp_servers")
        self._workspace = components.workspace
        self._tracer_provider = components.tracer_provider
        self._observations = ObservationRegistry(self._resources, configuration=self._configuration)
        self._materialized_resources: MaterializedResources | None = None
        # Where tools actually run. Equal to `directory` unless a workspace repointed it.
        self._runtime_directory = self._directory
        self._runtime: AgentRuntime | None = None
        self._restored = False
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
    def runtime(self) -> AgentRuntime:
        """The underlying `AgentRuntime`, built on first use and exposed so no non-obvious use needs a fork."""
        if self._runtime is None:
            if self._materialized_resources is None and self._resources is not None:
                if self._resources.local_path is None:
                    raise RuntimeError(
                        "non-local resources require `async with Session(...)` so LangMesh can hold their materialized POSIX view"
                    )
            from langmesh.base.primitives.limits import limits_from_configuration
            from langmesh.runtime.environment import RuntimeEnvironment
            from langmesh.runtime.runtime import AgentRuntime

            tracer = None
            if self._tracer_provider is not None:
                tracer = self._tracer_provider.get_tracer("langmesh")
            environment = RuntimeEnvironment(
                limits=limits_from_configuration(self._configuration.tuning),
                credentials=self._credentials,
                tracer=tracer,
            )
            # The directory the caller supplied plus the packaged base layer, and deliberately nothing of `$HOME`.
            if self._catalogue is None:
                from langmesh.base.contracts.catalogue import project_catalogue

                catalogue = project_catalogue()
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
            with environment.bind():
                self._runtime = AgentRuntime(
                    RuntimeProfile(
                        agent=agent_configuration,
                        configuration=self._configuration,
                        session_id=self._session_id,
                        working_directory=self._runtime_directory,
                        project_directory=self._directory,
                        permission_mode=self._permission_mode,
                        sandbox=self._sandbox,
                    ),
                    self._components.for_runtime(
                        catalogue=catalogue,
                        jobs=self._jobs,
                        observer=self._observer,
                        approvals=self._approvals,
                        transcript=self._transcript,
                        mcp_servers=self._mcp_server_manager,
                        synchronize_resources=(
                            self._materialized_resources.sync
                            if self._materialized_resources is not None
                            else None
                        ),
                        features=self._components.features or [],
                        environment=environment,
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

    async def prepare_worktree(self, strategy: WorktreeStrategy = "worktree") -> str:
        """Give this session its own git worktree and run its tools there; opt-in, because it writes to disk."""
        async with self._turn_lock:
            if self._runtime is not None:
                raise RuntimeError(
                    "prepare_worktree must run before the session builds its runtime"
                )
            if not self._directory:
                raise RuntimeError("prepare_worktree requires a local directory-backed session")
            manager = self._workspace
            if manager is None:
                from langmesh.base.persistence.worktrees import SessionWorktreeManager

                manager = SessionWorktreeManager()
            prepared = await manager.prepare(self._session_id, self._directory, strategy)
            self._runtime_directory = prepared.runtime_working_directory or self._directory
            return self._runtime_directory

    def refresh_prompt(self) -> None:
        """Rebuild catalogue-derived static instructions at the next model boundary."""
        if self._runtime is not None:
            self._runtime.refresh_system_prompt()

    async def sync_resources(self) -> None:
        """Publish path-native changes to the configured fsspec workspace now."""
        async with self._turn_lock:
            if self._materialized_resources is None:
                raise RuntimeError(
                    "resources are materialized only inside `async with Session(...)`"
                )
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
                self._runtime.refresh_system_prompt()

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
            permission_mode=str(
                runtime.permission_mode if runtime is not None else self._permission_mode
            ),
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
        return bool(self._runtime and self._runtime.abort_tool(tool_call_id))

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
                await self._restore()
            if self._pending is None:
                raise RuntimeError("This session has no suspended turn.")
            self._pending = self._pending.with_decision(request_id, decision)
            await self._save()
            return self.state

    async def cancel_pending(self) -> None:
        """Abandon a suspended batch without executing any of its calls."""
        async with self._turn_lock:
            if self._pending is None:
                raise RuntimeError("This session has no suspended turn.")
            self.runtime.abandon_suspension()
            self._pending = None
            self._phase = SessionPhase.IDLE
            await self._save()

    async def set_permission_mode(self, mode: str | PermissionMode) -> SessionState:
        """Change live permission policy and re-evaluate any unanswered parked gates."""
        resolved = mode if isinstance(mode, PermissionMode) else PermissionMode.resolve(mode)
        self._permission_mode = str(resolved)
        runtime = self.runtime
        runtime.set_permission_mode(resolved)
        if self._pending is not None:
            async with self._turn_lock:
                pending = self._pending
                if pending is None:
                    return self.state
                permissions = runtime.features.require(PermissionsCapability)
                for gate in pending.remaining:
                    verdict = await permissions.reconsider_gate(gate)
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
                await self._save()
        return self.state

    async def restore(self) -> bool:
        """Reload this session's conversation from its checkpoint store, and say whether anything was found."""
        async with self._turn_lock:
            return await self._restore()

    async def _restore(self) -> bool:
        state = await self._checkpoints.load(self._session_id)
        if not state:
            self._restored = True
            return False
        messages = state.get("conversation") or []
        session_state = state.get("session") or {}
        pending_state = state.get("pending")
        if not messages and not session_state and not pending_state:
            self._restored = True
            return False
        from langchain_core.messages import messages_from_dict

        self.runtime.conversation[:] = messages_from_dict(messages)
        if session_state:
            self.runtime.restore_session(session_state)
        if isinstance(pending_state, Mapping):
            self._pending = PendingTurn.restore(pending_state)
            self._phase = SessionPhase.SUSPENDED
        self._restored = True
        return True

    async def save(self) -> None:
        """Write this session's conversation to its checkpoint store through LangChain's message codec."""
        async with self._turn_lock:
            await self._save()

    async def _save(self) -> None:
        from langchain_core.messages import message_to_dict

        await self._checkpoints.save(
            self._session_id,
            {
                "conversation": [message_to_dict(message) for message in self.conversation],
                "session": self.runtime.session_snapshot(),
                "pending": self._pending.snapshot() if self._pending is not None else None,
            },
        )

    def _compose(
        self, message: str, attachments: Sequence[str | Path]
    ) -> str | list[dict[str, Any]]:
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
        runtime.note_attachments(composed.granted_paths)
        if composed.omitted_image_count:
            logger.warning(
                "%d attached image(s) were not inlined for %s because the model lacks image support, the file exceeds the inline limit, or the file could not be read; the paths remain available to tools.",
                composed.omitted_image_count,
                runtime.model_identifier or "the session model",
            )
        return composed.content

    async def stream(
        self,
        message: str,
        *,
        attachments: Sequence[str | Path] = (),
    ) -> AsyncIterator[TurnEventUnion]:
        """Drive a turn, yielding each event."""
        async with self._turn_lock:
            # A fresh user turn starts clean: no stop from before is owed.
            self.runtime.clear_stop()
            if not self._restored:
                await self._restore()
            if self._pending is not None:
                raise RuntimeError(
                    "This session is suspended. Respond to every pending interaction and call Session.resume(), or call Session.cancel_pending()."
                )
            self._phase = SessionPhase.RUNNING
            self.runtime.abandon_turn_retry()
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
                # A turn the person stopped opens no follow-up work of its own.
                if cancelled:
                    return
                self.runtime.mark_turn_succeeded()
            except Exception:
                self.runtime.mark_turn_failed()
                raise
            finally:
                await self._save()
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
                await self._restore()
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
                # A turn the person stopped opens no follow-up work of its own.
                if cancelled:
                    return
                self.runtime.mark_turn_succeeded()
            except Exception:
                self.runtime.mark_turn_failed()
                raise
            finally:
                await self._save()
                if self._pending is None:
                    self._phase = SessionPhase.IDLE

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

    async def retry(self) -> AsyncIterator[TurnEventUnion]:
        """Continue the last failed turn from its durable conversation state."""
        async with self._turn_lock:
            # A fresh user turn starts clean: no stop from before is owed.
            self.runtime.clear_stop()
            if not self._restored:
                await self._restore()
            if self._pending is not None:
                raise RuntimeError("A suspended turn must be resumed or cancelled before retrying.")
            self._phase = SessionPhase.RETRYING
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
                await self._save()
                self._phase = SessionPhase.IDLE

    @property
    def conversation(self) -> tuple[Any, ...]:
        """A fixed sequence snapshot of the model-facing messages accumulated across turns."""
        return tuple(self.runtime.conversation)

    async def aclose(self) -> None:
        """Release resources owned by this session without disturbing another session in the process."""
        if self._runtime is not None:
            with contextlib.suppress(Exception):
                self._runtime.abort()
        await self._lifecycle.aclose()
        self._materialized_resources = None
        self._runtime = None
        self._restored = False
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
