from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from langchain.tools import tool
from langchain_core.tools import StructuredTool

from langmesh.base.primitives.serialization import compact
from langmesh.runtime.tools import context as tool_context
from langmesh.runtime.tools.execution import current_tool_services
from langmesh.base.content.skills import enabled_skills
from langmesh.runtime.internals import _background_handle_kind

from langmesh.base.content.prompts import PackagePromptLoader

# What each tool and shared field tells the model, read from `descriptions/*.md` at import rather than inlined here.
logger = logging.getLogger(__name__)

# What an element id looks like on both surfaces, so one can be told from a description of an element.
_DESCRIPTIONS = PackagePromptLoader(Path(__file__).parent / "descriptions")


def _require_mcp_server_manager():
    manager = tool_context.current().mcp_server_manager
    if manager is None:
        raise RuntimeError("No MCP server is configured.")
    return manager


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
        result = {
            "code": "read_turn_unavailable",
            "message": "Reading turns is not available in this session.",
        }
    elif background_kind is not None:
        result = {
            "code": "not_a_readable_turn",
            "turn_id": requested_turn_id,
            "job_kind": background_kind,
        }
    else:
        task = await services.turn_reader(requested_turn_id)
        result = (
            task if task is not None else {"code": "turn_not_found", "turn_id": requested_turn_id}
        )
    return compact(result)


@tool
async def load_skill(*, name: str) -> str:
    """Load a skill; described in descriptions/load_skill.md."""
    services = current_tool_services()
    all_skills = enabled_skills(list(services.catalogue.skills()))
    match = next((skill for skill in all_skills if skill.identifier == name), None)
    if match is None:
        return compact({"code": "skill_missing", "error": f"No enabled skill named '{name}'."})
    return compact(
        {
            "code": "skill_loaded",
            "name": match.identifier,
            "title": match.display_title,
            "path": match.path,
            "content": match.body,
        }
    )


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
