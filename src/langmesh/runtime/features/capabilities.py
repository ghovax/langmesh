"""Structural capability contracts implemented by independent runtime features."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class BackgroundCapability(Protocol):
    @property
    def runner(self) -> Any: ...

    def bind_tool_call(self, job_id: str, tool_call_id: str) -> None: ...


@runtime_checkable
class CompactionCapability(Protocol):
    @property
    def failure(self) -> str | None: ...

    def submit_summary(self, summary: Any) -> None: ...


@runtime_checkable
class GoalCapability(Protocol):
    @property
    def goal(self) -> Any: ...

    def write(self, goal: Any) -> None: ...

    def submit(self, review: Any) -> None: ...


@runtime_checkable
class LocationsCapability(Protocol):
    def resolve_execution(self, tool_name: str, arguments: dict) -> Any: ...

    def set_locations(self, locations: list[dict[str, Any]] | None) -> None: ...


@runtime_checkable
class PermissionsCapability(Protocol):
    def retry_gate(self, **values: Any) -> Any: ...

    async def decide_retry(self, gate: Any) -> tuple[str, Any]: ...

    def retry_refusal_result(self, gate: Any) -> Any: ...

    async def reconsider_gate(self, gate: Any) -> dict[str, Any]: ...


@runtime_checkable
class TasksCapability(Protocol):
    @property
    def task_manager(self) -> Any: ...


__all__ = [
    "BackgroundCapability",
    "CompactionCapability",
    "GoalCapability",
    "LocationsCapability",
    "PermissionsCapability",
    "TasksCapability",
]
