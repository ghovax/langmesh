"""The Bash tool schema and its confined command execution."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

from langchain.tools import tool

from langmesh.base import confinement as _confinement
from langmesh.base.content.prompts import PackagePromptLoader
from langmesh.base.primitives.identifiers import new_id
from langmesh.base.primitives.serialization import compact
from langmesh.base.primitives.limits import current_limits, clip_to_tokens
from langmesh.runtime.background import (
    current_background_jobs,
    current_tool_call_id,
    record_child_group,
)
from langmesh.runtime.tools import context as tool_context
from langmesh.runtime.tools.execution import current_tool_services

#: The tool's model-facing description, read from this plugin's own prompts directory.
_DESCRIPTIONS = PackagePromptLoader(Path(__file__).parent / "prompts")


@tool
async def bash(
    *,
    command: str,
    background: bool = False,
    timeout: float = 60.0,
    **kwargs: Any,
) -> str:
    """Run a shell command inside the session's confinement; described in descriptions/bash.md."""
    active = tool_context.current()
    profile, workspace = active.sandbox, active.workspace
    job_id = new_id("bg")
    artifacts = current_tool_services().artifacts
    artifact_identifier = f"{job_id}-output"
    process_holder: dict[str, Any] = {}

    def cancel_process() -> None:
        process = process_holder.get("process")
        if process is None or process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception:
            try:
                process.terminate()
            except ProcessLookupError:
                return

    async def run() -> str:
        # The session's own tools ride in the environment the confinement builds, already on `PATH`.
        spawn = _confinement.spawn_recipe(
            _confinement.first_attempt(profile, workspace=workspace),
            workspace=workspace,
            extra_environment=active.child_environment(),
        )
        process = await asyncio.create_subprocess_exec(
            # Still a shell command; the working directory is the process's own, not a `cd` the model can escape.
            *_confinement.resolve_command(command, spawn),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace or None,
            env=spawn.environment,
            preexec_fn=spawn.preexec,
            # A new session denies terminal prompts while still giving `killpg` a dedicated group.
            start_new_session=True,
        )
        process_holder["process"] = process
        try:
            writer = await artifacts.create(
                f"{job_id}.log", "text/plain", identifier=artifact_identifier
            )
        except BaseException:
            cancel_process()
            try:
                await asyncio.wait_for(process.wait(), timeout=current_limits().sigterm_grace)
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            raise
        artifact = None

        async def finish_output():
            nonlocal artifact
            if artifact is None:
                artifact = await writer.close()
            return artifact

        async def read_output() -> str:
            reference = await finish_output()
            content = await artifacts.read(reference.identifier) or b""
            return content.decode(errors="replace")

        process_id = process.pid
        # Persist the group id, so a subtree orphaned by a crash is reaped on the next startup.
        try:
            group = os.getpgid(process_id)
            current_background_jobs().store.record_process_group(job_id, group)
            # And tell the host, which is how a call this child makes is attributed back to this session.
            record_child_group(active.session_id, group)
        except (ProcessLookupError, OSError):
            pass

        async def write_stream(stream):
            while True:
                line = await stream.readline()
                if not line:
                    break
                await writer.write(line)

        try:
            await asyncio.gather(write_stream(process.stdout), write_stream(process.stderr))
            await process.wait()
        except asyncio.CancelledError:
            cancel_process()
            try:
                await asyncio.wait_for(process.wait(), timeout=current_limits().sigterm_grace)
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except Exception:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.wait()
            output = await read_output()
            inline_output, output_truncated = clip_to_tokens(output, current_limits().output_tokens)
            payload = {
                "code": "bash_cancelled",
                "status": "error",
                "output": inline_output,
                "output_artifact": artifact_identifier,
                "truncated": output_truncated,
                "pid": process_id,
                "size": len(output),
                "returncode": process.returncode,
            }
            return compact(payload)
        except BaseException:
            await finish_output()
            raise
        output = await read_output()
        # A non-zero exit is a failure the model must see, or `exit 7` reads as success.
        return_code = process.returncode or 0
        result_code = "bash_completed" if return_code == 0 else "bash_failed"
        result_status = "ok" if return_code == 0 else "error"
        if not output:
            return compact(
                {
                    "code": result_code,
                    "status": result_status,
                    "output": "",
                    "output_artifact": artifact_identifier,
                    "truncated": False,
                    "pid": process_id,
                    "size": 0,
                    "returncode": return_code,
                }
            )
        inline_output, truncated = clip_to_tokens(output, current_limits().output_tokens)
        result = {
            "code": result_code,
            "status": result_status,
            "output": inline_output,
            "output_artifact": artifact_identifier,
            "truncated": truncated,
            "pid": process_id,
            "size": len(output),
            "returncode": return_code,
        }
        return compact(result)

    jobs = current_background_jobs()
    jobs.spawn(
        "bash",
        run(),
        identifier=job_id,
        cancel_callback=cancel_process,
        arguments={
            "command": command,
            "access_request": kwargs.get("access_request", {}),
            "explanation": kwargs.get("explanation", ""),
            "background": background,
        },
        # Correlate the job with its tool call, so a blocking foreground command can be backgrounded by that id.
        tool_call_identifier=current_tool_call_id(),
        # A backgrounded command is detached and survives a Stop; a synchronous one is killed by it.
        detached=background,
    )
    if not background:
        # Block and hand back real output, so the model never mistakes a placeholder for unfinished work.
        settled = await jobs.settle_inline(job_id, timeout)
        if settled is not None:
            return settled.result
    return compact(
        {
            "code": "bash_started",
            "status": "running",
            "job_id": job_id,
            "output_artifact": artifact_identifier,
        }
    )


# The tool's model-facing description is this plugin's own file, applied once at import.
bash.description = _DESCRIPTIONS.load("bash", {}).strip() or bash.description

__all__ = ["bash"]
