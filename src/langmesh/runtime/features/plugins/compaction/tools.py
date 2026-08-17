"""The compaction plugin's own summary-submission tool, defined where the plugin lives."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from langmesh.base.primitives.serialization import compact
from langmesh.runtime.compaction import CompactionSummary
from langmesh.runtime.tools.execution import current_tool_services
from langmesh.runtime.values import ToolStatus


async def _submit_compaction_summary(**arguments: Any) -> str:
    services = current_tool_services()
    services.features.invoke("submit_compaction_summary", CompactionSummary.model_validate(arguments))
    services.abort_event.set()
    return compact({"code": "compaction_summary_submitted", "status": ToolStatus.OK.value})


submit_compaction_summary = StructuredTool.from_function(
    coroutine=_submit_compaction_summary,
    name="submit_compaction_summary",
    description="Submit the compaction summary.",
    args_schema=CompactionSummary,
)
