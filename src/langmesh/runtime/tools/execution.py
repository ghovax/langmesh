"""The tool execution contract: one unit per tool, dispatched by the runtime without any name table.

A built-in or caller-supplied tool is a single `Tool`: its model-facing schema, its description,
and the handler that runs it. The runtime holds only a set of these units and calls
`tool.handler(services, ...)` for every call, so a tool can be added, replaced, or disabled
without touching the runtime. The handler is parameterized by an explicit `ToolServices` bundle
rather than reaching into the runtime, and the bundle also rides in a context variable so a
schema tool invoked directly (`.ainvoke`) is still fully functional.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Sequence

from langchain_core.tools import BaseTool

from langmesh.base.contracts.ports import ToolInvocation
from langmesh.runtime.turn_events import Error, ToolResult
from langmesh.runtime.values import ToolStatus


#: What one tool call is handed when it runs: the services, name, arguments, call id, decision,
#: policy, and resolved location. Each handler yields the events that become the transcript.
ToolHandler = Callable[
    [Any, str, dict, str, Any, Any, Any],
    AsyncIterator[Any],
]


@dataclass(frozen=True)
class Tool:
    """One executable tool: the schema the model binds, and the handler that runs it."""

    name: str
    schema: BaseTool
    description: str
    handler: ToolHandler


@dataclass
class ToolServices:
    """The explicit services a tool handler may use, supplied by the runtime at dispatch time."""

    features: Any
    permissions: Any
    prompt_loader: Any
    catalogue: Any
    tool_context: Any
    access_grants: Sequence[Any] = field(default_factory=tuple)
    attached_files: dict[str, None] = field(default_factory=dict)
    turn_reader: Any = None
    record_event: Any = None
    note_state_changed: Any = None
    abort_event: Any = None
    leases: Any = None
    pipeline: Any = None
    tools: Any = None
    project_directory: str = ""


# The services bound around the current dispatch, so a schema tool invoked directly (`.ainvoke`) resolves the same handler the runtime's generic dispatch uses.
_current_tool_services: contextvars.ContextVar[ToolServices | None] = contextvars.ContextVar(
    "current_tool_services", default=None
)


def bind_tool_services(services: ToolServices) -> contextvars.Token:
    return _current_tool_services.set(services)


def unbind_tool_services(token: contextvars.Token) -> None:
    _current_tool_services.reset(token)


def current_tool_services() -> ToolServices:
    services = _current_tool_services.get()
    if services is None:
        raise RuntimeError("No tool services are bound to the current context.")
    return services


# The permission decision for the call currently being dispatched, read by a tool whose behavior depends on it (which screen primitives may run, what an ask_user answers).
_current_tool_decision: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "current_tool_decision", default=None
)


def bind_tool_decision(decision: Any) -> contextvars.Token:
    return _current_tool_decision.set(decision)


def unbind_tool_decision(token: contextvars.Token) -> None:
    _current_tool_decision.reset(token)


def current_tool_decision() -> Any:
    return _current_tool_decision.get()


async def invoke_supplied(
    services: ToolServices,
    tool_name: str,
    tool_arguments: dict,
    tool_call_identifier: str,
    decision: Any,
    policy: Any,
    resolved_location: Any,
) -> AsyncIterator[Any]:
    """Run a caller-supplied tool through LangChain's own invocation, wrapped by the middleware pipeline."""
    tool = None
    if services.tools is not None:
        for candidate in services.tools():
            if getattr(candidate, "name", "") == tool_name:
                tool = candidate
                break
    if tool is None:
        yield Error(
            id=tool_call_identifier,
            message=f"Unknown tool '{tool_name}'",
            tool=tool_name,
        )
        return
    try:
        if services.pipeline is None:
            result = await tool.ainvoke(tool_arguments)
        else:
            result = await services.pipeline.run(
                ToolInvocation(name=tool_name, arguments=tool_arguments),
                lambda made: tool.ainvoke(made.arguments),
            )
    except Exception as error:  # noqa: BLE001 — a caller's tool failing is a tool result
        yield Error(id=tool_call_identifier, message=str(error), tool=tool_name)
        return
    yield ToolResult(
        id=tool_call_identifier,
        name=tool_name,
        result=result,
        status=ToolStatus.OK.value,
    )


__all__ = [
    "Tool",
    "ToolHandler",
    "ToolServices",
    "bind_tool_decision",
    "bind_tool_services",
    "current_tool_decision",
    "current_tool_services",
    "invoke_supplied",
    "unbind_tool_decision",
    "unbind_tool_services",
]
