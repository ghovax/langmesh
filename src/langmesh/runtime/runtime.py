from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from langchain_core.messages import (
    AIMessage,
    messages_to_dict,
)
from langchain_core.tools import BaseTool
from pydantic import SecretStr

from langmesh.base import confinement as _confinement
from langmesh.base.confinement import Grant, Profile
from langmesh.base.configuration import (
    AgentConfiguration,
    Configuration,
    PermissionEvaluator,
    SandboxConfiguration,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langmesh.runtime.models.litellm import ChatLiteLLMModel
from langmesh.runtime.models.codex import ChatCodexModel
from langmesh.runtime.models.cursor import ChatCursorModel
from langmesh.base.content.models import find_model, resolve_litellm
from langmesh.base.contracts.tools import as_tool_grants
from langmesh.base.contracts.catalogue import project_catalogue
from langmesh.runtime.tools.arguments import with_explanation
from langmesh.runtime.tools.execution import Tool, ToolServices, invoke_supplied
from langmesh.runtime.tools import registry as tools_registry
from langmesh.runtime.tools.handlers import HANDLERS
from langmesh.locations.resolver import LocationAddress, executor_for, location_uri_for
from langmesh.base.contracts.ports import Observation
from langmesh.runtime.tools.context import ToolContext

from langmesh.base.configuration.permission_mode import PermissionMode

from langmesh.runtime.locations import CallExecutionPolicy, Location, ResolvedLocation, ToolLocationError
from langmesh.runtime.turn_events import (
    TurnEvent,
    Usage,
)

from langmesh.runtime.tools.dispatch import (
    _DispatchesTools,
)

from langmesh.runtime.turn import (
    _RunsTurns,
)

from langmesh.base.primitives.serialization import compact
from langmesh.base.content.toolbox import toolbox_for
from langmesh.runtime.composition import RuntimeComponents, RuntimeProfile
from langmesh.runtime.features import (
    BoundaryView,
    BookkeepingView,
    ConversationView,
    PluginBus,
    PluginContext,
    PluginHost,
    ToolsView,
    TurnView,
    WindowView,
    build_features,
    feature_prompts,
)
from langmesh.runtime.hooks import HookRunner
from langmesh.runtime.pipeline import ToolPipeline
from langmesh.runtime.internals import (
    _utc_timestamp,
    conversation_tokens,
)

logger = logging.getLogger(__name__)

async def _drain_observer(pending) -> None:
    """Await a caller-supplied audit observer without making it part of turn control flow."""
    try:
        await pending
    except Exception:  # noqa: BLE001 — an audit sink must never fail a turn
        logger.debug("an asynchronous audit observer raised", exc_info=True)

class _CataloguePrompts:
    """A `PromptLoader`-shaped view of a catalogue, so the template seam cost one adapter rather than a rewrite."""

    def __init__(self, catalogue: Any) -> None:
        self._catalogue = catalogue

    def load(self, template_name: str, variables: dict[str, str]) -> str:
        return self._catalogue.prompt(template_name, variables)

def build_chat_model(
    model_identifier: str,
    global_configuration: Configuration,
    agent_configuration: AgentConfiguration,
    working_directory: str,
    session_id: str = "",
) -> BaseChatModel:
    """Build the chat model for a ``provider/model`` id: LiteLLM for almost all, and the two OAuth providers apart."""
    provider_identifier, model_suffix = model_identifier.split("/", 1)
    if provider_identifier == "chatgpt":
        catalog_entry = find_model(model_identifier)
        return ChatCodexModel(
            model=model_suffix,
            reasoning_effort=agent_configuration.reasoning_effort,
            context_length=catalog_entry.context_length if catalog_entry else 0,
            session_id=session_id,
        )
    if provider_identifier == "cursor":
        catalog_entry = find_model(model_identifier)
        # No reasoning_effort: a Cursor model id carries its effort, so a second setting could only disagree.
        return ChatCursorModel(
            model=model_suffix,
            workspace=working_directory,
            context_length=catalog_entry.context_length if catalog_entry else 0,
        )
    resolved = resolve_litellm(
        model_identifier,
        global_configuration.configured_provider_keys(),
        global_configuration.configured_provider_bases(),
    )
    # The catalogue's window travels with the model, since LiteLLM knows nothing of a gateway's models.
    catalogued = find_model(model_identifier)
    return ChatLiteLLMModel.model_validate(
        {
            "model": resolved["model"],
            "api_key": SecretStr(resolved["api_key"]) if resolved["api_key"] else None,
            "api_base": resolved["api_base"] or None,
            "session_id": session_id,
            "context_length": catalogued.context_length if catalogued else 0,
            "temperature": 0,
            "reasoning_effort": agent_configuration.reasoning_effort,
        }
    )

def _as_profile(sandbox: Any):
    """Whatever a caller called a sandbox, as the :class:`Profile` the runtime works with."""
    if sandbox is None:
        # The configured default, not `Profile()`: an empty writable set means "may write nowhere".
        return SandboxConfiguration().to_profile()
    if isinstance(sandbox, Profile):
        return sandbox
    if isinstance(sandbox, dict):
        return Profile.from_dict(sandbox)
    to_profile = getattr(sandbox, "to_profile", None)
    if callable(to_profile):
        return to_profile()
    raise TypeError(
        f"sandbox must be a confinement Profile, a SandboxConfiguration, or the dict form of either — got {type(sandbox).__name__}."
    )

def _build_tool_context(
    global_configuration: Configuration,
    *,
    sandbox,
    workspace: str,
    session_id: str = "",
    session_access: Any = None,
    conversation_snapshot: Optional[Callable[[], list[dict[str, Any]]]] = None,
    mcp_server_manager: Any = None,
) -> ToolContext:
    """The session-shaped state this runtime's tools read, derived from configuration rather than installed."""
    # The session's own tools, and the one widening that goes with them: it cannot install where it may not write.
    toolbox = toolbox_for(session_id, enabled=global_configuration.toolbox.enabled)
    if toolbox is not None:
        toolbox.prepare()
        sandbox = sandbox.with_grant(
            _confinement.approved(
                _confinement.AccessRequest(mutates=True, writes=(str(toolbox.root),)),
                by=_confinement.APPROVED_BY_RULE,
                purpose="the session's own toolbox directory",
            ),
            workspace=workspace,
        )

    exa_client = None
    exa_key = global_configuration.exa.effective_api_key
    if exa_key:
        from exa_py import Exa

        exa_client = Exa(api_key=exa_key)

    firecrawl_client = None
    firecrawl_key = global_configuration.firecrawl.effective_api_key
    if firecrawl_key:
        from firecrawl import AsyncFirecrawl

        api_url = global_configuration.firecrawl.effective_api_url
        firecrawl_client = (
            AsyncFirecrawl(api_key=firecrawl_key, api_url=api_url)
            if api_url
            else AsyncFirecrawl(api_key=firecrawl_key)
        )

    return ToolContext(
        sandbox=sandbox,
        workspace=workspace,
        exa_client=exa_client,
        mcp_server_manager=mcp_server_manager,
        firecrawl_client=firecrawl_client,
        jina_api_key=global_configuration.jina.effective_api_key,
        proxy_url=global_configuration.web_fetch.effective_proxy_url,
        fetch_timeout_seconds=global_configuration.web_fetch.timeout_seconds,
        download_timeout_seconds=global_configuration.web_fetch.download_timeout_seconds,
        minimum_useful_characters=global_configuration.web_fetch.minimum_useful_characters,
        session_access=session_access,
        conversation_snapshot=conversation_snapshot,
        session_id=session_id,
        toolbox=toolbox,
    )

class _LeaseAccess:
    """The filesystem-lease surface a tool handler may hold across an operation."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    async def acquire(self, *, scope: str, path: str, description: str, working_directory: str) -> str:
        return await self._runtime._acquire_filesystem_lease(
            scope=scope, path=path, description=description, working_directory=working_directory
        )

    def release(self, token: str) -> None:
        self._runtime._release_filesystem_lease(token)

    def canonical_working_directory(self, directory: str) -> str:
        return self._runtime._canonical_working_directory(directory)

class AgentRuntime(
    _DispatchesTools, _RunsTurns
):
    # A turn runs until the model is done or the user interrupts: no ceiling and no stuck-detector.

    def __init__(
        self,
        profile: RuntimeProfile,
        components: RuntimeComponents = RuntimeComponents(),
        *,
        conversation: Optional[list] = None,
    ):
        agent_configuration = profile.agent
        global_configuration = profile.configuration
        session_id = profile.session_id
        working_directory = profile.working_directory
        project_directory = profile.project_directory
        session_access = components.sessions
        mcp_server_manager = components.mcp_servers
        model = components.model
        observer = components.observer
        approvals = components.approvals
        catalogue = components.catalogue
        transcript = components.transcript
        tools = components.tools
        permissions = components.permissions
        toolset = components.toolset

        self._components = components
        self._prompt_composer = components.prompt_composer
        self._hooks = HookRunner(components.hooks)
        self._pipeline = ToolPipeline(components.middleware)
        self._resource_sync = components.synchronize_resources
        self._session_id = session_id
        # The session that created this one, empty when a person did. Reporting back needs its id.
        self._parent_session = profile.parent_session
        # What every child is confined to, held so a configuration edit cannot widen a live session.

        # Normalised once, because callers hand this three different shapes.
        self._sandbox = _as_profile(profile.sandbox)
        self._agent_configuration = agent_configuration
        self._global_configuration = global_configuration
        self._working_directory = working_directory or str(Path.home())
        self._project_directory = project_directory or self._working_directory
        # The host already resolved the session mode; a direct library caller falls back to the profile.
        self._permission_mode = PermissionMode.resolve(
            profile.permission_mode, agent_configuration.permission_default
        )
        # The locations the agent may address, with a local one synthesized when none were supplied.
        self._locations: dict[str, ResolvedLocation] = {}
        self._locations_by_name: dict[str, ResolvedLocation] = {}
        self._build_locations(profile.locations)

        model_identifier = agent_configuration.model_identifier
        # Only a runtime that must build a client needs to be told which one.
        if not model_identifier and model is None:
            raise ValueError(
                f"Agent '{agent_configuration.identifier}' names no model. Set `provider` and `model` in its profile, pass `model_identifier=\"provider/model\"` to `langmesh.Session`, or hand the runtime a `model=` of your own."
            )

        # A caller's own model wins, since accepting `BaseChatModel` is the whole of the model seam.
        self._model = (
            model
            if model is not None
            else build_chat_model(
                model_identifier,
                global_configuration,
                agent_configuration,
                self._working_directory,
                session_id,
            )
        )

        self._file_lease_manager = components.file_leases
        # The caller's tools, granted to this session. A grant is dispatchable and its description is appended to the conversation as a message, so the bound schema — and the provider cache prefix — never changes. A grant may therefore be added at creation or at any later moment; both are append-only.
        self._tool_grants = tuple(as_tool_grants(tools))
        # What a caller's tool is gated at: asking by default, so adding one cannot silently widen a session.
        self._supplied_tool_gate = components.supplied_tool_gate
        # The session's tools are composed by the caller, never forced: the complete roster comes from `toolset`, additions from `tools`/`grant_tool`, and nothing is injected by default.
        configured_tools = list(toolset) if toolset is not None else []
        # Every tool a session runs carries the shared `explanation` field, added here once.
        configured_tools = [with_explanation(tool) for tool in configured_tools]
        # The dispatchable units: every tool the session runs, assembled from the caller's set and the caller's own tools. A caller's tool of the same name replaces a built-in's execution. The model binds the configured schemas; grants ride as appended messages and only change who executes, keeping the cache prefix untouched.
        units: dict[str, Tool] = {}
        for tool in configured_tools:
            # A built-in keeps its event-rich handler only when it is the registry's own schema; a caller's tool of the same name is theirs to run through the generic invoke path.
            handler = (
                HANDLERS.get(tool.name, invoke_supplied)
                if tool is getattr(tools_registry, tool.name, None)
                else invoke_supplied
            )
            units[tool.name] = Tool(
                name=tool.name,
                schema=tool,
                description=tool.description or "",
                handler=handler,
            )
        for grant in self._tool_grants:
            units[grant.tool.name] = Tool(
                name=grant.tool.name,
                schema=grant.tool,
                description=(grant.tool.description or ""),
                handler=invoke_supplied,
            )
        self._tool_units = units
        # The caller's own tools, for the gate and for replacing a built-in's execution.
        self._supplied_tool_names = {grant.tool.name for grant in self._tool_grants}
        # Executable set (for gating, validation and direct invocation): configured plus grants, grants win.
        self._tools = [
            tool for tool in configured_tools if tool.name not in self._supplied_tool_names
        ] + [grant.tool for grant in self._tool_grants]
        self._model_tools = list(configured_tools)
        self._tool_schemas: dict[str, Any] = {tool.name: tool.args_schema for tool in self._tools}
        self._bound_model = self._model.bind_tools(self._model_tools)
        # The evaluator's own `tools_enabled` gate refuses what the profile did not declare.
        self._permissions = (
            permissions
            if permissions is not None
            else PermissionEvaluator(agent_configuration)
        )
        # Where the audit trail goes, and who answers a gate. Both absent by default.
        self._observer = observer
        self._approvals = approvals
        self._transcript = transcript
        # The conversation and the prompt this runtime runs with.

        self._conversation: list = conversation if conversation is not None else []
        self._system_prompt = agent_configuration.system_prompt
        # Files read this session, by location and path with their hash, so a stale edit is rejected.
        self._abort_event = asyncio.Event()
        # A stop is owed until a genuinely fresh turn clears it; steering must not erase it.
        self._stop_requested = False
        # Running token totals, summed from the usage each model call reports.
        self._token_usage: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cache_read_tokens": 0,
            # What a cache could have returned, since the read alone has no honest denominator.
            "reachable_tokens": 0,
            "reasoning_tokens": 0,
            "model_calls": 0,
        }

        # Where the prompt's material comes from, supplied rather than found by walking hardcoded paths.
        # The library default discovers no skills on disk: they are voluntary, injected by the caller.
        if catalogue is None:
            catalogue = project_catalogue(global_configuration, self._project_directory)
        self._catalogue = catalogue
        self._prompt_loader = _CataloguePrompts(catalogue)
        # Creation-time grants are described from the first turn: their messages sit at the head of the conversation, before any user message, and are stable for the session's life.
        for grant in self._tool_grants:
            self._conversation.append(self._tool_grant_message(grant.tool))
        self._cached_system_prompt: str | None = None
        self._session_revision = 0
        self._persisted_session_revision = 0
        self._execution_history: list[dict] = []
        # The permission policy as one value, resolved by the host before this runtime is built.
        self._a2a_turn_id: str = ""
        # Reads another task by id from the shared store, so context-aware agents can coordinate.
        self._turn_reader: Optional[Callable] = components.related_turns
        # Steering is a plain FIFO drained at the model boundary, never a queue raced against the stream.
        self._pending_steering: list[tuple[str, str, str, asyncio.Future[bool]]] = []
        self._active_tool_tasks: dict[str, asyncio.Task] = {}
        # The latest call replaces this estimate once usage arrives; restored sessions need it immediately.
        self._latest_context_tokens = conversation_tokens(self._conversation)
        context_window = getattr(self._model, "context_window", None)
        reported_context_window = max(0, int(context_window())) if callable(context_window) else 0
        # Every model must advertise its own context capacity; an unknown window means the harness cannot schedule compacting or refuse an oversized request with numbers.
        self._context_window_estimated = reported_context_window == 0
        self._context_window = reported_context_window
        self._turn_recovery = "none"
        # What the module-level tools read at call time, built from this runtime's own configuration and conversation.
        self._tool_context = _build_tool_context(
            global_configuration,
            sandbox=self._sandbox,
            workspace=self._working_directory,
            session_id=self._session_id,
            session_access=session_access,
            conversation_snapshot=self._peer_conversation_snapshot,
            mcp_server_manager=mcp_server_manager,
        )
        # What was approved beyond the configured profile. The boundary is the core's; only the permission plugin adds to it, so other plugins never need to know that plugin exists.
        self._access_grants: list[Grant] = []
        # The files the person attached: like a grant, but answering what they handed over rather than what was asked.
        self._attached_files: dict[str, None] = {}
        # The pluggable sub-behaviors this runtime runs, each with its own state and templates.
        self._plugin_bus = PluginBus()
        self._plugin_context = PluginContext(
            session_id=self._session_id,
            parent_session=self._parent_session,
            working_directory=self._working_directory,
            project_directory=self._project_directory,
            agent_configuration=self._agent_configuration,
            global_configuration=self._global_configuration,
            catalogue=self._catalogue,
            prompts=lambda name: feature_prompts(name, self._catalogue),
            bus=self._plugin_bus,
        )
        plugin_host = PluginHost(
            conversation=ConversationView(
                model=self._model,
                messages=self._conversation,
            ),
            boundary=BoundaryView(
                sandbox=self._sandbox,
                locations=self._locations,
                locations_by_name=self._locations_by_name,
                resolve_location=self._resolve_location,
                call_policy=self._call_policy,
                granted_profile=self._granted_profile,
                access_grants=lambda: self._access_grants,
                record_grant=self._record_grant,
                attached_files=self._attached_files,
            ),
            tools=ToolsView(
                tool_context=self._tool_context,
                model_tools=self._model_tools,
                tool_schemas=self._tool_schemas,
                supplied_tool_names=self._supplied_tool_names,
                supplied_tool_gate=self._supplied_tool_gate,
                turn_reader=self._turn_reader,
            ),
            window=WindowView(
                context_window=self._context_window,
                latest_context_tokens=self._latest_context_tokens,
                set_latest_context_tokens=lambda value: setattr(self, "_latest_context_tokens", value),
                refresh_cached_prompt=lambda: setattr(self, "_cached_system_prompt", None),
            ),
            turn=TurnView(
                abort_event=self._abort_event,
                discard_pending_steering=self.discard_pending_steering,
                build_static_system_prompt=self._build_static_system_prompt,
                build_turn_messages=self._build_turn_messages,
                refuse_if_over_window=self._refuse_if_over_window,
                reminder_message=self._reminder_message,
                maintenance_active=lambda: bool(self._features.active_maintenance()),
                feature_classes=lambda *exclusions: [
                    type(feature)
                    for feature in self._features.instances
                    if type(feature) not in exclusions
                ],
            ),
            bookkeeping=BookkeepingView(
                note_state_changed=self._note_session_changed,
                record_event=self._record_event,
                session_snapshot=self.session_snapshot,
                restore_session=self.restore_session,
            ),
        )
        self._features = build_features(components.features, self._plugin_context, plugin_host)
        # Features may contribute tools of their own (bash, computer use, ...); bind them to the
        # model and make them executable alongside the configured roster. A feature that answers
        # the `tool_handler` capability supplies the tool's event-rich handler; the generic path
        # runs the rest. The core never names a tool's owning feature.
        contributed = [with_explanation(tool) for tool in self._features.contributed_tools()]
        if contributed:
            contributed_names = {tool.name for tool in contributed}
            self._tools = [
                tool
                for tool in self._tools
                if tool.name not in contributed_names
            ] + list(contributed)
            self._model_tools = [
                tool
                for tool in self._model_tools
                if tool.name not in contributed_names
            ] + list(contributed)
            self._tool_schemas.update({tool.name: tool.args_schema for tool in contributed})
            for tool in contributed:
                handler = self._features.invoke("tool_handler", tool.name) or invoke_supplied
                self._tool_units[tool.name] = Tool(
                    name=tool.name,
                    schema=tool,
                    description=tool.description or "",
                    handler=handler,
                )
            self._bound_model = self._model.bind_tools(self._model_tools)
        # The services bundle every tool handler runs against: the tool's only view of the runtime.
        # Plugin capabilities are reached through the opaque features handle, never by class.
        self._services = ToolServices(
            features=self._features,
            permissions=self._permissions,
            prompt_loader=self._prompt_loader,
            catalogue=self._catalogue,
            tool_context=self._tool_context,
            access_grants=self._access_grants,
            attached_files=self._attached_files,
            turn_reader=self._turn_reader,
            record_event=self._record_event,
            note_state_changed=self._note_session_changed,
            abort_event=self._abort_event,
            leases=_LeaseAccess(self),
            pipeline=self._pipeline,
            tools=lambda: self._tools,
            project_directory=self._project_directory or "",
        )

    def note_attachments(self, paths: Sequence[str]) -> None:
        """Record attached files so a tool may read them where they live. Additive across the conversation."""
        for path in paths:
            if path:
                self._attached_files.setdefault(path, None)

    def refresh_system_prompt(self) -> None:
        """Rebuild catalogue-derived prompt material at the next model-call boundary."""
        self._cached_system_prompt = None

    def set_locations(self, locations: Sequence[Location] | None) -> None:
        """Adopt the workspace's environments as they are now, so one added later reaches an existing session."""
        carried = {uri: resolved.executor for uri, resolved in self._locations.items()}
        self._locations = {}
        self._locations_by_name = {}
        self._build_locations(locations, executors=carried)
        # A running prompt remains stable until its explicit refresh boundary (normally a maintenance fold).

    def _build_locations(
        self,
        locations: Sequence[Location] | None,
        *,
        executors: dict[str, Any] | None = None,
    ) -> None:
        """The resolved-location map, each entry carrying an executor and its effective policy."""
        entries = locations or []
        if not entries:
            # None supplied: synthesize one local location, so the single-location default still applies.
            entries = [Location("local", "local", self._working_directory)]
        for entry in entries:
            kind = entry.kind
            base_directory = entry.base_directory
            host_alias = entry.host_alias
            address = LocationAddress(
                kind=kind, base_directory=base_directory, host_alias=host_alias
            )
            uri = entry.uri or location_uri_for(address)
            resolved = ResolvedLocation(
                uri=uri,
                name=entry.name,
                kind=kind,
                base_directory=base_directory,
                executor=entry.executor or (executors or {}).get(uri) or executor_for(address),
            )
            self._locations[uri] = resolved
            self._locations_by_name[resolved.name] = resolved

    def _resolve_location(self, location_value: str | None) -> ResolvedLocation:
        """Resolve a call's ``location`` to its executor and policy, defaulting to the local filesystem."""
        if not location_value:
            if len(self._locations) == 1:
                return next(iter(self._locations.values()))
            # Default to local, so an omission is never executed on a remote host.
            local = next(
                (location for location in self._locations.values() if location.kind == "local"),
                None,
            )
            if local is not None:
                return local
            # Every location is remote, so require an explicit choice rather than picking one.
            raise ToolLocationError(
                self._prompt_loader.load(
                    "location_required",
                    {"available_locations": compact(sorted(self._locations_by_name))},
                )
            )
        if location_value in self._locations:
            return self._locations[location_value]
        if location_value in self._locations_by_name:
            return self._locations_by_name[location_value]
        raise ToolLocationError(
            self._prompt_loader.load(
                "location_unknown",
                {
                    "location": location_value,
                    "available_locations": compact(sorted(self._locations_by_name)),
                },
            )
        )

    def _call_policy(self, location: ResolvedLocation | None) -> CallExecutionPolicy:
        """One call's execution policy, as a value, so concurrent calls to different locations cannot cross."""
        working_directory = (
            self._working_directory
            if location is None or location.is_remote
            else location.base_directory
        )
        return CallExecutionPolicy(
            location=location, working_directory=working_directory, mode=self._permission_mode
        )

    def _canonical_working_directory(self, working_directory: str | None = None) -> str:
        return str(
            Path(working_directory or self._working_directory or Path.home())
            .expanduser()
            .resolve(strict=False)
        )

    async def _acquire_filesystem_lease(
        self, *, scope: str, path: str, description: str, working_directory: str | None = None
    ) -> str:
        if self._file_lease_manager is None:
            return ""
        return await self._file_lease_manager.acquire(
            owner_session_id=self._session_id,
            scope=scope,
            path=path,
            working_directory=self._canonical_working_directory(working_directory),
            description=description,
        )

    def _release_filesystem_lease(self, token: str) -> None:
        if token and self._file_lease_manager is not None:
            self._file_lease_manager.release(token)

    @property
    def conversation(self) -> list:
        return self._conversation

    def _peer_conversation_snapshot(self) -> list[dict[str, Any]]:
        """Serialize the usable conversation prefix inherited by a newly created peer."""
        inherited_messages = self._conversation
        if (
            inherited_messages
            and isinstance(inherited_messages[-1], AIMessage)
            and inherited_messages[-1].tool_calls
        ):
            inherited_messages = inherited_messages[:-1]
        return messages_to_dict(inherited_messages)

    @property
    def features(self):
        """The installed features, which a composer reaches through ``by_type`` to orchestrate."""
        return self._features

    @property
    def _background(self):
        """This runtime's background-job runner, owned by whatever plugin answers for it."""
        return self._features.invoke("background")

    def constrained_tool_named(self, tool_name: str):
        """One tool of a given name from the executable set, for a sub-session being bound down to its verdict tool."""
        return [tool for tool in self._tools if tool.name == tool_name]

    def constrain_toolset(self, only: Sequence[BaseTool]) -> None:
        """Bind the session down to exactly the given tools, as a reviewer or summarizer's one verdict tool."""
        self._tools = list(only)
        self._tool_schemas = {tool.name: tool.args_schema for tool in only}
        self._model_tools = list(only)
        self._bound_model = self._model.bind_tools(list(only))

    def grant_tool(self, tool: BaseTool) -> None:
        """Grant a tool to this session at any moment: dispatchable now, described to the model
        by an appended message, so the bound schema — and the provider cache prefix — is untouched.
        A grant of a name the session already runs replaces that tool's implementation."""
        if tool.name in self._supplied_tool_names:
            return
        tool = with_explanation(tool)
        self._supplied_tool_names.add(tool.name)
        self._tool_units[tool.name] = Tool(
            name=tool.name,
            schema=tool,
            description=tool.description or "",
            handler=invoke_supplied,
        )
        self._tools = [
            existing for existing in self._tools if existing.name != tool.name
        ] + [tool]
        self._tool_schemas[tool.name] = tool.args_schema
        self._conversation.append(self._tool_grant_message(tool))
        self._note_session_changed()

    def _tool_grant_message(self, tool: BaseTool):
        """The conversation message that describes a granted tool, schema included, so the model
        can construct a call without the tool being bound into the provider schema."""
        schema: dict[str, Any] = {}
        args_schema = getattr(tool, "args_schema", None)
        if args_schema is not None:
            try:
                schema = args_schema.model_json_schema()
            except Exception:  # noqa: BLE001 — a malformed schema still leaves the description useful
                schema = {}
        content = self._prompt_loader.load(
            "tool_grant",
            {
                "tool_name": tool.name,
                "description": (tool.description or "").strip(),
                "schema": compact(schema),
            },
        )
        return self._reminder_message(content, marks={"tool_grant": True, "tool_name": tool.name})

    @property
    def token_usage(self) -> dict[str, int]:
        return dict(self._token_usage)

    def _accumulate_usage(self, response: AIMessage) -> TurnEvent | None:
        """Accumulate one call's usage into the session total and answer a USAGE event, or ``None`` when none was reported."""
        usage = getattr(response, "usage_metadata", None)
        if not usage:
            return None
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        total_tokens = int(usage.get("total_tokens", 0) or 0) or (input_tokens + output_tokens)
        cache_read = int((usage.get("input_token_details") or {}).get("cache_read", 0) or 0)
        reasoning = int((usage.get("output_token_details") or {}).get("reasoning", 0) or 0)
        if not (input_tokens or output_tokens or total_tokens):
            return None
        # What the adapter worked out about this request's prefix, read before the totals below.
        cache_trace = response.additional_kwargs.get("cache_trace") or {}
        # What could have been served, which is never less than what was, and zero on a session's first call.
        reachable = max(int(cache_trace.get("reachable_tokens", 0) or 0), cache_read)
        self._token_usage["input_tokens"] += input_tokens
        self._token_usage["output_tokens"] += output_tokens
        self._token_usage["total_tokens"] += total_tokens
        self._token_usage["cache_read_tokens"] += cache_read
        self._token_usage["reachable_tokens"] += reachable
        self._token_usage["reasoning_tokens"] += reasoning
        self._token_usage["model_calls"] += 1
        # The latest call's input is the whole prompt, so it says how full the context is.
        model = getattr(self, "_model", None)
        reported_context_window = model.context_window() if model is not None else 0
        # Some OpenAI-compatible gateways omit model metadata on a response. Never replace a catalogued non-zero window with that absence: doing so disables automatic compacting.
        if reported_context_window > 0:
            self._context_window = reported_context_window
            self._context_window_estimated = False
        context_window = self._context_window
        self._latest_context_tokens = input_tokens + output_tokens
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cache_read_tokens=cache_read,
            reasoning_tokens=reasoning,
            context_window=context_window,
            context_window_estimated=self._context_window_estimated,
            cumulative=dict(self._token_usage),
            prefix_intact=bool(cache_trace.get("prefix_intact", False)),
            reachable_tokens=reachable,
            segments=int(cache_trace.get("segments", 0) or 0),
            shared_segments=int(cache_trace.get("shared_segments", 0) or 0),
            divergence=cache_trace.get("divergence"),
        )

    @property
    def agent_name(self) -> str:
        return self._agent_configuration.identifier

    @property
    def inline_image_bytes(self) -> int:
        """The ceiling on an image inlined into this conversation, as the person configured it."""
        return self._global_configuration.attachments.inline_image_bytes

    @property
    def model_identifier(self) -> str:
        return self._agent_configuration.model_identifier

    @property
    def working_directory(self) -> str:
        return self._working_directory

    @property
    def permission_mode(self) -> PermissionMode:
        return self._permission_mode

    @property
    def project_directory(self) -> str:
        return self._project_directory

    @property
    def writes_anywhere(self) -> bool:
        """Whether this session's confinement permits writing at all, which the operating system holds it to."""
        return bool(self._sandbox.filesystem.writable)

    def abort(self) -> None:
        # Stop tears down the live turn only: detached work and peer sessions have their own lifecycles. A queued steering message is superseded by the Stop: deliver its false and never let it keep the cancelled turn waiting.
        self.discard_pending_steering()
        self._stop_requested = True
        self._abort_event.set()
        runner = self._background
        if runner is not None:
            runner.cancel_foreground()
        for task in list(self._active_tool_tasks.values()):
            task.cancel()

    def clear_stop(self) -> None:
        """A genuinely fresh user turn begins: no stop is owed from before."""
        self._stop_requested = False

    @property
    def stop_requested(self) -> bool:
        """Whether a stop is still owed, which continuation work must not erase."""
        return self._stop_requested

    def interrupt_for_restart(self) -> None:
        """Stop live work while leaving its durable job records for startup recovery."""
        self._stop_requested = True
        self._abort_event.set()
        runner = self._background
        if runner is not None:
            runner.cancel_all()
        for task in list(self._active_tool_tasks.values()):
            task.cancel()

    def abort_tool(self, tool_call_identifier: str) -> bool:
        task = self._active_tool_tasks.get(tool_call_identifier)
        aborted = False
        if task is not None and not task.done():
            task.cancel()
            aborted = True
        runner = self._background
        return bool(
            (runner is not None and runner.cancel_by_tool_call(tool_call_identifier)) or aborted
        )

    def enqueue_steering(
        self, message: str, message_id: str = "", peer_sender: str = ""
    ) -> asyncio.Future[bool] | None:
        text = message.strip()
        if not text:
            return None
        accepted = asyncio.get_running_loop().create_future()
        self._pending_steering.append((text, message_id, peer_sender, accepted))
        # Drained at the next model boundary; never touches the interrupt event, which is a real stop only.
        return accepted

    def discard_pending_steering(self) -> None:
        """Drop steering accepted too late to be honoured, since the client re-delivers it as a fresh turn."""
        for _text, _message_id, _peer_sender, accepted in self._pending_steering:
            if not accepted.done():
                accepted.set_result(False)
        self._pending_steering.clear()

    def _has_queued_steering(self) -> bool:
        return bool(self._pending_steering)

    def set_permission_mode(self, mode: PermissionMode) -> PermissionMode:
        """Adopt the mode the host resolved, reaching the very next tool call."""
        self._permission_mode = mode
        return mode

    def set_a2a_turn_id(self, turn_id: str) -> None:
        """Record the current turn's task id, so work raised during it can name the turn it belongs to."""
        self._a2a_turn_id = turn_id

    def set_turn_reader(self, task_reader: Callable) -> None:
        """Install the reader `read_turn` uses to fetch related turns from the store."""
        self._turn_reader = task_reader

    def session_snapshot(self) -> dict:
        """The durable non-conversation state the features own, plus the core's own recovery flag."""
        return {**self._features.snapshot(), "turn_recovery": self._turn_recovery}

    def restore_session(self, snapshot: dict) -> None:
        """Rehydrate the features' durable state and the core's recovery flag."""
        self._features.restore(snapshot)
        recovery = str(snapshot.get("turn_recovery") or "none")
        # A process that died after claiming the retry still owes that retry after restart.
        self._turn_recovery = "retryable" if recovery == "retrying" else recovery
        if self._turn_recovery not in {"none", "retryable"}:
            self._turn_recovery = "none"

    @property
    def retryable_turn(self) -> bool:
        """Whether the last failed turn can continue from its durable conversation tail."""
        return self._turn_recovery == "retryable"

    def mark_turn_failed(self) -> None:
        if self._turn_recovery != "retryable":
            self._turn_recovery = "retryable"
            self._note_session_changed()

    def begin_turn_retry(self) -> bool:
        if self._turn_recovery != "retryable" or bool(self._features.blocked_reason()):
            return False
        self._turn_recovery = "retrying"
        self._note_session_changed()
        return True

    def abandon_turn_retry(self) -> None:
        """Let newly accepted user work supersede a previously failed turn."""
        if self._turn_recovery != "none":
            self._turn_recovery = "none"
            self._note_session_changed()

    def mark_turn_succeeded(self) -> None:
        if self._turn_recovery != "none":
            self._turn_recovery = "none"
            self._note_session_changed()

    # The boundary and the grants: the boundary is the core's, the permission plugin fills it.

    def _granted_profile(self):
        """The session's confinement with every standing grant compacted in. What an escape is measured against."""
        profile = self._sandbox
        for grant in self._access_grants:
            profile = profile.with_grant(grant, workspace=self._working_directory or "")
        return profile

    def _record_grant(self, grant: Grant) -> None:
        self._access_grants.append(grant)

    def dirty_session_snapshot(self) -> Optional[dict]:
        """Return state newer than the last persisted revision without acknowledging it."""
        return (
            self.session_snapshot()
            if self._persisted_session_revision < self._session_revision
            else None
        )

    @property
    def session_revision(self) -> int:
        """The revision captured beside a session-state snapshot before it is persisted."""
        return self._session_revision

    def _note_session_changed(self) -> None:
        """Advance the durable-state revision after a session mutation."""
        self._session_revision += 1

    def clear_session_dirty(self, persisted_revision: int) -> None:
        """Acknowledge exactly the revision whose write completed, without hiding newer mutations."""
        self._persisted_session_revision = max(self._persisted_session_revision, persisted_revision)

    def _record_event(self, event_type: str, data: dict) -> None:
        record = {
            "type": event_type,
            "timestamp": _utc_timestamp(datetime.now(timezone.utc)),
            **data,
        }
        self._execution_history.append(record)
        self._observe(event_type, data)

    def _record_message(self, role: str, content: str, tool_call_id: str = "") -> None:
        self._observe("message", {"role": role, "content": content, "tool_call_id": tool_call_id})

    def _observe(self, kind: str, data: dict) -> None:
        """Hand one observation to the caller's observer. One that raises must not take the turn with it."""
        if self._observer is None:
            return
        observation = Observation(
            session_id=self._session_id,
            kind=kind,
            at=datetime.now(timezone.utc),
            data=data,
        )
        try:
            pending = self._observer.observe(observation)
        except Exception:  # noqa: BLE001 — an audit sink must never fail a turn
            logger.debug("the observer raised on %s", kind, exc_info=True)
            return
        if pending is None:
            return
        try:
            asyncio.get_running_loop().create_task(_drain_observer(pending))
        except RuntimeError:
            # No loop: an observation outside a turn, with nothing to schedule it on.
            logger.debug("dropped an awaitable observation with no running loop")

    def _model_supports_vision(self) -> bool:
        """Whether the model advertises image input. An unknown model is assumed capable, as elsewhere."""
        model = find_model(self.model_identifier)
        return True if model is None else model.vision

    def get_execution_history(self) -> list[dict]:
        return self._execution_history
