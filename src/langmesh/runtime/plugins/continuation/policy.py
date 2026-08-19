"""Policies for autonomous goal and task continuation between model turns."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from langmesh.base.primitives.limits import current_limits


class DefaultContinuationPolicy:
    """The plain goal and task allowances, read from the current limits."""

    def continue_goal(self, goal: Any, completed_turns: int) -> bool:
        return bool(
            goal is not None
            and goal.is_open
            and completed_turns < current_limits().goal_continuation_turns
        )

    def continue_tasks(
        self,
        unfinished_tasks: Sequence[Mapping[str, Any]],
        completed_turns: int,
    ) -> bool:
        return bool(
            unfinished_tasks
            and completed_turns < current_limits().task_continuation_turns
        )


__all__ = ["DefaultContinuationPolicy"]
