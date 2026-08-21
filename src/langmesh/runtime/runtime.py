from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, cast

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    messages_to_dict,
)
from langchain_core.tools import BaseTool
from pydantic import SecretStr

from langmesh.base import confinement as _confinement
from langmesh.base.configuration import (
    AgentConfiguration,
    Configuration,
    PermissionEvaluator,
    SandboxConfiguration,
)
from langmesh.base.configuration.permission_mode import PermissionMode
from langmesh.base.confinement import Grant, Profile
from langmesh.base.content.models import find_model, resolve_litellm
from langmesh.base.contracts.catalogue import project_catalogue
from langmesh.base.contracts.ports import Observation
from langmesh.base.primitives.serialization import content_address
from langmesh.runtime.composition import RuntimeComponents, RuntimeProfile
from langmesh.runtime.environment import RuntimeEnvironment
from langmesh.runtime.features import (
    BackgroundCapability,
    BookkeepingView,
    BoundaryView,
    ConversationView,
    PluginBus,
    PluginContext,
    PluginHost,
    LocationsCapability,
    ToolsView,
    TurnView,
    WindowView,
    build_features,
    feature_prompts,
)
from langmesh.runtime.hooks import HookRunner
from langmesh.runtime.internals import (
    _utc_timestamp,
    conversation_tokens,
)
from langmesh.runtime.models.codex import ChatCodexModel
from langmesh.runtime.models.cursor import ChatCursorModel
from langmesh.runtime.models.litellm import ChatLiteLLMModel
from langmesh.runtime.pipeline import ToolPipeline
from langmesh.runtime.session_control import RenderedPrompt, SessionSnapshot
from langmesh.runtime.tools import registry as tools_registry
from langmesh.runtime.tools.arguments import with_shared_fields
from langmesh.runtime.tools.context import ToolContext
from langmesh.runtime.tools.execution import Tool, ToolServices, invoke_supplied
from langmesh.runtime.tools.handlers import HANDLERS
from langmesh.runtime.turn import (
    _RunsTurns,
)
from langmesh.runtime.turn_events import (
    Usage,
)

logger = logging.getLogger(__name__)


async def _drain_observer(pending) -> None:
    """Await a caller-supplied audit observer without making it part of turn control flow."""
    try:
        await pending
    except Exception:  # noqa: BLE001 — an audit sink must never fail a turn
        logger.debug("an asynchronous audit observer raised", exc_info=True)


class _CataloguePrompts:
    """A prompt-template view of a catalogue, so the template seam costs one adapter."""

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
            "default_headers": resolved["headers"],
            "session_id": session_id,
            "context_length": catalogued.context_length if catalogued else 0,
            "temperature": 0,
            "reasoning_effort": agent_configuration.reasoning_effort,
        }
    )


def _as_profile(sandbox: Any) -> Profile:
    """Whatever a caller called a sandbox, as the :class:`Profile` the runtime works with."""
    if sandbox is None:
        # The configured default, not `Profile()`: an empty writable set means "may write nowhere".
        return SandboxConfiguration.model_validate({}).to_profile()
    if isinstance(sandbox, Profile):
        return sandbox
    if isinstance(sandbox, dict):
        return Profile.from_dict(sandbox)
    to_profile = getattr(sandbox, "to_profile", None)
    if callable(to_profile):
        converted = to_profile()
        if isinstance(converted, Profile):
            return converted
        raise TypeError(
            f"sandbox's to_profile must return a confinement Profile — got {type(converted).__name__}."
        )
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
    toolbox: Any = None,
) -> ToolContext:
    """The session-shaped state this runtime's tools read, derived from configuration rather than installed."""
    # The session's own tools, and the one widening that goes with them: it cannot install where it may not write.
    if toolbox is not None:
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

    async def acquire(
        self, *, scope: str, path: str, description: str, working_directory: str
    ) -> str:
        return await self._runtime._acquire_filesystem_lease(
            scope=scope, path=path, description=description, working_directory=working_directory
        )

    def release(self, token: str) -> None:
        self._runtime._release_filesystem_lease(token)

    def canonical_working_directory(self, directory: str) -> str:
        return self._runtime._canonical_working_directory(directory)


