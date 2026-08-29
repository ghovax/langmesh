"""The structural contract supplied by the background plugin."""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class BackgroundCapability(Protocol):
    @property
    def runner(self) -> Any: ...

    def bind_tool_call(self, job_id: str, tool_call_id: str) -> None: ...


__all__ = ["BackgroundCapability"]
