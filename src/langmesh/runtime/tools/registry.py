from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path
from typing import Any, Literal

from langchain.tools import tool
from langchain_core.tools import StructuredTool
from pydantic import Field

from langmesh.base.primitives.identifiers import new_id
from langmesh.runtime.background import current_background_jobs, current_tool_call_id
from langmesh.base.primitives.tuning import Tunable, active_tuning, clip_to_tokens
from langmesh.base.primitives.serialization import compact
from langmesh.runtime.tools import context as tool_context, fetching
from langmesh.runtime.tools.execution import current_tool_decision, current_tool_services
from langmesh.base.content.skills import enabled_skills
from langmesh.runtime.internals import _background_handle_kind
from langmesh.runtime.features.plugins.goal_review import GoalReview
from langmesh.runtime.goal import Goal
from langmesh.runtime.values import ToolStatus
from langmesh.runtime.locations import PermissionDecision
from langmesh.runtime.compaction import CompactionSummary

from langmesh.base.configuration import PromptLoader

# What each tool and shared field tells the model, read from `descriptions/*.md` at import rather than inlined here.
logger = logging.getLogger(__name__)

# What an element id looks like on both surfaces, so one can be told from a description of an element.
_DESCRIPTIONS = PromptLoader(Path(__file__).parent / "descriptions")

#: Why a call is happening, in the words the person watching reads. Every tool takes one.
EXPLANATION = _DESCRIPTIONS.load("explanation", {}).strip()

#: What a call reaches for beyond its confinement. A difference against the profile, not an inventory.
ACCESS_REQUEST = _DESCRIPTIONS.load("access_request", {}).strip()


def _require_mcp_server_manager():
    manager = tool_context.current().mcp_server_manager
    if manager is None:
        raise RuntimeError("No MCP server is configured.")
    return manager


async def _submit_goal_review(**arguments: Any) -> str:
    services = current_tool_services()
    services.submit_goal_review(GoalReview.model_validate(arguments))
    services.abort_event.set()
    return compact({"code": "goal_review_submitted", "status": ToolStatus.OK.value})


async def _permission_decision(**arguments: Any) -> str:
    services = current_tool_services()
    services.abort_event.set()
    return compact({"code": "permission_decision_submitted", "status": ToolStatus.OK.value})


async def _submit_compaction_summary(**arguments: Any) -> str:
    services = current_tool_services()
    services.submit_compaction_summary(CompactionSummary.model_validate(arguments))
    services.abort_event.set()
    return compact({"code": "compaction_summary_submitted", "status": ToolStatus.OK.value})


submit_goal_review = StructuredTool.from_function(
    coroutine=_submit_goal_review,
    name="submit_goal_review",
    description=_DESCRIPTIONS.load("submit_goal_review", {}).strip(),
    args_schema=GoalReview,
)

permission_decision = StructuredTool.from_function(
    coroutine=_permission_decision,
    name="permission_decision",
    description="Submit the automatic permission reviewer's internal verdict.",
    args_schema=PermissionDecision,
)

submit_compaction_summary = StructuredTool.from_function(
    coroutine=_submit_compaction_summary,
    name="submit_compaction_summary",
    description=_DESCRIPTIONS.load("submit_compaction_summary", {}).strip(),
    args_schema=CompactionSummary,
)


