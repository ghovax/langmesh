"""The continuation plugin's own task tools, defined where the plugin lives."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain.tools import tool

from langmesh.base.configuration import PromptLoader
from langmesh.base.primitives.serialization import compact
from langmesh.runtime.features import TasksCapability
from langmesh.runtime.tools.execution import current_tool_services

#: The tools' model-facing descriptions, read from this plugin's own prompts directory.
_DESCRIPTIONS = PromptLoader(Path(__file__).parent / "prompts")


@tool
async def set_tasks(*, tasks: list[dict]) -> str:
    """Create tasks; described in descriptions/set_tasks.md."""
    services = current_tool_services()
    task_manager = services.features.require(TasksCapability).task_manager
    identifiers = task_manager.add_tasks(tasks)
    services.note_state_changed()
    return compact(
        {
            "code": "tasks_updated",
            "message": f"Created {len(identifiers)} task{'s' if len(identifiers) != 1 else ''}.",
            "tasks": task_manager.to_dict_list(),
        }
    )


@tool
async def update_tasks(*, updates: list[dict]) -> str:
    """Update tasks; described in descriptions/update_tasks.md."""
    services = current_tool_services()
    task_manager = services.features.require(TasksCapability).task_manager
    updated_ids, complaints = task_manager.update_tasks(updates)
    if updated_ids:
        services.note_state_changed()
    result: dict[str, Any] = {
        "code": "tasks_updated",
        "message": f"Updated {len(updated_ids)} task{'s' if len(updated_ids) != 1 else ''}."
        if updated_ids
        else "Nothing was updated.",
        "tasks": task_manager.to_dict_list(),
    }
    if complaints:
        result["rejected"] = complaints
        result["status"] = "error" if not updated_ids else result.get("status", "")
    return compact(result)


# The tools' model-facing descriptions are this plugin's own files, applied once at import.
set_tasks.description = _DESCRIPTIONS.load("set_tasks", {}).strip() or set_tasks.description
update_tasks.description = (
    _DESCRIPTIONS.load("update_tasks", {}).strip() or update_tasks.description
)
