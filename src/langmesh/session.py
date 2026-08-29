"""The embedded session lifecycle and its owned resources."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Mapping, Sequence

from langmesh.base.configuration import (
    AgentConfiguration,
    SandboxConfiguration,
)
from langmesh.base.configuration.permission_mode import PermissionMode
from langmesh.base.confinement import Profile
from langmesh.base.content.attachments import Attachment, AttachmentComposer
from langmesh.base.contracts.ports import (
    Approval,
    Approvals,
    Artifacts,
    Attachments,
    CatalogueLike,
    Checkpoints,
    CredentialStore,
    FileLeases,
    JobStore,
    MCPServers,
    MemoryCheckpoints,
    MemoryArtifacts,
    MemoryJobStore,
    MemoryTranscript,
    Observer,
    PermissionPolicy,
    SessionAccess,
    Transcript,
    describe_unmet,
)
from langmesh.base.contracts.tools import ToolLike
from langmesh.runtime.composition import RuntimeProfile, SessionComponents
from langmesh.runtime.session_control import (
    PendingTurn,
    SessionCheckpoint,
    SessionPhase,
    SessionSnapshot,
    SessionState,
)

# The vocabulary `stream()` speaks, exported because a caller driving a turn has to dispatch on it.
if TYPE_CHECKING:
    from langmesh.runtime.runtime import AgentRuntime

from langmesh.runtime.turn_events import (
    Checkpoint,
    Done,
    Suspended,
    TurnEventUnion,
)

logger = logging.getLogger(__name__)


def _provider_inputs(
    providers: Mapping[str, str | Mapping[str, str]] | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Split caller-supplied provider values into credentials and endpoint overrides."""
    keys: dict[str, str] = {}
    bases: dict[str, str] = {}
    for provider_identifier, value in (providers or {}).items():
        if isinstance(value, str):
            keys[provider_identifier] = value
            continue
        if value.get("api_key"):
            keys[provider_identifier] = value["api_key"]
        if value.get("base_url"):
            bases[provider_identifier] = value["base_url"]
    return keys, bases


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
        directory: str | Path,
        session_id: str = "",
        permission_mode: str = "",
        sandbox: Profile | SandboxConfiguration | Mapping[str, object] | None = None,
        # Provider credentials in code. Secret files fill empty slots at resolve time.
        providers: Mapping[str, str | Mapping[str, str]] | None = None,
        model_identifier: str = "",
        tools: Sequence[ToolLike] = (),
        limits: Any = None,
        components: SessionComponents | None = None,
    ) -> None:
        from langmesh.base.primitives.identifiers import new_id

        if isinstance(agent, str):
            raise TypeError(
                "agent must be an AgentConfiguration, not a name. A name would mean this library goes looking for a profile on your machine. Build one in code, or load your own catalogue."
            )
        self._agent = agent
        # Absolute, and not resolved against the process's directory: where tools run is a property of the run.
        if not Path(directory).is_absolute():
            raise ValueError(f"directory must be absolute, got {directory!r}.")
        self._directory = str(directory)
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
                application_tools=[*components.application_tools, *tools],
            )
        self._components = components
        # Live grants remain caller-owned code and are rebound whenever this session rebuilds its runtime.
        self._granted_tools: dict[str, ToolLike] = {}
        self._mcp_server_manager = components.mcp_servers
        self._provider_api_keys, self._provider_base_urls = _provider_inputs(providers)
        self._limits = limits
        self._model_identifier = model_identifier
        self._catalogue = _require(CatalogueLike, components.catalogue, "components.catalogue")
        self._checkpoints = (
            _require(Checkpoints, components.checkpoints, "components.checkpoints")
            or MemoryCheckpoints()
        )
        self._attachments = (
            _require(Attachments, components.attachments, "components.attachments")
            or AttachmentComposer()
        )
        self._jobs = _require(JobStore, components.jobs, "components.jobs") or MemoryJobStore()
        self._observer = _require(Observer, components.observer, "components.observer")
        self._approvals = _require(Approvals, components.approvals, "components.approvals")
        self._artifacts = (
            _require(Artifacts, components.artifacts, "components.artifacts") or MemoryArtifacts()
        )
        self._transcript = (
            _require(Transcript, components.transcript, "components.transcript")
            or MemoryTranscript()
        )
        self._credential_store = _require(
            CredentialStore, components.credential_store, "components.credential_store"
        )
        _require(PermissionPolicy, components.permissions, "components.permissions")
        _require(FileLeases, components.file_leases, "components.file_leases")
        _require(SessionAccess, components.sessions, "components.sessions")
        _require(MCPServers, components.mcp_servers, "components.mcp_servers")
        self._tracer_provider = components.tracer_provider
        self._runtime: AgentRuntime | None = None
        self._restored = False
        self._refresh_prompt_on_restore = False
        self._turn_lock = asyncio.Lock()
        self._checkpoint_lock = asyncio.Lock()
        self._phase = SessionPhase.IDLE
        self._pending: PendingTurn | None = None

    @property
    def id(self) -> str:
        """This session's identity, which is what a checkpoint is keyed by."""
        return self._session_id

    @property
    def runtime(self) -> AgentRuntime:
        """The underlying `AgentRuntime`, built on first use and exposed so no non-obvious use needs a fork."""
        if self._runtime is None:
            from langmesh.runtime.environment import RuntimeEnvironment
            from langmesh.runtime.runtime import AgentRuntime

            tracer = None
            if self._tracer_provider is not None:
                tracer = self._tracer_provider.get_tracer("langmesh")
            environment = RuntimeEnvironment(
                limits=self._limits,
                credentials=self._credential_store,
                tracer=tracer,
            )
            # A bare session uses only caller-supplied values and the packaged prompt layer.
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
                        session_id=self._session_id,
                        working_directory=self._directory,
                        project_directory=self._directory,
                        permission_mode=self._permission_mode,
                        sandbox=self._sandbox,
                        workspace_strategy="none",
                    ),
                    self._components.for_runtime(
                        catalogue=catalogue,
                        jobs=self._jobs,
                        observer=self._observer,
                        approvals=self._approvals,
                        artifacts=self._artifacts,
                        transcript=self._transcript,
                        mcp_servers=self._mcp_server_manager,
                        features=self._components.features or [],
                        environment=environment,
                        provider_api_keys=self._provider_api_keys,
                        provider_base_urls=self._provider_base_urls,
                    ),
                )
            for tool in self._granted_tools.values():
                self._runtime.grant_tool(tool)
        return self._runtime

    def grant_tool(self, tool: ToolLike) -> None:
        """Grant a real provider-visible tool, replacing one of the same name when present."""
        self._granted_tools[tool.name] = tool
        if self._runtime is not None:
            self._runtime.grant_tool(tool)

    def refresh_prompt(self) -> None:
        """Rebuild catalogue-derived static instructions at the next model boundary."""
        if not self._restored:
            self._refresh_prompt_on_restore = True
        if self._runtime is not None:
            self._runtime.refresh_system_prompt()

    @property
    def transcript(self) -> Transcript:
        """The record of this session's turns."""
        return self._transcript

    @property
    def artifacts(self) -> Artifacts:
        """The complete tool outputs stored for this session."""
        return self._artifacts

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

    @property
    def snapshot(self) -> SessionSnapshot:
        """The current durable runtime state as documented fields."""
        return self.runtime.session_snapshot()

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
            if not self._restored:
                await self._restore()
            if self._pending is None:
                raise RuntimeError("This session has no suspended turn.")
            self.runtime.abandon_suspension()
            self._pending = None
            self._phase = SessionPhase.IDLE
            await self._save()

    async def set_permission_mode(self, mode: str | PermissionMode) -> SessionState:
        """Persist live permission policy before applying it and re-evaluate parked gates."""
        resolved = mode if isinstance(mode, PermissionMode) else PermissionMode.resolve(mode)
        if not self._restored:
            async with self._turn_lock:
                if not self._restored:
                    await self._restore()
        await self._save(permission_mode=resolved)
        self._permission_mode = str(resolved)
        runtime = self.runtime
        runtime.set_permission_mode(resolved)
        if self._pending is not None:
            async with self._turn_lock:
                pending = self._pending
                if pending is None:
                    return self.state
                for gate in pending.remaining:
                    verdict = await runtime.features.reconsider_gate(gate)
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
        checkpoint = await self._checkpoints.load(self._session_id)
        if checkpoint is None:
            self._restored = True
            if self._refresh_prompt_on_restore:
                self.runtime.refresh_system_prompt()
                self._refresh_prompt_on_restore = False
            return False
        if not isinstance(checkpoint, SessionCheckpoint):
            raise TypeError("checkpoint adapters must return a SessionCheckpoint value")
        if (
            not checkpoint.conversation
            and checkpoint.session == SessionSnapshot()
            and checkpoint.pending is None
        ):
            self._restored = True
            return False
        from langchain_core.messages import ToolMessage, messages_from_dict

        from langmesh.base.primitives.limits import (
            bind_limits,
            Limits,
            reset_limits,
        )
        from langmesh.runtime.internals import _cap_model_result_payload

        restored_messages = messages_from_dict(list(checkpoint.conversation))
        limits_token = bind_limits(self._limits or Limits())
        try:
            for message in restored_messages:
                if not isinstance(message, ToolMessage):
                    continue
                if isinstance(message.content, str):
                    message.content = _cap_model_result_payload(message.content)
                if isinstance(message.artifact, dict):
                    message.artifact.pop("result", None)
        finally:
            reset_limits(limits_token)
        self.runtime.conversation[:] = restored_messages
        self.runtime.restore_session(checkpoint.session)
        if checkpoint.session.permission_mode:
            self._permission_mode = checkpoint.session.permission_mode
        if checkpoint.pending is not None:
            self._pending = checkpoint.pending
            self._phase = SessionPhase.SUSPENDED
        self._restored = True
        if self._refresh_prompt_on_restore:
            self.runtime.refresh_system_prompt()
            self._refresh_prompt_on_restore = False
        return True

    async def save(self) -> None:
        """Write this session's conversation to its checkpoint store through LangChain's message codec."""
        async with self._turn_lock:
            if not self._restored:
                await self._restore()
            await self._save()

    async def _save(self, *, permission_mode: PermissionMode | None = None) -> None:
        from langchain_core.messages import message_to_dict

        async with self._checkpoint_lock:
            snapshot = self.runtime.session_snapshot()
            if permission_mode is not None:
                snapshot = dataclasses.replace(snapshot, permission_mode=str(permission_mode))
            await self._checkpoints.save(
                self._session_id,
                SessionCheckpoint(
                    conversation=tuple(message_to_dict(message) for message in self.conversation),
                    session=snapshot,
                    pending=self._pending,
                ),
            )

    def _compose(
        self, message: str, attachments: Sequence[Attachment]
    ) -> str | list[dict[str, Any]]:
        """The model-facing input for a turn, including attachments, through the same composition the host uses."""
        if not attachments:
            return message
        runtime = self.runtime
        composed = self._attachments.compose(
            message,
            tuple(attachments),
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
        attachments: Sequence[Attachment] = (),
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
                    if isinstance(event, Checkpoint):
                        await self._save()
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
                    if isinstance(event, Checkpoint):
                        await self._save()
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

    async def ask(self, message: str, *, attachments: Sequence[Attachment] = ()) -> str:
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
                    if isinstance(event, Checkpoint):
                        await self._save()
                    yield event
                self.runtime.mark_turn_succeeded()
            except Exception:
                self.runtime.mark_turn_failed()
                raise
            finally:
                await self._save()
                self._phase = SessionPhase.IDLE

    async def retry_maintenance(self) -> AsyncIterator[TurnEventUnion]:
        """Retry failed context maintenance without accepting another user message."""
        async with self._turn_lock:
            self.runtime.clear_stop()
            if not self._restored:
                await self._restore()
            if self._pending is not None:
                raise RuntimeError(
                    "A suspended turn must be resumed or cancelled before retrying maintenance."
                )
            if self.runtime.features.retry_maintenance() is None:
                raise RuntimeError("This session has no failed maintenance to retry.")
            self._phase = SessionPhase.COMPACTING
            try:
                async for event in self.runtime.prepare_maintenance_stream():
                    if isinstance(event, Checkpoint):
                        await self._save()
                    yield event
            finally:
                await self._save()
                self._phase = SessionPhase.IDLE

    @property
    def conversation(self) -> tuple[Any, ...]:
        """A fixed sequence snapshot of the model-facing messages accumulated across turns."""
        return tuple(self.runtime.conversation)

    async def aclose(self) -> None:
        """Checkpoint this session and release its in-process runtime."""
        runtime = self._runtime
        if runtime is None:
            return
        runtime.interrupt_for_restart()
        async with self._turn_lock:
            if not self._restored:
                await self._restore()
            await self._save()
            self._runtime = None
            self._restored = False
            self._phase = SessionPhase.IDLE
            self._pending = None

    async def __aenter__(self) -> "Session":
        return self

    async def __aexit__(self, *_exception: object) -> None:
        await self.aclose()