@tool
async def bash(
    *,
    explanation: str = Field(..., description=EXPLANATION),
    command: str,
    access_request: dict[str, Any] = Field(..., description=ACCESS_REQUEST),
    location: str = "",
    background: bool = False,
    timeout: float = Tunable.bash_sync_window.default,
) -> str:
    """Run a shell command inside the session's confinement."""
    from langmesh.base import confinement as _confinement

    active = tool_context.current()
    profile, workspace = active.sandbox, active.workspace
    output_path = active.spill_path("bash")
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
        process_id = process.pid
        # Persist the group id, so a subtree orphaned by a crash is reaped on the next startup.
        try:
            group = os.getpgid(process_id)
            current_background_jobs().store.record_process_group(job_id, group)
            # And tell the host, which is how a call this child makes is attributed back to this session.
            from langmesh.runtime.background import record_child_group

            record_child_group(active.session_id, group)
        except (ProcessLookupError, OSError):
            pass

        async def write_stream(stream, handle):
            while True:
                line = await stream.readline()
                if not line:
                    break
                handle.write(line.decode())
                handle.flush()

        try:
            with output_path.open("w") as file_handle:
                await asyncio.gather(
                    write_stream(process.stdout, file_handle),
                    write_stream(process.stderr, file_handle),
                )

            await process.wait()
        except asyncio.CancelledError:
            cancel_process()
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=active_tuning().duration(Tunable.sigterm_grace)
                )
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
            # Read off the loop: a large log would otherwise block every session sharing it.
            output = (
                await asyncio.to_thread(output_path.read_text, errors="replace")
                if output_path.exists()
                else ""
            )
            inline_output, output_truncated = clip_to_tokens(
                output, active_tuning().amount(Tunable.output_tokens)
            )
            payload = {
                "code": "bash_cancelled",
                "status": "error",
                "output": inline_output,
                "output_file": str(output_path),
                "truncated": output_truncated,
                "pid": process_id,
                "size": len(output),
                "returncode": process.returncode,
            }
            return compact(payload)
        # Off the loop, for the same reason: a multi-megabyte output must not stall it.
        output = await asyncio.to_thread(output_path.read_text)
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
                    "output_file": str(output_path),
                    "truncated": False,
                    "pid": process_id,
                    "size": 0,
                    "returncode": return_code,
                }
            )
        inline_output, truncated = clip_to_tokens(
            output, active_tuning().amount(Tunable.output_tokens)
        )
        result = {
            "code": result_code,
            "status": result_status,
            "output": inline_output,
            "output_file": str(output_path),
            "truncated": truncated,
            "pid": process_id,
            "size": len(output),
            "returncode": return_code,
        }
        return compact(result)

    jobs = current_background_jobs()
    job_id = jobs.spawn(
        "bash",
        run(),
        output_path=output_path,
        cancel_callback=cancel_process,
        arguments={
            "command": command,
            "location": location,
            "access_request": access_request,
            "explanation": explanation,
            "background": background,
        },
        # Correlate the job with its tool call, so a blocking foreground command can be backgrounded by that id.
        tool_call_identifier=current_tool_call_id(),
        # A backgrounded command is detached and survives a Stop; a synchronous one is killed by it.
        detached=background,
    )
    if not background:
        # Block and hand back real output, so the model never mistakes a placeholder for unfinished work.
        settled = await jobs.settle_inline(job_id, active_tuning().scale_timeout(timeout))
        if settled is not None:
            return settled.result
    return compact(
        {
            "code": "bash_started",
            "status": "running",
            "job_id": job_id,
            "output_file": str(output_path),
        }
    )


