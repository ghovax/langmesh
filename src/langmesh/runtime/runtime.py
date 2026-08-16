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
from pydantic import BaseModel, SecretStr, ValidationError


from langmesh.base.configuration import (
    AgentConfiguration,
    Configuration,
    PermissionEvaluator,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langmesh.runtime.models.litellm import ChatLiteLLMModel
from langmesh.runtime.models.codex import ChatCodexModel
from langmesh.runtime.models.cursor import ChatCursorModel
from langmesh.base.models import find_model, resolve_litellm
from langmesh.base.tools import as_tool_grants
from langmesh.runtime.tools.execution import Tool, ToolServices, invoke_supplied
from langmesh.runtime.tools.units import BUILTIN_TOOLS
from langmesh.locations.resolver import LocationAddress, executor_for, location_uri_for
from langmesh.runtime.tools.registry import (
    bash as bash_tool,
    search_web as search_web_tool,
    read_turn as read_turn_tool,
    set_tasks as set_tasks_tool,
    update_tasks as update_tasks_tool,
    update_goal as update_goal_tool,
    list_mcp_tools as list_mcp_tools_tool,
    call_mcp_server_tool as call_mcp_server_tool_tool,
    list_mcp_resources as list_mcp_resources_tool,
    read_mcp_resource as read_mcp_resource_tool,
    fetch_url as fetch_url_tool,
    download_file as download_file_tool,
    control_screen as control_screen_tool,
    ask_user as ask_user_tool,
    load_skill as load_skill_tool,
)
from langmesh.base.ports import Observation
from langmesh.runtime.tools.context import ToolContext
from langmesh.runtime.tools.sessions import remote_agent_tools, session_tools
from langmesh.runtime.background import (
    BackgroundJobs,
    background_completion_event,
    background_include_result,
)


from langmesh.base.permission_mode import PermissionMode

from langmesh.runtime.locations import CallExecutionPolicy, Location, ResolvedLocation, ToolLocationError
from langmesh.runtime.turn_events import (
    ToolResult,
    TurnEvent,
    Usage,
)

from langmesh.runtime.tools.dispatch import (
    _DispatchesTools,
)

from langmesh.runtime.turn import (
    _RunsTurns,
)

from langmesh.runtime.permissions import (
    _DecidesPermissions,
)

from langmesh.runtime.compaction import (
    _CompactionControl,
    _CompactsContext,
)
from langmesh.base.serialization import compact
from langmesh.base.toolbox import toolbox_for
from langmesh.runtime.goal import Goal
from langmesh.runtime.goal_review import _ReviewsGoal
from langmesh.runtime.composition import RuntimeComponents, RuntimeProfile
from langmesh.runtime.internals import (
    _cap_model_result_payload,
    _maybe_json,
    _model_result_status,
    _tool_timing_metadata,
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


def _build_tools(
    agent_configuration: AgentConfiguration,
    global_configuration: Configuration,
    working_directory: str = "",
    *,
    can_reach_peers: bool = False,
    extra_tools: Sequence[BaseTool] = (),
    permission_mode: PermissionMode = PermissionMode.ASK,
) -> list[BaseTool]:
    tools = _all_available_tools(
        agent_configuration,
        global_configuration,
        working_directory,
        can_reach_peers=can_reach_peers,
        extra_tools=extra_tools,
        permission_mode=permission_mode,
    )
    # A profile's allow-list narrows our tools and never the caller's, which it was written long before.
    supplied = {tool.name for tool in extra_tools}
    allowed = _live_allow_list(
        agent_configuration.tools_enabled,
        {tool.name for tool in tools} - supplied,
    )
    # `disabled` applies to ours and the caller's alike: switching a supplied tool off means it.
    return [
        tool
        for tool in tools
        if agent_configuration.tools.is_enabled(tool.name)
        and (tool.name in supplied or not allowed or tool.name in allowed)
    ]


def _live_allow_list(configured: list[str], existing: set[str]) -> set[str]:
    """An agent's allow-list narrowed to tools that exist, so a stale name cannot leave it permitting nothing."""
    live = {name for name in configured if name in existing}
    return live


def _installed_agent_names(
    global_configuration: Configuration, working_directory: str
) -> list[str]:
    """The profiles a peer could be created with, read at build time so a bad name is unrepresentable."""
    from langmesh.base.configuration import list_agents

    directories = (
        global_configuration.agent_directories_for(working_directory)
        if working_directory
        else global_configuration.agent_directories()
    )
    try:
        return [entry["id"] for entry in list_agents(directories)]
    except Exception:  # noqa: BLE001 — an unreadable profile directory must not fail the runtime
        return []


def _all_available_tools(
    agent_configuration: AgentConfiguration,
    global_configuration: Configuration,
    working_directory: str = "",
    *,
    can_reach_peers: bool = False,
    extra_tools: Sequence[BaseTool] = (),
    permission_mode: PermissionMode = PermissionMode.ASK,
) -> list[BaseTool]:
    available = [
        bash_tool,
        fetch_url_tool,
        download_file_tool,
        load_skill_tool,
        search_web_tool,
        set_tasks_tool,
        update_tasks_tool,
        update_goal_tool,
        read_turn_tool,
    ]
    # Asking parks the turn, which only makes sense where somebody is there — so `automatic` is not given the tool.
    if permission_mode.asks:
        available.append(ask_user_tool)
    # Driving the live screen is opt-in, added only where the user enabled it in Settings.
    if global_configuration.computer_control.enabled:
        available.append(control_screen_tool)
    if global_configuration.mcp.enabled_servers():
        available.extend(
            [
                list_mcp_tools_tool,
                call_mcp_server_tool_tool,
                list_mcp_resources_tool,
                read_mcp_resource_tool,
            ]
        )
    # Peer sessions: offered only with a profile to run and a control plane to reach.
    if can_reach_peers:
        available.extend(
            session_tools(_installed_agent_names(global_configuration, working_directory))
        )
        # Remote agents are a different bargain, so they are separate verbs and appear only when registered.
        if global_configuration.remote_agents.agents:
            available.extend(remote_agent_tools())
    # The caller's tools last, so a name collision resolves to ours rather than replacing a built-in.
    known = {tool.name for tool in available}
    available.extend(tool for tool in extra_tools if tool.name not in known)
    return available


def _as_profile(sandbox: Any):
    """Whatever a caller called a sandbox, as the :class:`Profile` the runtime works with."""
    from langmesh.base.confinement import Profile

    if sandbox is None:
        # The configured default, not `Profile()`: an empty writable set means "may write nowhere".
        from langmesh.base.configuration import SandboxConfiguration

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
        from langmesh.base import confinement as _confinement

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


class TaskItem(BaseModel):
    identifier: str = ""
    # One short phrase naming the task, like a tool call's explanation; the description says what to do.
    title: str = ""
    description: str
    status: str = "pending"
    dependencies: list[str] = []


class TaskManager:
    def __init__(self):
        self._tasks: list[TaskItem] = []
        self._by_identifier: dict[str, TaskItem] = {}
        self._next_identifier: int = 1

    def add_tasks(self, task_definitions: list[dict]) -> list[str]:
        created = []
        for definition in task_definitions:
            # The identifier is the task's index: the model addresses a task by number, never by a prefixed id.
            identifier = str(self._next_identifier)
            self._next_identifier += 1
            task = TaskItem(
                identifier=identifier,
                title=definition.get("title", ""),
                description=definition.get("description", ""),
                dependencies=definition.get("dependencies", []),
            )
            self._tasks.append(task)
            self._by_identifier[identifier] = task
            created.append(identifier)
        return created

    # What an update may say. A key outside this set is reported rather than silently matching nothing.
    UPDATE_KEYS = frozenset({"task_id", "status"})
    STATUSES = ("pending", "in_progress", "completed", "blocked")

    def update_tasks(self, updates: list[dict]) -> tuple[list[str], list[str]]:
        """Apply each update, returning the ids that changed and a complaint for each that did not."""
        updated_ids: list[str] = []
        complaints: list[str] = []
        known = self._by_identifier
        for update in updates:
            unknown = sorted(set(update) - self.UPDATE_KEYS)
            if unknown:
                complaints.append(
                    f"{', '.join(unknown)} is not part of an update; use {', '.join(sorted(self.UPDATE_KEYS))}."
                )
            task_id = update.get("task_id", "")
            status = update.get("status", "")
            if status not in self.STATUSES:
                complaints.append(
                    f"{status!r} is not a status; use one of {', '.join(self.STATUSES)}."
                )
                continue
            if task_id not in known:
                complaints.append(
                    f"There is no task {task_id!r}. Current ids: {', '.join(sorted(known)) or 'none'}."
                )
                continue
            known[task_id].status = status
            updated_ids.append(task_id)
        return updated_ids, complaints

    def render_json(self) -> str:
        if not self._tasks:
            return ""
        return compact([task.model_dump() for task in self._tasks])

    def to_dict_list(self) -> list[dict]:
        return [task.model_dump() for task in self._tasks]

    def unfinished(self) -> list[dict]:
        """Tracked work that still needs action; blocked items remain visible but do not spin a turn."""
        return [task.model_dump() for task in self._tasks if task.status != "completed"]

    def actionable(self) -> list[dict]:
        """Unfinished work that is neither explicitly blocked nor waiting on unfinished work."""
        completed = {task.identifier for task in self._tasks if task.status == "completed"}
        return [
            task.model_dump()
            for task in self._tasks
            if task.status not in {"completed", "blocked"}
            and all(dependency in completed for dependency in task.dependencies)
        ]

    def snapshot(self) -> dict:
        """The manager's durable state, so a rebuilt runtime restores the tasks and keeps minting fresh ids."""
        return {"tasks": self.to_dict_list(), "next_identifier": self._next_identifier}

    def restore(self, snapshot: dict) -> None:
        """Rehydrate from :meth:`snapshot`, tolerating a missing or partial one by staying empty."""
        self._tasks = [TaskItem.model_validate(task) for task in snapshot.get("tasks", [])]
        self._by_identifier = {task.identifier: task for task in self._tasks}
        if len(self._by_identifier) != len(self._tasks):
            raise ValueError("task snapshot contains duplicate identifiers")
        numeric_identifiers = [
            int(task.identifier.removeprefix("task-"))
            for task in self._tasks
            if task.identifier.removeprefix("task-").isdigit()
        ]
        stored_next = int(snapshot.get("next_identifier", 1))
        self._next_identifier = max(stored_next, max(numeric_identifiers, default=0) + 1)


class _GoalAccess:
    """The goal as a tool handler sees it: read the current one, write a new one."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def current(self) -> Any:
        return self._runtime.goal

    def write(self, goal: Any) -> None:
        self._runtime.write_goal(goal)


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
    _DispatchesTools, _DecidesPermissions, _CompactsContext, _ReviewsGoal, _RunsTurns
):
    # A turn runs until the model is done or the user interrupts: no ceiling and no stuck-detector.

    def __init__(
        self,
        profile: RuntimeProfile,
        components: RuntimeComponents = RuntimeComponents(),
        *,
        conversation: Optional[list] = None,
    ):
        from langmesh.runtime.hooks import HookRunner
        from langmesh.runtime.pipeline import ToolPipeline

        agent_configuration = profile.agent
        global_configuration = profile.configuration
        session_id = profile.session_id
        working_directory = profile.working_directory
        project_directory = profile.project_directory
        session_access = components.sessions
        mcp_server_manager = components.mcp_servers
        model = components.model
        jobs = components.jobs
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
        self._compaction = components.compaction
        self._resource_sync = components.synchronize_resources
        self._session_id = session_id
        # The session that created this one, empty when a person did. Reporting back needs its id.
        self._parent_session = profile.parent_session
        # What every child is confined to, held so a configuration edit cannot widen a live session.
        from langmesh.base.confinement import Grant

        # Normalised once, because callers hand this three different shapes.
        self._sandbox = _as_profile(profile.sandbox)
        self._agent_configuration = agent_configuration
        self._global_configuration = global_configuration
        self._working_directory = working_directory or str(Path.home())
        self._project_directory = project_directory or self._working_directory
        # The daemon already resolved the session mode; a direct library caller falls back to the profile.
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
        # The caller's tools, granted to this session. A grant is dispatchable and its description
        # is appended to the conversation as a message, so the bound schema — and the provider
        # cache prefix — never changes. A grant may therefore be added at creation or at any
        # later moment; both are append-only.
        self._tool_grants = tuple(as_tool_grants(tools))
        # What a caller's tool is gated at: asking by default, so adding one cannot silently widen a session.
        self._supplied_tool_gate = components.supplied_tool_gate
        configured_tools = (
            list(toolset)
            if toolset is not None
            else _build_tools(
                agent_configuration,
                global_configuration,
                self._working_directory,
                can_reach_peers=session_access is not None,
                extra_tools=(),
                permission_mode=self._permission_mode,
            )
        )
        # The dispatchable units: every tool the session runs, assembled from the built-in registry
        # and the caller's own tools. A caller's tool of the same name replaces the built-in, so
        # "my own bash" wins over ours. The model binds the configured schemas; grants ride as
        # appended messages and only change who executes, keeping the cache prefix untouched.
        units: dict[str, Tool] = {}
        for tool in configured_tools:
            builtin = BUILTIN_TOOLS.get(tool.name)
            units[tool.name] = (
                builtin
                if builtin is not None and builtin.schema is tool
                else Tool(
                    name=tool.name,
                    schema=tool,
                    description=tool.description or "",
                    handler=invoke_supplied,
                )
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
        self._compaction_summarizer = components.compaction_summarizer
        # The evaluator gates against the same narrowed allow-list the tool set was built from.
        self._permissions = (
            permissions
            if permissions is not None
            else PermissionEvaluator(
                agent_configuration.model_copy(
                    update={
                        "tools_enabled": sorted(
                            _live_allow_list(
                                agent_configuration.tools_enabled,
                                {tool.name for tool in self._tools},
                            )
                        ),
                    }
                )
            )
        )
        self._background = BackgroundJobs(
            session_id=session_id,
            agent_name=agent_configuration.identifier,
            store=jobs,
        )
        # Where the audit trail goes, and who answers a gate. Both absent by default.
        self._observer = observer
        from langmesh.runtime.compaction import DirectCompactionPreparation

        self._compaction_preparation = (
            components.compaction_preparation or DirectCompactionPreparation()
        )
        from langmesh.runtime.continuation import TuningContinuationPolicy

        self._continuations = components.continuations or TuningContinuationPolicy()
        self._observation_registry_metadata = {}
        initial_observation_registry_error = None
        self._approvals = approvals
        self._transcript = transcript
        # The daemon's event publisher is optional; the registry reader is used only for compaction verification.
        self._goal_review_journal = components.goal_review_journal
        self._submitted_goal_review = None
        self._submitted_compaction_summary: Any = None
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
        if catalogue is None:
            from langmesh.base.catalogue import machine_catalogue

            catalogue = machine_catalogue(global_configuration, self._project_directory)
        self._catalogue = catalogue
        self._prompt_loader = _CataloguePrompts(catalogue)
        # Creation-time grants are described from the first turn: their messages sit at the head
        # of the conversation, before any user message, and are stable for the session's life.
        for grant in self._tool_grants:
            self._conversation.append(self._tool_grant_message(grant.tool))
        self._cached_system_prompt: str | None = None
        self._task_manager = TaskManager()
        # Independent from goal continuations: one may share a turn with the other, but neither consumes its allowance.
        self._task_continuations = 0
        self._goal: Optional[Goal] = None
        # Called when the goal changes, so the layer above can tell the daemon and the interface.
        self._on_goal_change: Optional[Callable[[Optional[Goal]], None]] = None
        self._session_revision = 0
        self._persisted_session_revision = 0
        self._execution_history: list[dict] = []
        # The permission policy as one value, resolved by the daemon before this runtime is built.
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
        # Every model must advertise its own context capacity; an unknown window means the
        # harness cannot schedule compacting or refuse an oversized request with numbers.
        self._context_window_estimated = reported_context_window == 0
        self._context_window = reported_context_window
        # Durable compaction preparation: one state value prevents contradictory phase, reason,
        # resumption, revision, and failure flags from surviving a restart.
        self._compaction_control = _CompactionControl()
        self._turn_recovery = "none"
        self._observation_registry_error: str | None = initial_observation_registry_error
        self._pending_observation_registry_feedback: str | None = initial_observation_registry_error
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
        self._on_goal_change = components.goal_listener
        # What was approved beyond the configured profile, held for the session so one grant is not re-asked.
        self._access_grants: list[Grant] = []
        # The files the person attached: like a grant, but answering what they handed over rather than what was asked.
        self._attached_files: dict[str, None] = {}
        # Per-session state a screen-control script keeps between calls, and the services bundle
        # every tool handler runs against. The bundle is the tool's only view of the runtime.
        self._screen_queries_asked: list[tuple[Any, str]] = []
        self._services = ToolServices(
            background=self._background,
            permissions=self._permissions,
            task_manager=self._task_manager,
            goal=_GoalAccess(self),
            prompt_loader=self._prompt_loader,
            catalogue=self._catalogue,
            tool_context=self._tool_context,
            access_grants=self._access_grants,
            attached_files=self._attached_files,
            turn_reader=self._turn_reader,
            record_event=self._record_event,
            mark_dirty=self._mark_session_dirty,
            abort_event=self._abort_event,
            submit_goal_review=lambda review: setattr(self, "_submitted_goal_review", review),
            submit_compaction_summary=lambda summary: setattr(
                self, "_submitted_compaction_summary", summary
            ),
            leases=_LeaseAccess(self),
            retry_gate=self.retry_gate,
            decide_retry=self.decide_retry,
            retry_refusal_result=self._retry_refusal_result,
            pipeline=self._pipeline,
            tools=lambda: self._tools,
            screen_query_log=self._screen_queries_asked,
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

    def note_observation_registry(self, metadata: dict[str, Any], error: str | None = None) -> None:
        """Adopt watcher metadata and queue a changed schema failure for the next model opening."""
        normalized_error = error.strip() if error else None
        metadata_changed = metadata != self._observation_registry_metadata
        error_changed = normalized_error != self._observation_registry_error
        if not metadata_changed and not error_changed:
            return
        if metadata_changed:
            self._observation_registry_metadata = dict(metadata)
            # The memory panel receives this revision immediately, while the model's static
            # prefix adopts it only at an explicit prompt refresh such as successful compacting.
        self._observation_registry_error = normalized_error
        if error_changed:
            self._pending_observation_registry_feedback = normalized_error

    def _take_observation_registry_feedback(self) -> str | None:
        message = self._pending_observation_registry_feedback
        self._pending_observation_registry_feedback = None
        return message

    def set_locations(self, locations: Sequence[Location] | None) -> None:
        """Adopt the workspace's environments as they are now, so one added later reaches an existing session."""
        carried = {uri: resolved.executor for uri, resolved in self._locations.items()}
        self._locations = {}
        self._locations_by_name = {}
        self._build_locations(locations, executors=carried)
        # A running prompt remains stable until its explicit refresh boundary (normally compaction).

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
    def background_jobs(self) -> BackgroundJobs:
        """This runtime's background-job runner, which the executor's resume pump reads."""
        return self._background

    def has_pending_jobs(self) -> bool:
        """Whether any background job is still in flight, without reaching into the runner's internals."""
        return self._background.has_pending()

    def has_completed_undelivered_jobs(self) -> bool:
        """Whether a completed background result is waiting to be delivered to the model."""
        return self._background.has_completed_undelivered()

    async def wait_for_jobs(self) -> None:
        """Await the next background-job completion (the resume pump's wait point)."""
        await self._background.wait_for_completion()

    def inject_stored_background_result(
        self, *, kind: str, identifier: str, tool_call_identifier: str, result: str
    ) -> None:
        """Append a restored background result, so a rebuilt runtime replays it exactly like a live completion."""
        capped_result = _cap_model_result_payload(result, code=f"{kind}_result_truncated")
        metadata = _tool_timing_metadata(
            tool_name=kind,
            tool_call_identifier=tool_call_identifier,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            duration_milliseconds=0,
            background_job_id=identifier,
        )
        status, code = _model_result_status(capped_result, ok=True, backgrounded=False)
        self._append_background_result_messages(capped_result, metadata, status, code)

    def grant_tool(self, tool: BaseTool) -> None:
        """Grant a tool to this session at any moment: dispatchable now, described to the model
        by an appended message, so the bound schema — and the provider cache prefix — is untouched.
        A grant of a name the session already runs replaces that tool's implementation."""
        if tool.name in self._supplied_tool_names:
            return
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
        self._mark_session_dirty()

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

    def _append_background_result_messages(
        self,
        content: str,
        metadata: dict[str, Any],
        status: str,
        code: str | None,
    ) -> None:
        """Append background data first and any actionable guidance second."""
        self._conversation.append(
            self._reminder_message(
                self._background_result_message(content, metadata, status, code),
                marks={"background_result": metadata, "status": status, "code": code},
            )
        )
        result_data = _maybe_json(content)
        result_code = str(result_data.get("code") or "") if isinstance(result_data, dict) else ""
        if result_code.endswith("_interrupted"):
            self._conversation.append(
                self._reminder_message(
                    self._prompt_loader.load(
                        "background_interrupted",
                        {"kind": str(metadata.get("tool_name") or "tool")},
                    ),
                    marks={
                        "background_guidance": True,
                        "background_result": metadata,
                    },
                )
            )

    def _background_result_message(
        self,
        content: str,
        metadata: dict[str, Any],
        status: str,
        code: str | None,
    ) -> str:
        """Render a background completion while keeping its machine metadata on the message envelope."""
        return self._prompt_loader.load(
            "background_result",
            {
                "tool_name": str(metadata.get("tool_name") or "background tool"),
                "job_id": str(metadata.get("background_job_id") or "unknown"),
                "status": status,
                "code": code or "none",
                "content": content,
            },
        )

    @property
    def token_usage(self) -> dict[str, int]:
        return dict(self._token_usage)

    def _accumulate_usage(self, response: AIMessage) -> TurnEvent | None:
        """Compaction one call's usage into the session total and answer a USAGE event, or ``None`` when none was reported."""
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
        # Some OpenAI-compatible gateways omit model metadata on a response. Never replace
        # a catalogued non-zero window with that absence: doing so disables automatic compacting.
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
        # Stop tears down the live turn only: detached work and peer sessions have their own lifecycles.
        # A queued steering message is superseded by the Stop: deliver its false and never let it keep the cancelled turn waiting.
        self.discard_pending_steering()
        self._stop_requested = True
        self._abort_event.set()
        self._background.cancel_foreground()
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
        self._background.cancel_all()
        for task in list(self._active_tool_tasks.values()):
            task.cancel()

    def abort_tool(self, tool_call_identifier: str) -> bool:
        task = self._active_tool_tasks.get(tool_call_identifier)
        aborted = False
        if task is not None and not task.done():
            task.cancel()
            aborted = True
        return self._background.cancel_by_tool_call(tool_call_identifier) or aborted

    def background_snapshots(self) -> list[dict[str, Any]]:
        return self._background.active_snapshots()

    def send_tool_to_background(self, tool_call_identifier: str) -> bool:
        """Push a blocking command to the background on the user's behalf, as if the model had done it."""
        return self._background.request_background(tool_call_identifier)

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
        """Adopt the mode the daemon resolved, reaching the very next tool call."""
        self._permission_mode = mode
        return mode

    def set_a2a_turn_id(self, turn_id: str) -> None:
        """Record the current turn's task id, so work raised during it can name the turn it belongs to."""
        self._a2a_turn_id = turn_id

    def set_turn_reader(self, task_reader: Callable) -> None:
        """Install the reader `read_turn` uses to fetch related turns from the store."""
        self._turn_reader = task_reader

    def unfinished_tasks(self) -> list[dict]:
        return self._task_manager.unfinished()

    def has_actionable_tasks(self) -> bool:
        return bool(self._task_manager.actionable())

    def should_continue_goal(self) -> bool:
        return self._continuations.continue_goal(
            self._goal,
            self._goal.continuations if self._goal is not None else 0,
        )

    def should_continue_tasks(self) -> bool:
        return self._continuations.continue_tasks(
            self._task_manager.actionable(),
            self._task_continuations,
        )

    def task_continuation_message(self) -> str:
        """The hidden instruction that makes unfinished tracked work an actual next turn."""
        return self._prompt_loader.load(
            "task_continuation_note",
            {"tasks": compact(self.unfinished_tasks())},
        )

    def continuation_content(self, *, goal_review: str = "", task_continuation: str = "") -> str:
        """The one message a continuation turn carries: the goal review's prose and the
        task note, composed by the shared template rather than joined in Python."""
        return self._prompt_loader.load(
            "goal_and_task_continuation",
            {"goal_review": goal_review, "task_continuation": task_continuation},
        ).strip()

    @property
    def task_continuations(self) -> int:
        return self._task_continuations

    def note_task_continuation(self) -> None:
        self._task_continuations += 1
        self._mark_session_dirty()

    def restore_task_allowance(self) -> None:
        if self._task_continuations == 0:
            return
        self._task_continuations = 0
        self._mark_session_dirty()

    def session_snapshot(self) -> dict:
        """The durable non-conversation state — the goal and the tasks — persisted beside the checkpoint."""
        return {
            "goal": self._goal.model_dump() if self._goal is not None else None,
            "tasks": self._task_manager.snapshot(),
            "task_continuations": self._task_continuations,
            "compaction": self._compaction_control.snapshot(),
            "turn_recovery": self._turn_recovery,
        }

    def restore_session(self, snapshot: dict) -> None:
        """Rehydrate goal and tasks. A goal that does not validate is dropped rather than guessed at."""
        stored = snapshot.get("goal")
        goal = None
        if isinstance(stored, dict) and str(stored.get("text", "")).strip():
            try:
                goal = Goal.model_validate(stored)
            except ValidationError:
                logger.warning("discarding a stored goal that no longer validates")
        self._goal = goal
        self._task_manager.restore(snapshot.get("tasks", {}) or {})
        self._task_continuations = max(0, int(snapshot.get("task_continuations", 0) or 0))
        self._compaction_control = _CompactionControl.restore(snapshot.get("compaction"))
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
            self._mark_session_dirty()

    def begin_turn_retry(self) -> bool:
        if self._turn_recovery != "retryable" or self._compaction_control.failure:
            return False
        self._turn_recovery = "retrying"
        self._mark_session_dirty()
        return True

    def abandon_turn_retry(self) -> None:
        """Let newly accepted user work supersede a previously failed turn."""
        if self._turn_recovery != "none":
            self._turn_recovery = "none"
            self._mark_session_dirty()

    def mark_turn_succeeded(self) -> None:
        if self._turn_recovery != "none":
            self._turn_recovery = "none"
            self._mark_session_dirty()

    @property
    def compaction_failure(self) -> str | None:
        """The failure that blocks new input until an explicit compaction retry succeeds."""
        return self._compaction_control.failure

    def _fail_compaction(self, message: str) -> None:
        self._compaction_control.fail_compaction(message)
        # Messages queued during the atomic preparation segment were never accepted into
        # the conversation. Release their senders so they can remain visibly held outside it.
        self.discard_pending_steering()
        self._mark_session_dirty()

    def _record_compaction_preparation(self) -> None:
        self._compaction_control.record()
        self._mark_session_dirty()

    def retry_compaction(self) -> str | None:
        """Reopen exactly the failed compaction phase and return the operation to drive."""
        if self._compaction_control.phase == "compaction_failed":
            self._compaction_control.retry_compaction()
            self._mark_session_dirty()
            return "compaction"
        if self._compaction_control.phase != "preparation_failed":
            return None
        # A retry gets one unambiguous preparation notice. Retain any accepted user message
        # that followed the failed private segment while removing that segment's discarded work.
        self._conversation[:] = self._without_compaction_preparation(self._conversation)
        self._begin_compaction_preparation(
            reason=self._compaction_control.reason,
            resume_after=self._compaction_control.resume_after,
        )
        return "prepare"

    def begin_compaction_preparation(self) -> bool:
        """Begin an explicit compaction's recording handshake when no other compaction state is active."""
        if self._compaction_control.failure or not self._compaction_control.idle:
            return False
        self._begin_compaction_preparation(reason="manual", resume_after=False)
        return True

    @property
    def resumes_after_compaction(self) -> bool:
        return self._compaction_control.resume_after

    @property
    def pending_compaction_reason(self) -> str:
        return self._compaction_control.reason

    @property
    def awaiting_compaction_recording(self) -> bool:
        """Whether a persisted compaction is waiting for its private recording segment to finish."""
        return self._compaction_control.waiting

    # The goal, and the four things anyone outside this class does with it.

    @property
    def goal(self) -> Optional[Goal]:
        """The session's goal, or ``None`` when it has none."""
        return self._goal

    def set_goal_listener(self, listener: Optional[Callable[[Optional[Goal]], None]]) -> None:
        """Install the callback that hears every goal change, which is how the interface learns of one."""
        self._on_goal_change = listener

    def write_goal(self, goal: Optional[Goal]) -> None:
        """Set, replace or drop the goal, and announce it. The single writer, so no path changes it silently."""
        self._goal = goal
        self._mark_session_dirty()
        if self._on_goal_change is not None:
            self._on_goal_change(goal)

    def note_goal_continuation(self) -> None:
        """Count one review-opened turn and consume the message that opened it."""
        if self._goal is None:
            return
        self.write_goal(
            self._goal.updated(
                continuations=self._goal.continuations + 1,
                review_message=None,
                review_id=None,
            )
        )

    def restore_goal_allowance(self) -> None:
        """A person spoke, so the allowance restarts and a parked goal resumes. A settled one keeps its answer."""
        goal = self._goal
        if goal is None or goal.status not in (Goal.ACTIVE, Goal.PARKED):
            return
        if goal.continuations == 0 and goal.status == Goal.ACTIVE and not goal.review_message:
            return
        self.write_goal(
            goal.updated(
                continuations=0,
                status=Goal.ACTIVE,
                review_message=None,
                review_id=None,
            )
        )

    def park_goal(self) -> None:
        """Stop working the goal until a person speaks. The goal is kept: it is still what the session is for."""
        if self._goal is None or not self._goal.is_open:
            return
        self.write_goal(self._goal.updated(status=Goal.PARKED, review_message=None, review_id=None))

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

    def _mark_session_dirty(self) -> None:
        """Advance the durable-state revision after a goal or task mutation."""
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

    def _background_result_events(self) -> list[TurnEvent]:
        events: list[TurnEvent] = []
        for completion in self._background.drain_completed():
            capped_result = _cap_model_result_payload(
                completion.result,
                code=f"{completion.kind}_result_truncated",
            )
            duration_milliseconds = int(
                (completion.completed_at - completion.started_at).total_seconds() * 1000
            )
            background_metadata = _tool_timing_metadata(
                tool_name=completion.kind,
                tool_call_identifier=completion.tool_call_identifier,
                started_at=completion.started_at,
                completed_at=completion.completed_at,
                duration_milliseconds=duration_milliseconds,
                background_job_id=completion.identifier,
            )
            # Append-only: the placeholder stays and the result lands as a new user-role reminder, keeping the prefix.
            background_status, background_code = _model_result_status(
                capped_result,
                ok=True,
                backgrounded=False,
            )
            self._append_background_result_messages(
                capped_result,
                background_metadata,
                background_status,
                background_code,
            )
            events.append(
                ToolResult(
                    id=completion.tool_call_identifier,
                    name=completion.kind,
                    result=_maybe_json(capped_result),
                    status=background_status,
                    job_id=completion.identifier,
                )
            )
            completion_event_data: dict[str, Any] = {"job_id": completion.identifier}
            if background_include_result(completion.kind):
                completion_event_data["result"] = capped_result
            self._record_event(background_completion_event(completion.kind), completion_event_data)
        return events

    def _model_supports_vision(self) -> bool:
        """Whether the model advertises image input. An unknown model is assumed capable, as elsewhere."""
        model = find_model(self.model_identifier)
        return True if model is None else model.vision

    def get_execution_history(self) -> list[dict]:
        return self._execution_history
