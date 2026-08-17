from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Literal

from langchain.tools import tool
from langchain_core.tools import StructuredTool
from pydantic import Field

from langmesh.base.primitives.identifiers import new_id
from langmesh.runtime.background import current_background_jobs, current_tool_call_id
from langmesh.base.primitives.tuning import Tunable, active_tuning
from langmesh.base.primitives.serialization import compact
from langmesh.runtime.tools import context as tool_context, fetching
from langmesh.runtime.tools.execution import current_tool_decision, current_tool_services
from langmesh.base.content.skills import enabled_skills
from langmesh.runtime.internals import _background_handle_kind

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

@tool
async def search_web(
    *,
    query: str,
    result_count: int = 5,
    **kwargs: Any,
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
        arguments={"query": query, "explanation": kwargs.get("explanation", ""), "result_count": result_count},
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
async def list_mcp_tools(*, server: str = "") -> str:
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
async def list_mcp_resources(*, server: str = "") -> str:
    """List a configured MCP server's resources."""
    try:
        result = await _require_mcp_server_manager().list_resources(server)
        return compact(result)
    except Exception as exception:
        return compact(
            {"code": "mcp_list_resources_error", "status": "error", "message": str(exception)}
        )

@tool
async def read_mcp_resource(*, server: str, uri: str) -> str:
    """Read one resource from a configured MCP server."""
    try:
        result = await _require_mcp_server_manager().read_resource(server, uri)
        return compact(result)
    except Exception as exception:
        return compact(
            {"code": "mcp_read_resource_error", "status": "error", "message": str(exception)}
        )

@tool
async def read_turn(*, turn_id: str = "") -> str:
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
async def fetch_url(
    *,
    url: str,
    format: Literal["markdown", "text", "html"] = "markdown",
    timeout: float = Tunable.slow_tool_sync_window.default,
    hard_deadline: float = 30,
    background: bool = False,
) -> str:
    """Fetch a page; described in descriptions/fetch_url.md."""
    services = current_tool_services()
    runner = services.features.invoke("background")
    sync_window = float(timeout or Tunable.slow_tool_sync_window.default)
    configured = tool_context.current().fetch_timeout_seconds
    hard_deadline = int(hard_deadline or configured or 30)
    job_identifier = runner.spawn(
        "fetch_url", fetching.fetch_url(url, format, hard_deadline),
        tool_call_identifier=current_tool_call_id(), detached=background,
    )
    if not background:
        completion = await runner.settle_inline(
            job_identifier, active_tuning().scale_timeout(sync_window)
        )
        if completion is not None:
            return completion.result
    return compact({"code": "fetch_url_started", "status": "running", "job_id": job_identifier})

@tool
async def download_file(
    *,
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
    runner = services.features.invoke("background")
    job_identifier = runner.spawn(
        "download_file",
        fetching.download_file(executor, url, resolved, hard_deadline),
        tool_call_identifier=current_tool_call_id(),
        detached=background,
    )
    if not background:
        completion = await runner.settle_inline(
            job_identifier, active_tuning().scale_timeout(sync_window)
        )
        if completion is not None:
            return completion.result
    return compact({"code": "download_file_started", "status": "running", "job_id": job_identifier})

@tool
async def ask_user(
    *,
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
async def load_skill(*, name: str) -> str:
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
