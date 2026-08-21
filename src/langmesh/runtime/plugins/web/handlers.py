"""The web plugin's event-rich handlers: search and download need more than a plain result."""

from __future__ import annotations

import os
from typing import Any, AsyncIterator

from langmesh.base.contracts.ports import FileLeaseConflict
from langmesh.base.primitives.limits import current_limits
from langmesh.runtime.background import bind_background_jobs, unbind_background_jobs
from langmesh.runtime.features import BackgroundCapability
from langmesh.runtime.internals import _maybe_json
from langmesh.runtime.tools import context as tool_context, fetching
from langmesh.runtime.tools.execution import ToolExecution
from langmesh.runtime.turn_events import Error, ToolResult


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
    runner = services.features.require(BackgroundCapability).runner
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
            sync_window,
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
    profile = tool_context.current().sandbox
    if profile is None or not resolved:
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


async def handle_download_file(execution: ToolExecution) -> AsyncIterator[Any]:
    services = execution.services
    tool_name = execution.name
    tool_arguments = execution.arguments
    tool_call_identifier = execution.call_id
    policy = execution.policy
    url = str(tool_arguments.get("url", ""))
    destination = str(tool_arguments.get("path", ""))
    sync_window = float(
        tool_arguments.get("timeout", current_limits().slow_tool_sync_window)
        or current_limits().slow_tool_sync_window
    )
    configured = tool_context.current().download_timeout_seconds
    hard_deadline = int(tool_arguments.get("hard_deadline", configured) or configured)
    background = bool(tool_arguments.get("background", False))
    resolved = os.path.abspath(os.path.join(policy.working_directory, destination))
    refusal = confinement_refusal(services, resolved, policy, writing=True)
    if refusal:
        yield Error(
            id=tool_call_identifier, code="outside_confinement", message=refusal, tool=tool_name
        )
        return
    lease_token = ""
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
            fetching.download_file(url, resolved, hard_deadline),
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
            and services.features.require(BackgroundCapability).runner.add_done_callback(
                backgrounded_job_id,
                lambda _identifier, token=lease_token: services.leases.release(token),
            )
        ):
            lease_token = ""
    finally:
        services.leases.release(lease_token)


async def handle_search_web(execution: ToolExecution) -> AsyncIterator[Any]:
    services = execution.services
    from langmesh.runtime.plugins.web.tools import search_web as search_web_tool

    background_token = bind_background_jobs(services.features.require(BackgroundCapability).runner)
    try:
        result = await search_web_tool.ainvoke(execution.arguments)
    finally:
        unbind_background_jobs(background_token)
    result_data = _maybe_json(result)
    model_guidance = ""
    if isinstance(result_data, dict) and result_data.get("code") == "web_search_started":
        model_guidance = services.prompt_loader.load("web_search_started_note", {})
    yield ToolResult(
        id=execution.call_id,
        name=execution.name,
        result=result_data,
        model_guidance=model_guidance,
    )