@tool
async def search_web(
    *,
    explanation: str = Field(..., description=EXPLANATION),
    query: str,
    result_count: int = 5,
) -> str:
    """Search the web."""
    client = tool_context.current().exa_client
    if client is None:
        return compact(
            {
                "code": "web_search_error",
                "status": "error",
                "message": "Web search is not configured.",
            }
        )

    # Mint the id up front, so a delivered result can be matched to the search that started it.
    job_id = new_id("search")
    output_path = tool_context.current().spill_path("search")

    async def run() -> str:
        try:
            results = await asyncio.to_thread(
                client.search,
                query,
                num_results=min(result_count, active_tuning().amount(Tunable.web_search_maximum)),
                contents={"text": True},
            )
            entries = []
            for result in results.results:
                entry = {"title": result.title, "url": result.url}
                if result.text:
                    entry["summary"] = result.text
                if result.published_date:
                    entry["published_date"] = result.published_date
                entries.append(entry)
            payload = compact(
                {
                    "code": "web_search_completed",
                    "status": "ok",
                    "job_id": job_id,
                    "query": query,
                    "results": entries,
                }
            )
            await asyncio.to_thread(output_path.write_text, payload)
            return payload
        except Exception as exception:
            payload = compact(
                {
                    "code": "web_search_error",
                    "status": "error",
                    "job_id": job_id,
                    "message": str(exception),
                }
            )
            await asyncio.to_thread(output_path.write_text, payload)
            return payload

    jobs = current_background_jobs()
    jobs.spawn(
        "search_web",
        run(),
        identifier=job_id,
        output_path=output_path,
        arguments={"query": query, "explanation": explanation, "result_count": result_count},
        # A search outliving the turn keeps running, so its result still lands and wakes the agent.
        detached=True,
    )
    # A short inline window, so the common case returns results rather than a pending handle.
    settled = await jobs.settle_inline(
        job_id, active_tuning().duration(Tunable.web_search_sync_window)
    )
    if settled is not None:
        return settled.result
    # No path or fetch-looking handle in the acknowledgement: the id is the only thing the model needs.
    return compact(
        {
            "code": "web_search_started",
            "status": "running",
            "job_id": job_id,
        }
    )


@tool
async def list_mcp_tools(
    *, explanation: str = Field(..., description=EXPLANATION), server: str = ""
) -> str:
    """List a configured MCP server's tools."""
    try:
        result = await _require_mcp_server_manager().list_tools(server)
        return compact(result)
    except Exception as exception:
        return compact(
            {"code": "mcp_list_tools_error", "status": "error", "message": str(exception)}
        )


@tool
async def call_mcp_server_tool(
    *,
    explanation: str = Field(..., description=EXPLANATION),
    server: str,
    tool_name: str,
    access_request: dict[str, Any] = Field(..., description=ACCESS_REQUEST),
    arguments: dict[str, Any] | None = None,
) -> str:
    """Call one tool on a configured MCP server."""
    try:
        result = await _require_mcp_server_manager().call_tool(server, tool_name, arguments or {})
        return compact(result)
    except Exception as exception:
        return compact(
            {"code": "mcp_server_tool_call_error", "status": "error", "message": str(exception)}
        )


async def call_mcp_server_tool_with_events(
    server: str,
    tool_name: str,
    arguments: dict[str, Any] | None,
    event_callback,
) -> dict[str, Any]:
    return await _require_mcp_server_manager().call_tool(
        server,
        tool_name,
        arguments or {},
        event_callback=event_callback,
    )


@tool
async def list_mcp_resources(
    *, explanation: str = Field(..., description=EXPLANATION), server: str = ""
) -> str:
    """List a configured MCP server's resources."""
    try:
        result = await _require_mcp_server_manager().list_resources(server)
        return compact(result)
    except Exception as exception:
        return compact(
            {"code": "mcp_list_resources_error", "status": "error", "message": str(exception)}
        )


@tool
async def read_mcp_resource(
    *, explanation: str = Field(..., description=EXPLANATION), server: str, uri: str
) -> str:
    """Read one resource from a configured MCP server."""
    try:
        result = await _require_mcp_server_manager().read_resource(server, uri)
        return compact(result)
    except Exception as exception:
        return compact(
            {"code": "mcp_read_resource_error", "status": "error", "message": str(exception)}
        )


@tool
async def read_turn(*, explanation: str = Field(..., description=EXPLANATION), turn_id: str = "") -> str:
    """Read a sibling turn; described in descriptions/read_turn.md."""
    services = current_tool_services()
    requested_turn_id = turn_id
    background_kind = _background_handle_kind(requested_turn_id)
    if services.turn_reader is None:
        result = {"code": "read_turn_unavailable", "message": "Reading turns is not available in this session."}
    elif background_kind is not None:
        result = {"code": "not_a_readable_turn", "turn_id": requested_turn_id, "job_kind": background_kind}
    else:
        task = await services.turn_reader(requested_turn_id)
        result = task if task is not None else {"code": "turn_not_found", "turn_id": requested_turn_id}
    return compact(result)


