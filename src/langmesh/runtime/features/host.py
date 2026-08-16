"""The internal host: the core capabilities the library's own plugins use.

A caller's plugin implements `Feature` hooks and receives a `PluginContext`; it never sees this.
The host carries only what the core itself is — the conversation, the boundary, the window, the
turn's machinery, and the session's bookkeeping — grouped so each capability is documented.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ConversationView:
    """The conversation the core runs."""

    model: Any  #: The chat model every model-facing call is made through.
    messages: list  #: The live conversation, which a feature may append to.


@dataclass(frozen=True)
class BoundaryView:
    """The confinement a feature is held to, and the reach approved so far."""

    sandbox: Any  #: The configured confinement the operating system will enforce.
    locations: dict  #: The resolved locations, keyed by uri.
    locations_by_name: dict  #: The resolved locations, keyed by name.
    resolve_location: Any  #: ``(location) -> ResolvedLocation``, resolving a call's location value.
    call_policy: Any  #: ``(location) -> CallExecutionPolicy`` for one call, as a value.
    granted_profile: Any  #: The confinement with every standing grant compacted in.
    access_grants: Any  #: The standing grants approved for this session.
    record_grant: Any  #: ``(grant) -> None``, recording an approved widening on the boundary.
    attached_files: Any  #: The files the person attached this session.


@dataclass(frozen=True)
class ToolsView:
    """The session's tool surface, as the model binds it and the dispatcher validates it."""

    tool_context: Any  #: The call-time tool state (mcp, grants, sandbox) handlers read.
    model_tools: list  #: The tools the model is bound to.
    tool_schemas: dict  #: The argument schemas by tool name, used to validate and coerce calls.
    supplied_tool_names: set  #: The tools the caller supplied, whose rules are the caller's.
    supplied_tool_gate: str  #: Whether a supplied tool is asked about or runs freely.
    turn_reader: Any  #: ``(task_id) -> Task``, reading a related task from the shared store.


@dataclass(frozen=True)
class WindowView:
    """The context-window accounting the loop keeps."""

    context_window: int  #: The model's advertised window, or 0 when unknown.
    latest_context_tokens: int  #: How full the context is, from the last reported call.
    set_latest_context_tokens: Any  #: ``(tokens) -> None``, adopting a new context estimate.
    refresh_cached_prompt: Any  #: ``() -> None``, invalidating the cached static prompt.


@dataclass(frozen=True)
class TurnView:
    """The machinery of the turn loop a held feature coordinates with."""

    abort_event: Any  #: Set when the user stops, so long work can end.
    discard_pending_steering: Any  #: ``() -> None``, releasing senders whose steering was dropped.
    build_static_system_prompt: Any  #: ``() -> str``, the session prompt for this model opening.
    build_turn_messages: Any  #: ``() -> list``, the static prompt and the whole conversation.
    refuse_if_over_window: Any  #: ``(messages) -> None``, raising before an oversized request leaves.
    reminder_message: Any  #: ``(content, marks) -> HumanMessage``, a harness note to the model.
    maintenance_active: Any  #: ``() -> bool``, whether any feature is currently holding the loop.
    feature_classes: Any  #: ``() -> list[type]``, the installed features' classes, for sub-sessions.


@dataclass(frozen=True)
class BookkeepingView:
    """The durable session accounting the core keeps."""

    mark_dirty: Any  #: ``() -> None``, advancing the durable-state revision after a mutation.
    record_event: Any  #: ``(type, data) -> None``, appending to the audit trail.
    session_snapshot: Any  #: ``() -> dict``, the durable state to persist beside the checkpoint.
    restore_session: Any  #: ``(snapshot) -> None``, rehydrating the durable state.


@dataclass(frozen=True)
class PluginHost:
    """The core capabilities the library's built-in plugins use. Internal to the library."""

    conversation: ConversationView
    boundary: BoundaryView
    tools: ToolsView
    window: WindowView
    turn: TurnView
    bookkeeping: BookkeepingView


__all__ = [
    "BookkeepingView",
    "BoundaryView",
    "ConversationView",
    "PluginHost",
    "ToolsView",
    "TurnView",
    "WindowView",
]