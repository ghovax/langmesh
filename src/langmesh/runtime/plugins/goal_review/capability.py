"""The structural contract supplied by the goal-review plugin."""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class GoalCapability(Protocol):
    @property
    def goal(self) -> Any: ...

    def write(self, goal: Any) -> None: ...

    def submit(self, review: Any) -> None: ...


__all__ = ["GoalCapability"]