@tool
async def set_tasks(*, explanation: str = Field(..., description=EXPLANATION), tasks: list[dict]) -> str:
    """Create tasks; described in descriptions/set_tasks.md."""
    services = current_tool_services()
    identifiers = services.task_manager.add_tasks(tasks)
    services.mark_dirty()
    return compact({
        "code": "tasks_updated",
        "message": f"Created {len(identifiers)} task{'s' if len(identifiers) != 1 else ''}.",
        "tasks": services.task_manager.to_dict_list(),
    })


@tool
async def update_tasks(
    *, explanation: str = Field(..., description=EXPLANATION), updates: list[dict]
) -> str:
    """Update tasks; described in descriptions/update_tasks.md."""
    services = current_tool_services()
    updated_ids, complaints = services.task_manager.update_tasks(updates)
    if updated_ids:
        services.mark_dirty()
    result: dict[str, Any] = {
        "code": "tasks_updated",
        "message": f"Updated {len(updated_ids)} task{'s' if len(updated_ids) != 1 else ''}."
        if updated_ids else "Nothing was updated.",
        "tasks": services.task_manager.to_dict_list(),
    }
    if complaints:
        result["rejected"] = complaints
        result["status"] = "error" if not updated_ids else result.get("status", "")
    return compact(result)


@tool
async def update_goal(
    *,
    explanation: str = Field(..., description=EXPLANATION),
    goal: str,
    purpose: str,
    requirements: list[str],
) -> str:
    """Set the goal; described in descriptions/update_goal.md."""
    services = current_tool_services()
    goal_text = goal.strip()
    purpose_text = purpose.strip()
    requirement_lines = [line for line in (str(entry).strip() for entry in requirements) if line]

    def refuse(message: str) -> dict[str, Any]:
        return {"code": "goal_update_error", "status": "error", "message": message}

    if not goal_text:
        result = refuse("Say what the goal is: the end state, written so it is either true or not.")
    elif not purpose_text:
        result = refuse("Say what the end state is for, so a closed route can be told from a lost goal.")
    elif not requirement_lines:
        result = refuse("A goal needs minimum conditions: what must hold for it to be met, each one something a reader can go and check.")
    else:
        current = services.goal.current()
        services.goal.write(Goal(text=goal_text, purpose=purpose_text, requirements=requirement_lines,
                                 continuations=current.continuations if current is not None else 0))
        result = {"code": "goal_active", "goal": goal_text, "purpose": purpose_text, "requirements": requirement_lines}
        services.record_event("goal_updated", result)
    return compact(result)


@tool
async def fetch_url(
    *,
    explanation: str = Field(..., description=EXPLANATION),
    url: str,
    format: Literal["markdown", "text", "html"] = "markdown",
    timeout: float = Tunable.slow_tool_sync_window.default,
    hard_deadline: float = 30,
    background: bool = False,
) -> str:
    """Fetch a page; described in descriptions/fetch_url.md."""
    services = current_tool_services()
    sync_window = float(timeout or Tunable.slow_tool_sync_window.default)
    configured = tool_context.current().fetch_timeout_seconds
    hard_deadline = int(hard_deadline or configured or 30)
    job_identifier = services.background.spawn(
        "fetch_url", fetching.fetch_url(url, format, hard_deadline),
        tool_call_identifier=current_tool_call_id(), detached=background,
    )
    if not background:
        completion = await services.background.settle_inline(
            job_identifier, active_tuning().scale_timeout(sync_window)
        )
        if completion is not None:
            return completion.result
    return compact({"code": "fetch_url_started", "status": "running", "job_id": job_identifier})


