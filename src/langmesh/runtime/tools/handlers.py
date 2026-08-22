"""The event-rich built-in tool handlers.

Most built-ins are fully implemented in `registry.py` and dispatched by the generic invoke path.
These few need more than a string result — an MCP call streams events, a peer-session verb
reports back — so they run through their own handlers over the same `ToolServices` bundle.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any, AsyncIterator

from langmesh.runtime.background import bind_background_jobs, unbind_background_jobs
from langmesh.runtime.features import BackgroundCapability
from langmesh.runtime.internals import _coerce_mcp_arguments, _maybe_json
from langmesh.runtime.tools import sessions
from langmesh.runtime.tools.execution import ToolExecution
from langmesh.runtime.tools.ingest import ingest_paths
from langmesh.runtime.tools.output import ToolOutput
from langmesh.runtime.tools.registry import call_mcp_server_tool_with_events
from langmesh.runtime.turn_events import Error, MCPEvent, ToolResult

logger = logging.getLogger(__name__)


async def handle_call_mcp_server_tool(execution: ToolExecution) -> AsyncIterator[Any]:
    tool_arguments = execution.arguments
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
    get_task: asyncio.Task | None = None
    try:
        while True:
            if call_task.done():
                while not event_queue.empty():
                    yield MCPEvent(
                        id=execution.call_id,
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
                yield MCPEvent(
                    id=execution.call_id,
                    name="call_mcp_server_tool",
                    server=tool_arguments.get("server", ""),
                    tool=tool_arguments.get("tool_name", ""),
                    event=get_task.result(),
                )
            else:
                get_task.cancel()
        result_data = await call_task
    except asyncio.CancelledError:
        if get_task is not None and not get_task.done():
            get_task.cancel()
        call_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await call_task
        raise
    except Exception as exception:
        yield Error(id=execution.call_id, message=str(exception), tool=execution.name)
        return
    yield ToolResult(id=execution.call_id, name=execution.name, result=result_data)


async def handle_session(execution: ToolExecution) -> AsyncIterator[Any]:
    """Every peer-session verb, in one handler: they differ only in which call they make."""
    services = execution.services
    create_tool = next(
        (tool for tool in services.tools() if getattr(tool, "name", "") == "create_session"), None
    )
    background_token = bind_background_jobs(services.features.require(BackgroundCapability).runner)
    try:
        result = await sessions.invoke(execution.name, execution.arguments, create_tool)
    finally:
        unbind_background_jobs(background_token)
    model_guidance = ""
    if isinstance(result, ToolOutput):
        model_guidance = result.model_guidance
        result = result.result
    yield ToolResult(
        id=execution.call_id,
        name=execution.name,
        result=_maybe_json(result) if isinstance(result, str) else result,
        model_guidance=model_guidance,
    )


async def handle_read_paths(execution: ToolExecution) -> AsyncIterator[Any]:
    result, image_blocks = ingest_paths(list(execution.arguments.get("paths") or []))
    extra = {"content_blocks": image_blocks} if image_blocks else {}
    yield ToolResult(
        id=execution.call_id,
        name=execution.name,
        result=result,
        extra=extra,
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
    "read_paths": handle_read_paths,
}
