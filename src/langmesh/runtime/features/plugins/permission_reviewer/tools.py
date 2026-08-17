"""The permission reviewer's own verdict tool, defined where the plugin lives."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from langmesh.base.primitives.serialization import compact
from langmesh.runtime.locations import PermissionDecision
from langmesh.runtime.tools.execution import current_tool_services
from langmesh.runtime.values import ToolStatus


async def _permission_decision(**arguments: Any) -> str:
    services = current_tool_services()
    services.abort_event.set()
    return compact({"code": "permission_decision_submitted", "status": ToolStatus.OK.value})


permission_decision = StructuredTool.from_function(
    coroutine=_permission_decision,
    name="permission_decision",
    description="Submit the automatic permission reviewer's internal verdict.",
    args_schema=PermissionDecision,
)
