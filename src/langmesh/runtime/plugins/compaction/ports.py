"""The compaction plugin's own ports a caller may supply.

The fold itself lives in this plugin; these are the small values a product hands it: the
preparation handoff ports, a keep-recent strategy, and the summary model the hidden
summarizer's verdict tool returns.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

class CompactionSummary(BaseModel):
    """The durable summary a compaction instruction asks the model to submit."""

    summary: str = Field(
        description="The entire summary of the conversation compacted away, factual and specific."
    )


class DirectCompactionPreparation:
    """Compaction directly, for applications that persist no external preparation."""

    def instruction(self, default: str) -> None:
        return None

    async def baseline(self) -> None:
        return None

    async def completed(self, baseline: Any) -> bool:
        return True

    async def describe(self) -> dict:
        return {}


__all__ = [
    "CompactionSummary",
    "DirectCompactionPreparation",
]
