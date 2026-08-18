"""The bash plugin's event-rich handler: confinement, lease, retry, and background correlation.

The handler rides the same `ToolServices` bundle every tool handler runs against, so nothing
here reaches into the runtime. It is contributed through the feature seam and the core never
names it.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, AsyncIterator

from langmesh.base import confinement as _confinement
from langmesh.base.confinement import parse_access_request
from langmesh.base.confinement.file_leases import FileLeaseConflict
from langmesh.base.configuration import PermissionDenied
from langmesh.runtime.background import (
    bind_background_jobs,
    bind_tool_call_id,
    unbind_background_jobs,
    unbind_tool_call_id,
)
from langmesh.runtime.features.plugins.bash import bash as bash_tool
from langmesh.runtime.internals import _maybe_json
from langmesh.runtime.tools import context as tool_context
from langmesh.runtime.turn_events import Error, RetryRequested, ToolResult

async def run_bash(
    services: Any, tool_arguments: dict, tool_call_identifier: str, *, retry_grant=None
) -> Any:
    """One run of the bash tool, inside whatever confinement this attempt carries."""
    if retry_grant is not None:
        tool_context.bind(tool_context.current().for_retry(retry_grant))
    background_token = bind_background_jobs(services.features.invoke("background"))
    tool_call_token = bind_tool_call_id(tool_call_identifier)
    try:
        result = await bash_tool.ainvoke(tool_arguments)
    finally:
        unbind_tool_call_id(tool_call_token)
        unbind_background_jobs(background_token)
    return _maybe_json(result)

def sandbox_denial(services: Any, result_data: Any, policy: Any) -> Any:
    """Whether a finished command looks like the operating system stopped it."""
    if not isinstance(result_data, dict):
        return None
    if result_data.get("code") == "bash_started":
        return None
    exit_code = result_data.get("returncode")
    if not isinstance(exit_code, int):
        return None
    output = " ".join(
        str(result_data.get(key, "")) for key in ("stdout", "stderr", "output", "error")
    )
    return _confinement.denial_in(
        exit_code=exit_code,
        output=output,
        attempt=_confinement.first_attempt(
            tool_context.current().sandbox,
            workspace=policy.working_directory,
        ),
    )

async def handle_bash(
    services, tool_name, tool_arguments, tool_call_identifier, decision, policy, call_site
) -> AsyncIterator[Any]:
    raw_command = tool_arguments.get("command", "")
    # A feature that owns execution targets answers with an opaque call site: forward the command through it, letting the remote draw its own boundary.
    if call_site is not None:
        executor, base_directory = call_site
        remote_argv = executor.ssh_argv(raw_command, base_directory)
        tool_arguments = dict(tool_arguments)
        tool_arguments["command"] = shlex.join(remote_argv)
        tool_context.bind(services.tool_context.for_remote())
    # `location` is the execution-target selector, resolved above; the plain tool never sees it.
    tool_arguments.pop("location", None)
    directory = policy.working_directory
    if directory and call_site is None:
        directory_path = Path(directory).expanduser()
        if not directory_path.is_absolute():
            yield Error(
                id=tool_call_identifier,
                code="invalid_working_directory",
                message=f"Working directory must be an absolute path: {directory}",
                tool=tool_name,
            )
            return
        if not directory_path.is_dir():
            yield Error(
                id=tool_call_identifier,
                code="invalid_working_directory",
                message=f"Working directory does not exist: {directory}",
                tool=tool_name,
            )
            return
        tool_context.bind(
            services.tool_context.for_directory(str(directory_path))
            .with_grants(services.access_grants)
            .with_attachments(services.attached_files)
        )
    requested, _ = parse_access_request(tool_arguments.get("access_request"))
    declared_read_only = requested is not None and requested.mutates is False

    wants_background = tool_arguments.get("background", False)
    if isinstance(wants_background, str):
        wants_background = wants_background.lower() == "true"
    if wants_background:
        try:
            services.permissions.check_bash_background()
        except PermissionDenied as denial:
            yield Error(
                id=tool_call_identifier,
                code="background_not_allowed",
                message=str(denial),
                tool=tool_name,
            )
            return

    lease_token = ""
    if not declared_read_only and call_site is None:
        try:
            lease_token = await services.leases.acquire(
                scope="worktree",
                path=services.leases.canonical_working_directory(policy.working_directory),
                description=f"mutating bash: {raw_command}",
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
        if decision.retry_grant is not None:
            result_data = await run_bash(
                services, tool_arguments, tool_call_identifier, retry_grant=decision.retry_grant
            )
            yield ToolResult(id=tool_call_identifier, name=tool_name, result=result_data)
            return
        result_data = await run_bash(services, tool_arguments, tool_call_identifier)
        model_guidance = ""
        denial = sandbox_denial(services, result_data, policy)
        if denial is not None:
            retry_gate = services.features.invoke(
                "retry_gate",
                tool_call_id=tool_call_identifier,
                command=raw_command,
                denial=denial,
                explanation=str(tool_arguments.get("explanation", "") or ""),
            )
            verdict, grant = await services.features.invoke("decide_retry", retry_gate)
            if verdict == "run" and grant is not None:
                result_data = await run_bash(
                    services, tool_arguments, tool_call_identifier, retry_grant=grant
                )
            elif verdict == "ask":
                yield RetryRequested(
                    id=tool_call_identifier,
                    command=raw_command,
                    denial_kind=denial.kind,
                    denial_evidence=denial.evidence,
                    explanation=str(tool_arguments.get("explanation", "") or ""),
                    result=result_data,
                )
                return
            else:
                result_data = services.features.invoke("retry_refusal_result", retry_gate)
                model_guidance = retry_gate.deny_message
        yield ToolResult(
            id=tool_call_identifier,
            name=tool_name,
            result=result_data,
            model_guidance=model_guidance,
        )
        if isinstance(result_data, dict) and result_data.get("code") == "bash_started":
            job_id = result_data.get("job_id", "")
            if job_id:
                services.record_event(
                    "background_bash_started", {"job_id": job_id, "command": raw_command}
                )
                if lease_token and services.features.invoke("background").add_done_callback(
                    job_id,
                    lambda _identifier, token=lease_token: services.leases.release(token),
                ):
                    lease_token = ""
    finally:
        services.leases.release(lease_token)

__all__ = ["handle_bash", "run_bash"]
