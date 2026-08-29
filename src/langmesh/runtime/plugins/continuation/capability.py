"""The structural contract supplied by the continuation plugin."""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TasksCapability(Protocol):
    @property
    def task_manager(self) -> Any: ...


__all__ = ["TasksCapability"]
