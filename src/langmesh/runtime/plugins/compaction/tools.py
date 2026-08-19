"""The compaction plugin's own summary-submission tool, defined where the plugin lives."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool

from langmesh.base.configuration import PromptLoader
from langmesh.base.primitives.serialization import compact
from langmesh.runtime.plugins.compaction.ports import CompactionSummary
from langmesh.runtime.tools.execution import current_tool_services
from langmesh.runtime.values import ToolStatus

#: The tool's model-facing description, read from this plugin's own prompts directory.
_DESCRIPTIONS = PromptLoader(Path(__file__).parent / "prompts")


async def _submit_compaction_summary(**arguments: Any) -> str:
    services = current_tool_services()
    services.features.invoke("submit_compaction_summary", CompactionSummary.model_validate(arguments))
    services.abort_event.set()
    return compact({"code": "compaction_summary_submitted", "status": ToolStatus.OK.value})


submit_compaction_summary = StructuredTool.from_function(
    coroutine=_submit_compaction_summary,
    name="submit_compaction_summary",
    description=_DESCRIPTIONS.load("submit_compaction_summary", {}).strip(),
    args_schema=CompactionSummary,
)
