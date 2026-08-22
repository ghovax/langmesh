"""Policies for autonomous goal and task continuation between model turns."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


class DefaultContinuationPolicy:
    """Open goals and unfinished tasks both keep going; nothing is a hard-turn-capped allowance."""

    def continue_goal(self, goal: Any) -> bool:
        return bool(goal is not None and goal.is_open)

    def continue_tasks(self, unfinished_tasks: Sequence[Mapping[str, Any]]) -> bool:
        return bool(unfinished_tasks)


__all__ = ["DefaultContinuationPolicy"]
