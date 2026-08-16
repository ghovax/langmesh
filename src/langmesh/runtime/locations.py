"""Value types for resolving a call's location and carrying its execution policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from pydantic import BaseModel

from langmesh.base.configuration.permission_mode import PermissionMode
from langmesh.locations.executor import LocationExecutor


@dataclass(frozen=True)
class Location:
    """One addressable execution environment, optionally with a caller-supplied executor."""

    name: str
    kind: Literal["local", "remote"]
    base_directory: str
    host_alias: str = ""
    uri: str = ""
    executor: LocationExecutor | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Location":
        return cls(
            name=str(value.get("name") or "location"),
            kind=str(value.get("kind") or "local"),
            base_directory=str(value.get("base_directory") or ""),
            host_alias=str(value.get("host_alias") or ""),
            uri=str(value.get("uri") or ""),
            executor=value.get("executor"),
        )

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("location name must not be empty")
        if self.kind not in {"local", "remote"}:
            raise ValueError("location kind must be 'local' or 'remote'")
        if not self.base_directory:
            raise ValueError("location base_directory must not be empty")
        if self.kind == "remote" and not self.host_alias and not (self.executor and self.uri):
            raise ValueError("a remote location needs host_alias, or both uri and a custom executor")


@dataclass
class ResolvedLocation:
    """A location resolved for execution: its identity, its executor, its base directory, its mode."""

    uri: str
    name: str
    kind: str  # "local" | "remote"
    base_directory: str
    executor: LocationExecutor
    @property
    def is_remote(self) -> bool:
        return self.kind == "remote"


class ToolLocationError(ValueError):
    """A tool call named a `location` that is missing, ambiguous, or unknown."""


@dataclass(frozen=True)
class CallExecutionPolicy:
    """One call's execution policy, threaded as a value so concurrent calls cannot cross locations."""

    location: ResolvedLocation | None
    working_directory: str
    mode: PermissionMode

    @property
    def asks(self) -> bool:
        """Whether a gate raised by this call goes to a person rather than the reviewer."""
        return self.mode.asks

    @property
    def is_remote(self) -> bool:
        return self.location is not None and self.location.is_remote


# The tools that act on a location's filesystem or shell, and so resolve against one.
_LOCATION_TOOLS = frozenset({"bash", "download_file"})


class PermissionDecision(BaseModel):
    """The reviewer's verdict. Its ``risk`` is its own reading, which the agent cannot see and did not supply."""

    action: Literal["allow", "deny"]
    explanation: str
    risk: Literal["low", "medium", "high"]
