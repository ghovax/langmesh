"""The durable summary model and the composeable compaction ports a caller supplies.

The fold itself lives in the `features.plugins.compaction` plugin; these are the small values a
product hands the plugin through `RuntimeComponents`: the preparation handoff ports and
a keep-recent strategy, plus the summary model the hidden summarizer's verdict tool returns.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class CompactionSummary(BaseModel):
    """The durable summary a compaction instruction asks the model to submit."""

    summary: str = Field(
        description="The entire summary of the conversation compacted away, factual and specific."
    )


class ObservationCompactionPreparation:
    """Require an observational-memory revision to advance before compacting."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def instruction(self, default: str) -> str:
        return default

    async def baseline(self) -> int:
        try:
            return await self._store.revision()
        except Exception as error:  # noqa: BLE001 — repair is the preparation turn's job
            logger.warning("observation registry requires repair before compaction: %s", error)
            return 0

    async def completed(self, baseline: Any) -> bool:
        # An absent registry has nothing to hand off, so an empty baseline is a complete handoff.
        if not baseline:
            return True
        try:
            return await self._store.revision() > int(baseline)
        except Exception:  # noqa: BLE001 — an invalid registry is not a completed checkpoint
            return False

    async def describe(self) -> dict:
        return await self._store.describe()


class DirectCompactionPreparation:
    """Compaction directly, for applications that persist no external memory handoff."""

    def instruction(self, default: str) -> None:
        return None

    async def baseline(self) -> None:
        return None

    async def completed(self, baseline: Any) -> bool:
        return True

    async def describe(self) -> dict:
        return {}


class KeepRecentTurns:
    """Keep the last `keep` exchanges and drop the rest, with no model call and no cost."""

    def __init__(self, keep: int = 20) -> None:
        if keep < 1:
            raise ValueError(f"keep must be at least 1, got {keep}.")
        self._keep = keep

    def should_compact(self, state) -> bool:
        return len(state.messages) > self._keep * 2

    async def compact(self, state) -> list:
        return list(state.messages[-self._keep * 2 :])


__all__ = [
    "CompactionSummary",
    "DirectCompactionPreparation",
    "KeepRecentTurns",
    "ObservationCompactionPreparation",
]
