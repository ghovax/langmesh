"""The continuation plugin: the tracked task list and autonomous continuation.

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
    """The plugin's task list."""

    tasks: TaskState

    @classmethod
    def from_data(cls, value: object) -> "ContinuationState | None":
        """Validate a continuation state from its storage representation."""
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return None
        return cls(tasks=TaskState.from_data(value.get("tasks")))


class Continuation(Feature):
    """Whether the session keeps going on its own, by the configured policy."""

    def __init__(self, *, policy: Any = None) -> None:
        self._policy = policy if policy is not None else DefaultContinuationPolicy()
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

    def should_continue_goal(self, goal) -> bool:
        return bool(self._policy.continue_goal(goal))

    def should_continue_tasks(self, actionable: Sequence) -> bool:
        return bool(self._policy.continue_tasks(actionable))

    def task_continuation_message(self, unfinished_tasks: list) -> str:
        """The hidden instruction that makes unfinished tracked work an actual next turn."""
        return self._prompts.load("task_continuation_note", {"tasks": compact(unfinished_tasks)})

    def continuation_messages(self, *, segments: Sequence[str] = ()) -> list[str]:
        """The continuation turn's input: every plugin's own segment kept as its own separate
        message, in order, so obligations share one turn without ever being merged into one text."""
        return [segment.strip() for segment in segments if segment.strip()]

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
        return ContinuationState(self._task_manager.snapshot())

    def restore(self, state: object) -> None:
        restored = ContinuationState.from_data(state)
        if restored is None:
            return
        self._task_manager.restore(restored.tasks)