class AgentRuntime(_RunsTurns):
    # A turn runs until the model is done or the user interrupts: no ceiling and no stuck-detector.

    def __init__(
        self,
        profile: RuntimeProfile,
        components: RuntimeComponents | None = None,
        *,
        conversation: Optional[list] = None,
    ):
        if components is None:
            components = RuntimeComponents()

        self._components = components
        self._environment = components.environment or RuntimeEnvironment()
        self._prompt_composer = components.prompt_composer
        self._hooks = HookRunner(components.hooks)
        self._pipeline = ToolPipeline(components.middleware)
        self._resource_sync = components.synchronize_resources
        self._session_id = profile.session_id
        # The session that created this one, empty when a person did. Reporting back needs its id.
        self._parent_session = profile.parent_session
        # What every child is confined to, held so a configuration edit cannot widen a live session.

        # Normalised once, because callers hand this three different shapes.
        self._sandbox = _as_profile(profile.sandbox)
        self._agent_configuration = profile.agent
        self._global_configuration = profile.configuration
        self._working_directory = profile.working_directory or str(Path.home())
        self._project_directory = profile.project_directory or self._working_directory
        # The host already resolved the session mode; a direct library caller falls back to the profile.
        self._permission_mode = PermissionMode.resolve(
            profile.permission_mode, profile.agent.permission_default
        )

        model_identifier = profile.agent.model_identifier
        # Only a runtime that must build a client needs to be told which one.
        if not model_identifier and components.model is None:
            raise ValueError(
                f"Agent '{profile.agent.identifier}' names no model. Set `provider` and `model` in its profile, pass `model_identifier=\"provider/model\"` to `langmesh.Session`, or hand the runtime a `model=` of your own."
            )

        # A caller's own model wins, since accepting `BaseChatModel` is the whole of the model seam.
        self._model = (
            components.model
            if components.model is not None
            else build_chat_model(
                model_identifier or "",
                profile.configuration,
                profile.agent,
                self._working_directory,
                profile.session_id,
            )
        )

        self._file_lease_manager = components.file_leases
        # Caller-supplied tools join the initial provider schema, while a later grant deliberately changes that schema once.
        supplied_tools = tuple(components.tools)
        # What a caller's tool is gated at: asking by default, so adding one cannot silently widen a session.
        self._tool_gate = components.tool_gate
        # The session's tools are composed by the caller, never forced: the complete roster comes from `toolset`, additions from `tools`/`grant_tool`, and nothing is injected by default.
        configured_tools = list(components.toolset) if components.toolset is not None else []
        # Every tool a session runs carries the shared `explanation` field, added here once.
        configured_tools = [with_shared_fields(tool) for tool in configured_tools]
        # The dispatchable units: every tool the session runs, assembled from the configured set and caller-supplied replacements.
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
        for tool in supplied_tools:
            units[tool.name] = Tool(
                name=tool.name,
                schema=tool,
                description=(tool.description or ""),
                handler=invoke_supplied,
            )
        self._tool_units = units
        # The caller's own tools, for the gate and for replacing a built-in's execution.
        self._supplied_tool_names = {tool.name for tool in supplied_tools}
        # Executable set (for gating, validation and direct invocation): configured plus grants, grants win.
        self._tools = [
            tool for tool in configured_tools if tool.name not in self._supplied_tool_names
        ] + list(supplied_tools)
        self._model_tools = list(self._tools)
        self._tool_schemas: dict[str, Any] = {tool.name: tool.args_schema for tool in self._tools}
        self._bound_model = self._bind_model_tools(self._model_tools)
        # The evaluator's own `tools_enabled` gate refuses what the profile did not declare.
        self._permissions = (
            components.permissions
            if components.permissions is not None
            else PermissionEvaluator(profile.agent)
        )
        # Where the audit trail goes, and who answers a gate. Both absent by default.
        self._observer = components.observer
        self._approvals = components.approvals
        self._transcript = components.transcript
        # The conversation and the prompt this runtime runs with.

        self._conversation: list = conversation if conversation is not None else []
        self._system_prompt = profile.agent.system_prompt
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
            "cache_write_tokens": 0,
            # What a cache could have returned, since the read alone has no honest denominator.
            "reusable_prefix_tokens": 0,
            "reasoning_tokens": 0,
            "model_calls": 0,
        }

        # Where the prompt's material comes from, supplied rather than found by walking hardcoded paths.
        # The library default discovers no skills or instruction files on disk: they are voluntary, injected by the caller.
        self._catalogue = components.catalogue or project_catalogue()
        self._prompt_loader = _CataloguePrompts(self._catalogue)
        # Creation-time grants are described from the first turn: their messages sit at the head of the conversation, before any user message, and are stable for the session's life.
        self._cached_system_prompt: str | None = None
        self._rendered_prompt: RenderedPrompt | None = None
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
        context_window: Any = getattr(self._model, "context_window", None)
        reported_context_window = (
            max(0, int(cast(Any, context_window)())) if callable(context_window) else 0
        )
        # Every model must advertise its own context capacity; an unknown window means the harness cannot schedule compacting or refuse an oversized request with numbers.
        self._context_window_estimated = reported_context_window == 0
        self._context_window = reported_context_window
        self._turn_recovery = "none"
        # The failed turn a current retry continues: retries change per attempt, so chain identity
        # must come from the terminal error the chain began with, not from the attempt being failed.
        self._turn_failure_root: str | None = None
        # What the module-level tools read at call time, built from this runtime's own configuration and conversation.
        self._tool_context = _build_tool_context(
            profile.configuration,
            sandbox=self._sandbox,
            workspace=self._working_directory,
            session_id=self._session_id,
            session_access=components.sessions,
            conversation_snapshot=self._peer_conversation_snapshot,
            mcp_server_manager=components.mcp_servers,
            toolbox=components.toolbox,
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
                writes_anywhere=self.writes_anywhere,
                resolve_execution=self._resolve_execution,
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
                tool_gate=self._tool_gate,
                turn_reader=self._turn_reader,
            ),
            window=WindowView(
                context_window=self._context_window,
                latest_context_tokens=self._latest_context_tokens,
                set_latest_context_tokens=lambda value: setattr(
                    self, "_latest_context_tokens", value
                ),
                refresh_cached_prompt=self.refresh_system_prompt,
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
            services=components.services,
        )
        self._features = build_features(components.features, self._plugin_context, plugin_host)
        # Features may contribute tools and event-rich handlers without the core naming their owners.
        contributed = [with_shared_fields(tool) for tool in self._features.contributed_tools()]
        contributed_handlers = self._features.contributed_tool_handlers()
        if contributed:
            contributed_names = {tool.name for tool in contributed}
            self._tools = [
                tool for tool in self._tools if tool.name not in contributed_names
            ] + list(contributed)
            self._model_tools = [
                tool for tool in self._model_tools if tool.name not in contributed_names
            ] + list(contributed)
            self._tool_schemas.update({tool.name: tool.args_schema for tool in contributed})
            for tool in contributed:
                handler = contributed_handlers.get(tool.name, invoke_supplied)
                self._tool_units[tool.name] = Tool(
                    name=tool.name,
                    schema=tool,
                    description=tool.description or "",
                    handler=handler,
                )
            self._bound_model = self._bind_model_tools(self._model_tools)
        self._apply_contributed_schema_fields()
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
        if self._cached_system_prompt is None and self._rendered_prompt is None:
            return
        self._cached_system_prompt = None
        self._rendered_prompt = None
        self._note_session_changed()

    def _system_prompt_revision(self) -> str:
        """Identify every stable construction input while excluding mutable session state."""
        tools = []
        for tool in self._model_tools:
            schema = getattr(tool, "args_schema", None)
            model_json_schema = getattr(schema, "model_json_schema", None)
            tools.append(
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "schema": model_json_schema() if callable(model_json_schema) else {},
                }
            )
        user_context = getattr(self._global_configuration, "user_context", None)
        return content_address(
            {
                "agent": {
                    "name": self._agent_configuration.name,
                    "skills": self._agent_configuration.skills,
                    "system_prompt": self._system_prompt,
                },
                "catalogue": self._catalogue.prompt_revision(),
                "components": self._components.prompt_revision,
                "features": self._features.prompt_revision(),
                "profile": {
                    "session": self._session_id,
                    "parent": self._parent_session,
                    "working_directory": self._working_directory,
                    "project_directory": self._project_directory,
                    "workspace_strategy": self._global_configuration.workspace.strategy,
                    "sandbox_enforcement": self._global_configuration.sandbox.enforce,
                    "user_context_enabled": bool(user_context and user_context.enabled),
                },
                "prompt_composer": (
                    f"{type(self._prompt_composer).__module__}.{type(self._prompt_composer).__qualname__}"
                    if self._prompt_composer is not None
                    else ""
                ),
                "tools": tools,
            }
        )

    def _apply_contributed_schema_fields(self) -> None:
        """Add each plugin's extra argument fields to the tools they extend, and rebind.

        The extension happens after the features are built, so a plugin's contributed field
        (the locations plugin's ``location`` selector on bash) appears only when that plugin
        is composed. The field's explanation lives with the plugin that contributes it.
        """
        from pydantic import create_model

        extended_any = False
        for tool in self._tools:
            extra = self._features.contributed_schema_fields(tool.name)
            if not extra:
                continue
            schema: Any = tool.args_schema
            if schema is None:
                continue
            fields = {
                name: (field.annotation, field)
                for name, field in schema.model_fields.items()
                if name not in ("kwargs", "args")
            }
            fields.update(extra)
            extended = create_model(f"{schema.__name__}Arguments", **cast(Any, fields))
            tool.args_schema = extended
            self._tool_schemas[tool.name] = extended
            unit = self._tool_units.get(tool.name)
            if unit is not None:
                self._tool_units[tool.name] = dataclasses.replace(unit, schema=extended)
            extended_any = True
        if extended_any:
            self._bound_model = self._bind_model_tools(self._model_tools)

    def _bind_model_tools(self, tools: Sequence[BaseTool]) -> Any:
        """Bind a nonempty tool roster while leaving an ordinary chat model untouched for a plain turn."""
        return self._model.bind_tools(list(tools)) if tools else self._model

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
        capability = self._features.capability(BackgroundCapability)
        return capability.runner if capability is not None else None

    def _resolve_execution(self, tool_name: str, arguments: dict) -> Any:
        """Resolve a feature-owned execution target without naming the feature that owns it."""
        capability = self._features.capability(LocationsCapability)
        return (
            capability.resolve_execution(tool_name, arguments) if capability is not None else None
        )

    def constrained_tool_named(self, tool_name: str):
        """One tool of a given name from the executable set, for a sub-session being bound down to its verdict tool."""
        return [tool for tool in self._tools if tool.name == tool_name]

    def constrain_toolset(self, only: Sequence[BaseTool]) -> None:
        """Bind the session down to exactly the given tools, as a reviewer or summarizer's one verdict tool."""
        self._tools = list(only)
        self._tool_schemas = {tool.name: tool.args_schema for tool in only}
        self._model_tools = list(only)
        self._bound_model = self._bind_model_tools(only)

    def grant_tool(self, tool: BaseTool) -> None:
        """Grant or replace a provider-visible tool, intentionally changing the next request's schema."""
        tool = with_shared_fields(tool)
        self._supplied_tool_names.add(tool.name)
        self._tool_units[tool.name] = Tool(
            name=tool.name,
            schema=tool,
            description=tool.description or "",
            handler=invoke_supplied,
        )
        self._tools = [existing for existing in self._tools if existing.name != tool.name] + [tool]
        self._model_tools = [
            existing for existing in self._model_tools if existing.name != tool.name
        ] + [tool]
        self._tool_schemas[tool.name] = tool.args_schema
        self._bound_model = self._bind_model_tools(self._model_tools)
        self._note_session_changed()

    @property
    def token_usage(self) -> dict[str, int]:
        return dict(self._token_usage)

    def _accumulate_usage(self, response: AIMessage) -> Usage | None:
        """Accumulate one call's usage into the session total and answer a USAGE event, or ``None`` when none was reported."""
        usage = getattr(response, "usage_metadata", None)
        if not usage:
            return None
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        total_tokens = int(usage.get("total_tokens", 0) or 0) or (input_tokens + output_tokens)
        cache_read = int((usage.get("input_token_details") or {}).get("cache_read", 0) or 0)
        cache_write = int((usage.get("input_token_details") or {}).get("cache_creation", 0) or 0)
        reasoning = int((usage.get("output_token_details") or {}).get("reasoning", 0) or 0)
        if not (input_tokens or output_tokens or total_tokens):
            return None
        # What the adapter worked out about this request's prefix, read before the totals below.
        cache_trace = response.additional_kwargs.get("cache_trace") or {}
        # What could have been served, which is never less than what was, and zero on a session's first call.
        reachable = max(int(cache_trace.get("reusable_prefix_tokens", 0) or 0), cache_read)
        self._token_usage["input_tokens"] += input_tokens
        self._token_usage["output_tokens"] += output_tokens
        self._token_usage["total_tokens"] += total_tokens
        self._token_usage["cache_read_tokens"] += cache_read
        self._token_usage["cache_write_tokens"] += cache_write
        self._token_usage["reusable_prefix_tokens"] += reachable
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
            cache_write_tokens=cache_write,
            reasoning_tokens=reasoning,
            context_window=context_window,
            context_window_estimated=self._context_window_estimated,
            cumulative=dict(self._token_usage),
            cache_prefix_reusable=cache_trace.get("cache_prefix_reusable"),
            reusable_prefix_tokens=reachable,
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
        return self._agent_configuration.model_identifier or ""

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

    def session_snapshot(self) -> SessionSnapshot:
        """The durable non-conversation state the features own, plus the core's own recovery flag."""
        cache_snapshot = getattr(self._model, "model_cache_snapshot", None)
        return SessionSnapshot(
            features=self._features.snapshot(),
            turn_recovery="retryable" if self._turn_recovery != "none" else "none",
            turn_failure_root=self._turn_failure_root,
            model_cache=cache_snapshot() if callable(cache_snapshot) else None,
            system_prompt=self._rendered_prompt,
        )

    def restore_session(self, snapshot: SessionSnapshot) -> None:
        """Rehydrate the features' durable state and the core's recovery flag."""
        if not isinstance(snapshot, SessionSnapshot):
            raise TypeError("snapshot must be a SessionSnapshot value")
        self._features.restore(snapshot.features)
        restore_model_cache = getattr(self._model, "restore_model_cache", None)
        if callable(restore_model_cache):
            restore_model_cache(snapshot.model_cache)
        prompt = snapshot.system_prompt
        if prompt is not None and prompt.revision == self._system_prompt_revision():
            self._cached_system_prompt = prompt.content
            self._rendered_prompt = prompt
        else:
            self._cached_system_prompt = None
            self._rendered_prompt = None
        self._turn_recovery = snapshot.turn_recovery
        self._turn_failure_root = snapshot.turn_failure_root
        if self._turn_recovery != "retryable":
            self._turn_failure_root = None

    @property
    def retryable_turn(self) -> bool:
        """Whether the last failed turn can continue from its durable conversation tail."""
        return self._turn_recovery == "retryable"

    def mark_turn_failed(self, chain_root: str | None = None) -> None:
        if self._turn_recovery != "retryable":
            self._turn_recovery = "retryable"
            # The transition names the chain's root: the terminal error the chain began with. A
            # retry attempt that also fails keeps the root the base failure established.
            if chain_root is not None:
                self._turn_failure_root = chain_root
            self._note_session_changed()

    @property
    def turn_failure_root(self) -> str | None:
        """The terminal error id the current retry chain began with, if one is owed."""
        return self._turn_failure_root

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
            self._turn_failure_root = None
            self._note_session_changed()

    def mark_turn_succeeded(self) -> None:
        if self._turn_recovery != "none":
            self._turn_recovery = "none"
            self._turn_failure_root = None
            self._note_session_changed()

    # The boundary and the grants: the boundary is the core's, the permission plugin fills it.

    def _granted_profile(self) -> Profile:
        """The session's confinement with every standing grant compacted in. What an escape is measured against."""
        profile = self._sandbox
        for grant in self._access_grants:
            profile = profile.with_grant(grant, workspace=self._working_directory or "")
        return profile

    def _record_grant(self, grant: Grant) -> None:
        self._access_grants.append(grant)

    def dirty_session_snapshot(self) -> Optional[SessionSnapshot]:
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
