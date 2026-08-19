"""Public structural state for driving an embedded session safely."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum, StrEnum
from typing import Any, Mapping

from langmesh.base.contracts.ports import Approval, SuspensionGate


def _plain(value: Any) -> Any:
    """Reduce a public control value to the JSON-shaped checkpoint contract."""
    if is_dataclass(value) and not isinstance(value, type):
        return _plain(asdict(value))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _plain(model_dump(mode="json"))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


class SessionPhase(StrEnum):
    """The one operation an embedded session is currently able to accept."""

    IDLE = "idle"
    RUNNING = "running"
    SUSPENDED = "suspended"
    COMPACTING = "compacting"
    RETRYING = "retrying"


@dataclass(frozen=True)
class PendingTurn:
    """A suspended tool batch and the decisions already supplied for it."""

    interactions: tuple[SuspensionGate, ...]
    plans: Mapping[str, dict[str, Any]]
    decisions: Mapping[str, Approval]

    @property
    def remaining(self) -> tuple[SuspensionGate, ...]:
        return tuple(
            gate for gate in self.interactions if gate.request_id not in self.decisions
        )

    @property
    def ready(self) -> bool:
        return bool(self.interactions) and not self.remaining

    def with_decision(self, request_id: str, decision: Approval) -> "PendingTurn":
        if request_id not in {gate.request_id for gate in self.interactions}:
            raise KeyError(f"No pending interaction has request id {request_id!r}.")
        return PendingTurn(
            interactions=self.interactions,
            plans=self.plans,
            decisions={**self.decisions, request_id: decision},
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "interactions": [_plain(gate) for gate in self.interactions],
            "plans": _plain(self.plans),
            "decisions": {
                request_id: asdict(decision)
                for request_id, decision in self.decisions.items()
            },
        }

    @classmethod
    def restore(cls, state: Mapping[str, Any]) -> "PendingTurn":
        return cls(
            interactions=tuple(
                SuspensionGate(**gate) for gate in state.get("interactions", ())
            ),
            plans=dict(state.get("plans", {})),
            decisions={
                str(request_id): Approval(**decision)
                for request_id, decision in state.get("decisions", {}).items()
            },
        )


@dataclass(frozen=True)
class SessionState:
    """A coherent snapshot of the controls available to an embedded caller.

    Only the core's own controls live here. Plugin-owned state (goals, tasks, compaction,
    background jobs) is reached through the feature seam, never through this snapshot.
    """

    phase: SessionPhase
    pending: PendingTurn | None
    permission_mode: str


__all__ = ["PendingTurn", "SessionPhase", "SessionState"]