@tool
async def download_file(
    *,
    explanation: str = Field(..., description=EXPLANATION),
    url: str,
    path: str,
    location: str = "",
    timeout: float = Tunable.slow_tool_sync_window.default,
    hard_deadline: float = 120,
    background: bool = False,
) -> str:
    """Download a file; described in descriptions/download_file.md."""
    from langmesh.locations.resolver import LocationAddress, executor_for
    from langmesh.locations import location_uri
    from langmesh.locations.location_uri import LocationTarget

    services = current_tool_services()
    sync_window = float(timeout or Tunable.slow_tool_sync_window.default)
    configured = tool_context.current().download_timeout_seconds
    hard_deadline = int(hard_deadline or configured or 120)
    target = (
        location_uri.parse(location)
        if location
        else LocationTarget(kind="local", base_directory=tool_context.current().workspace or "", user="", host="", port=0)
    )
    address = LocationAddress(
        kind=target.kind,
        base_directory=target.base_directory,
        host_alias=target.host or target.base_directory if target.kind == "remote" else "",
    )
    executor = executor_for(address)
    resolved = await asyncio.to_thread(executor.resolve, target.base_directory, path)
    job_identifier = services.background.spawn(
        "download_file",
        fetching.download_file(executor, url, resolved, hard_deadline),
        tool_call_identifier=current_tool_call_id(),
        detached=background,
    )
    if not background:
        completion = await services.background.settle_inline(
            job_identifier, active_tuning().scale_timeout(sync_window)
        )
        if completion is not None:
            return completion.result
    return compact({"code": "download_file_started", "status": "running", "job_id": job_identifier})


@tool
async def ask_user(
    *,
    explanation: str = Field(..., description=EXPLANATION),
    questions: list[dict],
) -> str:
    """Ask the user; described in descriptions/ask_user.md."""
    services = current_tool_services()
    answers = current_tool_decision().answers if current_tool_decision() is not None else None
    if isinstance(answers, dict) and answers.get("__declined__"):
        result = {
            "code": "user_declined",
            "status": "error",
            "decision": {"actor": str(answers.get("__actor__") or "person"),
                         "reason": answers.get("__reason__") or None},
        }
        services.abort_event.set()
    else:
        result = {"code": "user_answered", "answers": answers}
    return compact(result)


@tool
async def load_skill(*, explanation: str = Field(..., description=EXPLANATION), name: str) -> str:
    """Load a skill; described in descriptions/load_skill.md."""
    services = current_tool_services()
    all_skills = enabled_skills(list(services.catalogue.skills()))
    match = next((skill for skill in all_skills if skill.identifier == name), None)
    if match is None:
        return compact({"code": "skill_missing", "error": f"No enabled skill named '{name}'."})
    return compact({
        "code": "skill_loaded",
        "name": match.identifier,
        "title": match.display_title,
        "path": match.path,
        "content": match.body,
    })


# Background jobs are cancelled by whoever owns the process, since a library configures nothing unasked.


def tool_description(tool_name: str) -> str:
    """One tool's model-facing description, for a tool built too late to be given one at import."""
    text = _DESCRIPTIONS.load(tool_name, {}).strip()
    if not text:
        raise ValueError(
            f"No description file in runtime/tools/descriptions for the {tool_name!r} tool."
        )
    return text


def _apply_descriptions() -> None:
    """Give every built-in its model-facing description, and fail at import rather than ship one
    offered as an empty string. A tool with no description file must carry an inline one."""
    missing = []
    for entity in globals().values():
        if not isinstance(entity, StructuredTool):
            continue
        text = _DESCRIPTIONS.load(entity.name, {}).strip()
        if not text:
            if not (entity.description or "").strip():
                missing.append(entity.name)
            continue
        entity.description = text
    if missing:
        raise RuntimeError(
            "These tools have no description file in runtime/tools/descriptions: "
            + ", ".join(sorted(missing))
        )


_apply_descriptions()
