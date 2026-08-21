"""The continuation plugin: the tracked tasks and the autonomous goal and task allowances.

Whether the session keeps going on its own is one pluggable concern, and the task list that
drives it lives here, never in the runtime core. The goal is passed in by the harness, which
reads it from the goal plugin, so no feature needs to know another.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Sequence

from langmesh.base.primitives.serialization import compact
from langmesh.runtime.tasks import TaskManager, TaskState
from langmesh.runtime.features import Feature, PluginContext, PluginHost
from langmesh.runtime.plugins.continuation.policy import DefaultContinuationPolicy
from langmesh.runtime.plugins.continuation.tools import set_tasks, update_tasks


@dataclass(frozen=True)
class ContinuationState:
    """The plugin's task list and consumed autonomous-turn allowance."""

    tasks: TaskState
    task_continuations: int = 0


class Continuation(Feature):
    """Whether the session keeps going on its own, by the configured policy and the allowances spent."""

    def __init__(self, *, policy: Any = None) -> None:
        self._policy = policy if policy is not None else DefaultContinuationPolicy()
        # Independent from goal continuations: one may share a turn with the other, but neither consumes its allowance.
        self._task_continuations = 0
        self._task_manager = TaskManager()

    def attach(self, context: PluginContext, host: PluginHost) -> None:
        self._context = context
        self._host = host
        self._prompts = context.prompts("continuation")

    @property
    def task_manager(self):
        return self._task_manager

    def unfinished(self) -> list[dict]:
        return self._task_manager.unfinished()

    def actionable(self) -> list[dict]:
        return self._task_manager.actionable()

    @property
    def task_continuations(self) -> int:
        return self._task_continuations

    def should_continue_goal(self, goal) -> bool:
        return bool(self._policy.continue_goal(goal, goal.continuations if goal is not None else 0))

    def should_continue_tasks(self, actionable: Sequence) -> bool:
        return bool(self._policy.continue_tasks(actionable, self._task_continuations))

    def note_task_continuation(self) -> None:
        self._task_continuations += 1
        self._host.bookkeeping.note_state_changed()

    def restore_task_allowance(self) -> None:
        if self._task_continuations == 0:
            return
        self._task_continuations = 0
        self._host.bookkeeping.note_state_changed()

    def restore_task_continuations(self, value: int) -> None:
        """Rehydrate the durable allowance, which is never negative."""
        self._task_continuations = max(0, value)

    def task_continuation_message(self, unfinished_tasks: list) -> str:
        """The hidden instruction that makes unfinished tracked work an actual next turn."""
        return self._prompts.load("task_continuation_note", {"tasks": compact(unfinished_tasks)})

    def continuation_content(self, *, goal_review: str = "", task_continuation: str = "") -> str:
        """The one message a continuation turn carries: the goal review's prose and the
        task note, composed by the shared template rather than joined in Python."""
        return self._prompts.load(
            "goal_and_task_continuation",
            {"goal_review": goal_review, "task_continuation": task_continuation},
        ).strip()

    def compose_context(self, context: dict) -> None:
        """The tracked tasks as the model sees them."""
        context["tasks"] = self._task_manager.to_dict_list()

    def contribute_tools(self) -> list:
        """The task-list tools this plugin owns."""
        return [set_tasks, update_tasks]

    def compose_prompt(self, variables: dict[str, str]) -> None:
        """Place stable task guidance in the session prompt once so later calls remain append-only."""
        variables["task_guidance"] = self._prompts.load("task_guidance", {}).strip()

    def snapshot(self) -> ContinuationState:
        return ContinuationState(self._task_manager.snapshot(), self._task_continuations)

    def restore(self, state: object) -> None:
        if isinstance(state, ContinuationState):
            tasks = state.tasks
            task_continuations = state.task_continuations
        elif isinstance(state, Mapping):
            tasks = state.get("tasks", {})
            task_continuations = int(state.get("task_continuations", 0) or 0)
        else:
            return
        self._task_manager.restore(tasks)
        self.restore_task_continuations(task_continuations)
