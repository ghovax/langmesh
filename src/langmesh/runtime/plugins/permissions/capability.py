"""The structural contract supplied by the permissions plugin."""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PermissionsCapability(Protocol):
    def retry_gate(self, **values: Any) -> Any: ...

    async def decide_retry(self, gate: Any) -> tuple[str, Any]: ...

    def retry_refusal_result(self, gate: Any) -> Any: ...

    async def reconsider_gate(self, gate: Any) -> dict[str, Any]: ...


__all__ = ["PermissionsCapability"]
