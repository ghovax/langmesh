"""The event-rich built-in tool handlers.

Most built-ins are fully implemented in `registry.py` and dispatched by the generic invoke path.
These few need more than a string result — an MCP call streams events, a web search carries
guidance, a download resolves an execution location — so they run through their own handlers
over the same `ToolServices` bundle.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from langmesh.base.confinement.file_leases import FileLeaseConflict
from langmesh.base.primitives.tuning import Tunable, active_tuning
from langmesh.runtime.background import bind_background_jobs, unbind_background_jobs
from langmesh.runtime.internals import _coerce_mcp_arguments, _maybe_json
from langmesh.runtime.tools import context as tool_context
from langmesh.runtime.tools import fetching, sessions
from langmesh.runtime.tools.registry import (
    call_mcp_server_tool_with_events,
    search_web as search_web_tool,
)
from langmesh.runtime.turn_events import Error, Mcp, ToolResult

logger = logging.getLogger(__name__)

async def run_slow_tool(
    services: Any,
    tool_name: str,
    tool_call_identifier: str,
    operation,
    *,
    started_code: str,
    sync_window: float,
    background: bool,
) -> AsyncIterator[Any]:
    """Run a slow tool inline briefly, then return its background handle if it is still running."""
    runner = services.features.invoke("background")
    job_identifier = runner.spawn(
        tool_name,
        operation,
        tool_call_identifier=tool_call_identifier,
        detached=background,
    )
    completion = None
    if not background:
        completion = await runner.settle_inline(
            job_identifier,
            active_tuning().scale_timeout(sync_window),
        )
    if completion is not None:
        yield ToolResult(
            id=tool_call_identifier,
            name=tool_name,
            result=_maybe_json(completion.result),
        )
        return
    yield ToolResult(
        id=tool_call_identifier,
        name=tool_name,
        result={"code": started_code, "status": "running", "job_id": job_identifier},
    )

def confinement_refusal(services: Any, resolved: str, policy: Any, *, writing: bool) -> str:
    """Why the confinement refuses this path. The file tools run in-process, so the profile is applied by hand."""
    if policy.is_remote or not resolved:
        return ""
    profile = tool_context.current().sandbox
    if profile is None:
        return ""
    workspace = policy.working_directory
    permitted = (
        profile.may_write(resolved, workspace=workspace)
        if writing
        else profile.may_read(resolved, workspace=workspace)
    )
    if permitted:
        return ""
    return services.prompt_loader.load(
        "outside_confinement",
        {"path": resolved, "action": "write" if writing else "read"},
    )

async def handle_download_file(
    services, tool_name, tool_arguments, tool_call_identifier, decision, policy, resolved_location
) -> AsyncIterator[Any]:
    assert resolved_location is not None
    executor = resolved_location.executor
    url = str(tool_arguments.get("url", ""))
    destination = str(tool_arguments.get("path", ""))
    sync_window = float(
        tool_arguments.get("timeout", Tunable.slow_tool_sync_window.default)
        or Tunable.slow_tool_sync_window.default
    )
    configured = tool_context.current().download_timeout_seconds
    hard_deadline = int(tool_arguments.get("hard_deadline", configured) or configured)
    background = bool(tool_arguments.get("background", False))
    resolved = await asyncio.to_thread(
        executor.resolve, resolved_location.base_directory, destination
    )
    refusal = confinement_refusal(services, resolved, policy, writing=True)
    if refusal:
        yield Error(
            id=tool_call_identifier, code="outside_confinement", message=refusal, tool=tool_name
        )
        return
    lease_token = ""
    if not policy.is_remote:
        try:
            lease_token = await services.leases.acquire(
                scope="file",
                path=resolved,
                description=f"{tool_name}: {resolved}",
                working_directory=policy.working_directory,
            )
        except FileLeaseConflict as exception:
            yield Error(
                id=tool_call_identifier,
                code="filesystem_lease_conflict",
                message=str(exception),
                tool=tool_name,
            )
            return
    try:
        backgrounded_job_id = ""
        async for event in run_slow_tool(
            services,
            tool_name,
            tool_call_identifier,
            fetching.download_file(executor, url, resolved, hard_deadline),
            started_code="download_file_started",
            sync_window=sync_window,
            background=background,
        ):
            if (
                isinstance(event, ToolResult)
                and isinstance(event.result, dict)
                and event.result.get("code") == "download_file_started"
            ):
                backgrounded_job_id = str(event.result.get("job_id", ""))
            yield event
        if (
            lease_token
            and backgrounded_job_id
            and services.features.invoke("background").add_done_callback(
                backgrounded_job_id,
                lambda _identifier, token=lease_token: services.leases.release(token),
            )
        ):
            lease_token = ""
    finally:
        services.leases.release(lease_token)

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
    from langmesh.runtime.tools.output import ToolOutput

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

async def handle_search_web(
    services, tool_name, tool_arguments, tool_call_identifier, decision, policy, resolved_location
) -> AsyncIterator[Any]:
    background_token = bind_background_jobs(services.features.invoke("background"))
    try:
        result = await search_web_tool.ainvoke(tool_arguments)
    finally:
        unbind_background_jobs(background_token)
    result_data = _maybe_json(result)
    model_guidance = ""
    if isinstance(result_data, dict) and result_data.get("code") == "web_search_started":
        model_guidance = services.prompt_loader.load("web_search_started_note", {})
    yield ToolResult(
        id=tool_call_identifier,
        name=tool_name,
        result=result_data,
        model_guidance=model_guidance,
    )

#: name -> handler, for the built-ins whose execution needs more than the generic invoke path.
HANDLERS: dict[str, Any] = {
    "download_file": handle_download_file,
    "search_web": handle_search_web,
    "call_mcp_server_tool": handle_call_mcp_server_tool,
    "create_session": handle_session,
    "message_session": handle_session,
    "read_session": handle_session,
    "list_sessions": handle_session,
    "list_remote_agents": handle_session,
    "message_remote_agent": handle_session,
}
