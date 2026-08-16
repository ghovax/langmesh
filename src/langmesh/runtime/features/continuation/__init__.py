"""Autonomous continuation: the goal and task allowances between model turns."""

from __future__ import annotations

from typing import Sequence

from langmesh.base.serialization import compact
from langmesh.runtime.features.base import FeatureServices, feature_prompts


class Continuation:
    """Whether the session keeps going on its own, by the configured policy and the allowances spent."""

    def __init__(self, services: FeatureServices) -> None:
        self._services = services
        self._prompts = feature_prompts('continuation', services.catalogue)
        # Independent from goal continuations: one may share a turn with the other, but neither consumes its allowance.
        self._task_continuations = 0

    @property
    def task_continuations(self) -> int:
        return self._task_continuations

    def should_continue_goal(self, goal) -> bool:
        return bool(
            self._services.continuation_policy.continue_goal(
                goal, goal.continuations if goal is not None else 0
            )
        )

    def should_continue_tasks(self, actionable: Sequence) -> bool:
        return bool(
            self._services.continuation_policy.continue_tasks(
                actionable, self._task_continuations
            )
        )

    def note_task_continuation(self) -> None:
        self._task_continuations += 1
        self._services.mark_dirty()

    def restore_task_allowance(self) -> None:
        if self._task_continuations == 0:
            return
        self._task_continuations = 0
        self._services.mark_dirty()

    def restore_task_continuations(self, value: int) -> None:
        """Rehydrate the durable allowance, which is never negative."""
        self._task_continuations = max(0, value)

    def task_continuation_message(self, unfinished_tasks: list) -> str:
        """The hidden instruction that makes unfinished tracked work an actual next turn."""
        return self._prompts.load(
            "task_continuation_note", {"tasks": compact(unfinished_tasks)}
        )

    def continuation_content(self, *, goal_review: str = "", task_continuation: str = "") -> str:
        """The one message a continuation turn carries: the goal review's prose and the
        task note, composed by the shared template rather than joined in Python."""
        return self._prompts.load(
            "goal_and_task_continuation",
            {"goal_review": goal_review, "task_continuation": task_continuation},
        ).strip()