"""The continuation plugin's own task tools, defined where the plugin lives."""

from __future__ import annotations

from typing import Any

from langchain.tools import tool
from pydantic import Field

from langmesh.base.primitives.serialization import compact
from langmesh.runtime.tools.execution import current_tool_services
from langmesh.runtime.tools.registry import EXPLANATION


@tool
async def set_tasks(*, explanation: str = Field(..., description=EXPLANATION), tasks: list[dict]) -> str:
    """Create tasks; described in descriptions/set_tasks.md."""
    services = current_tool_services()
    task_manager = services.features.invoke("task_manager")
    identifiers = task_manager.add_tasks(tasks)
    services.note_state_changed()
    return compact({
        "code": "tasks_updated",
        "message": f"Created {len(identifiers)} task{'s' if len(identifiers) != 1 else ''}.",
        "tasks": task_manager.to_dict_list(),
    })


@tool
async def update_tasks(
    *, explanation: str = Field(..., description=EXPLANATION), updates: list[dict]
) -> str:
    """Update tasks; described in descriptions/update_tasks.md."""
    services = current_tool_services()
    task_manager = services.features.invoke("task_manager")
    updated_ids, complaints = task_manager.update_tasks(updates)
    if updated_ids:
        services.note_state_changed()
    result: dict[str, Any] = {
        "code": "tasks_updated",
        "message": f"Updated {len(updated_ids)} task{'s' if len(updated_ids) != 1 else ''}."
        if updated_ids else "Nothing was updated.",
        "tasks": task_manager.to_dict_list(),
    }
    if complaints:
        result["rejected"] = complaints
        result["status"] = "error" if not updated_ids else result.get("status", "")
    return compact(result)
