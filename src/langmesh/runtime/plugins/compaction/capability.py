"""The structural contract supplied by the compaction plugin."""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CompactionCapability(Protocol):
    """The summary handoff exposed only inside the compaction plugin."""

    def submit_summary(self, summary: Any) -> None: ...


__all__ = ["CompactionCapability"]
