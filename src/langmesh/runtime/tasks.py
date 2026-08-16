"""The tracked task list, a leaf module so the runtime and the continuation plugin can both use it."""

from __future__ import annotations

from pydantic import BaseModel

from langmesh.base.primitives.serialization import compact


class TaskItem(BaseModel):
    identifier: str = ""
    # One short phrase naming the task, like a tool call's explanation; the description says what to do.
    title: str = ""
    description: str
    status: str = "pending"
    dependencies: list[str] = []


class TaskManager:
    def __init__(self):
        self._tasks: list[TaskItem] = []
        self._by_identifier: dict[str, TaskItem] = {}
        self._next_identifier: int = 1

    def add_tasks(self, task_definitions: list[dict]) -> list[str]:
        created = []
        for definition in task_definitions:
            # The identifier is the task's index: the model addresses a task by number, never by a prefixed id.
            identifier = str(self._next_identifier)
            self._next_identifier += 1
            task = TaskItem(
                identifier=identifier,
                title=definition.get("title", ""),
                description=definition.get("description", ""),
                dependencies=definition.get("dependencies", []),
            )
            self._tasks.append(task)
            self._by_identifier[identifier] = task
            created.append(identifier)
        return created

    # What an update may say. A key outside this set is reported rather than silently matching nothing.
    UPDATE_KEYS = frozenset({"task_id", "status"})
    STATUSES = ("pending", "in_progress", "completed", "blocked")

    def update_tasks(self, updates: list[dict]) -> tuple[list[str], list[str]]:
        """Apply each update, returning the ids that changed and a complaint for each that did not."""
        updated_ids: list[str] = []
        complaints: list[str] = []
        known = self._by_identifier
        for update in updates:
            unknown = sorted(set(update) - self.UPDATE_KEYS)
            if unknown:
                complaints.append(
                    f"{', '.join(unknown)} is not part of an update; use {', '.join(sorted(self.UPDATE_KEYS))}."
                )
            task_id = update.get("task_id", "")
            status = update.get("status", "")
            if status not in self.STATUSES:
                complaints.append(
                    f"{status!r} is not a status; use one of {', '.join(self.STATUSES)}."
                )
                continue
            if task_id not in known:
                complaints.append(
                    f"There is no task {task_id!r}. Current ids: {', '.join(sorted(known)) or 'none'}."
                )
                continue
            known[task_id].status = status
            updated_ids.append(task_id)
        return updated_ids, complaints

    def render_json(self) -> str:
        if not self._tasks:
            return ""
        return compact([task.model_dump() for task in self._tasks])

    def to_dict_list(self) -> list[dict]:
        return [task.model_dump() for task in self._tasks]

    def unfinished(self) -> list[dict]:
        """Tracked work that still needs action; blocked items remain visible but do not spin a turn."""
        return [task.model_dump() for task in self._tasks if task.status != "completed"]

    def actionable(self) -> list[dict]:
        """Unfinished work that is neither explicitly blocked nor waiting on unfinished work."""
        completed = {task.identifier for task in self._tasks if task.status == "completed"}
        return [
            task.model_dump()
            for task in self._tasks
            if task.status not in {"completed", "blocked"}
            and all(dependency in completed for dependency in task.dependencies)
        ]

    def snapshot(self) -> dict:
        """The manager's durable state, so a rebuilt runtime restores the tasks and keeps minting fresh ids."""
        return {"tasks": self.to_dict_list(), "next_identifier": self._next_identifier}

    def restore(self, snapshot: dict) -> None:
        """Rehydrate from :meth:`snapshot`, tolerating a missing or partial one by staying empty."""
        self._tasks = [TaskItem.model_validate(task) for task in snapshot.get("tasks", [])]
        self._by_identifier = {task.identifier: task for task in self._tasks}
        if len(self._by_identifier) != len(self._tasks):
            raise ValueError("task snapshot contains duplicate identifiers")
        numeric_identifiers = [
            int(task.identifier.removeprefix("task-"))
            for task in self._tasks
            if task.identifier.removeprefix("task-").isdigit()
        ]
        stored_next = int(snapshot.get("next_identifier", 1))
        self._next_identifier = max(stored_next, max(numeric_identifiers, default=0) + 1)


__all__ = ["TaskItem", "TaskManager"]