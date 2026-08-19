"""The permission reviewer's own verdict tool, defined where the plugin lives."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool

from langmesh.base.configuration import PromptLoader
from langmesh.base.primitives.serialization import compact
from langmesh.runtime.locations import PermissionDecision
from langmesh.runtime.tools.execution import current_tool_services
from langmesh.runtime.values import ToolStatus

#: The tool's model-facing description, read from this plugin's own prompts directory.
_DESCRIPTIONS = PromptLoader(Path(__file__).parent / "prompts")


async def _permission_decision(**arguments: Any) -> str:
    services = current_tool_services()
    services.abort_event.set()
    return compact({"code": "permission_decision_submitted", "status": ToolStatus.OK.value})


permission_decision = StructuredTool.from_function(
    coroutine=_permission_decision,
    name="permission_decision",
    description=_DESCRIPTIONS.load("permission_decision", {}).strip(),
    args_schema=PermissionDecision,
)
