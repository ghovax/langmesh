"""The structural contract supplied by the locations plugin."""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LocationsCapability(Protocol):
    def resolve_execution(self, tool_name: str, arguments: dict) -> Any: ...

    def set_locations(self, locations: list[dict[str, Any]] | None) -> None: ...


__all__ = ["LocationsCapability"]
