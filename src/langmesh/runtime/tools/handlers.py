"""The event-rich built-in tool handlers.

Most built-ins are fully implemented in `registry.py` and dispatched by the generic invoke path.
These few need more than a string result — an MCP call streams events, a peer-session verb
reports back — so they run through their own handlers over the same `ToolServices` bundle.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from langmesh.runtime.background import bind_background_jobs, unbind_background_jobs
from langmesh.runtime.internals import _coerce_mcp_arguments, _maybe_json
from langmesh.runtime.tools import sessions
from langmesh.runtime.tools.output import ToolOutput
from langmesh.runtime.tools.registry import call_mcp_server_tool_with_events
from langmesh.runtime.turn_events import Error, Mcp, ToolResult

logger = logging.getLogger(__name__)

async def handle_call_mcp_server_tool(
    services, tool_name, tool_arguments, tool_call_identifier, decision, policy, resolved_location
) -> AsyncIterator[Any]:
    event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def on_mcp_event(event: dict[str, Any]) -> None:
        await event_queue.put(event)

    call_task = asyncio.create_task(
        call_mcp_server_tool_with_events(
            str(tool_arguments.get("server", "")),
            str(tool_arguments.get("tool_name", "")),
            _coerce_mcp_arguments(tool_arguments.get("arguments")),
            on_mcp_event,
        )
    )
    try:
        while True:
            if call_task.done():
                while not event_queue.empty():
                    yield Mcp(
                        id=tool_call_identifier,
                        name="call_mcp_server_tool",
                        server=tool_arguments.get("server", ""),
                        tool=tool_arguments.get("tool_name", ""),
                        event=event_queue.get_nowait(),
                    )
                break
            get_task = asyncio.create_task(event_queue.get())
            done, _ = await asyncio.wait(
                {call_task, get_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if get_task in done:
                yield Mcp(
                    id=tool_call_identifier,
                    name="call_mcp_server_tool",
                    server=tool_arguments.get("server", ""),
                    tool=tool_arguments.get("tool_name", ""),
                    event=get_task.result(),
                )
            else:
                get_task.cancel()
        result_data = await call_task
    except Exception as exception:
        yield Error(id=tool_call_identifier, message=str(exception), tool=tool_name)
        return
    yield ToolResult(id=tool_call_identifier, name=tool_name, result=result_data)

async def handle_session(
    services, tool_name, tool_arguments, tool_call_identifier, decision, policy, resolved_location
) -> AsyncIterator[Any]:
    """Every peer-session verb, in one handler: they differ only in which call they make."""
    create_tool = next((tool for tool in services.tools() if getattr(tool, "name", "") == "create_session"), None)
    background_token = bind_background_jobs(services.features.invoke("background"))
    try:
        result = await sessions.invoke(tool_name, tool_arguments, create_tool)
    finally:
        unbind_background_jobs(background_token)
    model_guidance = ""
    if isinstance(result, ToolOutput):
        model_guidance = result.model_guidance
        result = result.result
    yield ToolResult(
        id=tool_call_identifier,
        name=tool_name,
        result=_maybe_json(result) if isinstance(result, str) else result,
        model_guidance=model_guidance,
    )

#: name -> handler, for the built-ins whose execution needs more than the generic invoke path.
HANDLERS: dict[str, Any] = {
    "call_mcp_server_tool": handle_call_mcp_server_tool,
    "create_session": handle_session,
    "message_session": handle_session,
    "read_session": handle_session,
    "list_sessions": handle_session,
    "list_remote_agents": handle_session,
    "message_remote_agent": handle_session,
}
