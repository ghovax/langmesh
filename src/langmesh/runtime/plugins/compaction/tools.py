"""The compaction plugin's own summary-submission tool, defined where the plugin lives."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool

from langmesh.base.content.prompts import PackagePromptLoader
from langmesh.runtime.plugins.compaction.ports import CompactionSummary

#: The tool's model-facing description, read from this plugin's own prompts directory.
_DESCRIPTIONS = PackagePromptLoader(Path(__file__).parent / "prompts")


async def _submit_compaction_summary(**arguments: Any) -> str:
    """Validate the model-facing schema if a tool runner invokes the bound declaration."""
    return CompactionSummary.model_validate(arguments).summary


submit_compaction_summary = StructuredTool.from_function(
    coroutine=_submit_compaction_summary,
    name="submit_compaction_summary",
    description=_DESCRIPTIONS.load("submit_compaction_summary", {}).strip(),
    args_schema=CompactionSummary,
)
