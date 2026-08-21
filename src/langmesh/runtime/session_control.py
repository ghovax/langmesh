"""Public structural state for driving an embedded session safely."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum, StrEnum
from typing import Any, Literal, Mapping, cast

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
        return tuple(gate for gate in self.interactions if gate.request_id not in self.decisions)

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

    def to_data(self) -> dict[str, Any]:
        """Encode this value for a storage or transport adapter."""
        return {
            "interactions": [_plain(gate) for gate in self.interactions],
            "plans": _plain(self.plans),
            "decisions": {
                request_id: asdict(decision) for request_id, decision in self.decisions.items()
            },
        }

    @classmethod
    def from_data(cls, state: Mapping[str, Any]) -> "PendingTurn":
        """Decode the storage representation produced by :meth:`to_data`."""
        return cls(
            interactions=tuple(SuspensionGate(**gate) for gate in state.get("interactions", ())),
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


@dataclass(frozen=True)
class FeatureState:
    """One plugin's namespaced durable value."""

    name: str
    value: object


@dataclass(frozen=True)
class RenderedPrompt:
    """The exact stable instructions sent upstream and their construction revision."""

    instructions: str
    revision: str


@dataclass(frozen=True)
class PendingInput:
    """One accepted model-facing input not yet appended to the conversation."""

    message: Mapping[str, Any]
    recorded_text: str


@dataclass(frozen=True)
class SessionSnapshot:
    """The runtime state that is durable independently of its conversation."""

    features: tuple[FeatureState, ...] = ()
    turn_recovery: Literal["none", "retryable"] = "none"
    turn_failure_root: str | None = None
    model_cache: object | None = None
    system_prompt: RenderedPrompt | None = None
    pending_input: PendingInput | None = None

    def feature(self, name: str) -> object | None:
        """Return one plugin's state by its stable name."""
        return next((item.value for item in self.features if item.name == name), None)

    def to_data(self) -> dict[str, Any]:
        """Encode this value for a storage or transport adapter."""
        return {
            "features": [
                {"name": feature.name, "value": _plain(feature.value)} for feature in self.features
            ],
            "turn_recovery": self.turn_recovery,
            "turn_failure_root": self.turn_failure_root,
            "model_cache": _plain(self.model_cache),
            "system_prompt": _plain(self.system_prompt),
            "pending_input": _plain(self.pending_input),
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> "SessionSnapshot":
        """Decode the storage representation produced by :meth:`to_data`."""
        recovery = str(data.get("turn_recovery") or "none")
        if recovery == "retrying":
            recovery = "retryable"
        if recovery not in {"none", "retryable"}:
            recovery = "none"
        raw_features = data.get("features", ())
        raw_prompt = data.get("system_prompt")
        raw_pending_input = data.get("pending_input")
        features = tuple(
            FeatureState(name=str(item["name"]), value=item.get("value"))
            for item in raw_features
            if isinstance(item, Mapping) and str(item.get("name") or "")
        )
        return cls(
            features=features,
            turn_recovery=cast(Literal["none", "retryable"], recovery),
            turn_failure_root=str(data["turn_failure_root"])
            if data.get("turn_failure_root")
            else None,
            model_cache=data.get("model_cache"),
            system_prompt=RenderedPrompt(
                instructions=str(raw_prompt.get("instructions") or ""),
                revision=str(raw_prompt.get("revision") or ""),
            )
            if isinstance(raw_prompt, Mapping)
            and str(raw_prompt.get("content") or "")
            and str(raw_prompt.get("revision") or "")
            else None,
            pending_input=PendingInput(
                message=dict(raw_pending_input.get("message") or {}),
                recorded_text=str(raw_pending_input.get("recorded_text") or ""),
            )
            if isinstance(raw_pending_input, Mapping)
            and isinstance(raw_pending_input.get("message"), Mapping)
            else None,
        )


@dataclass(frozen=True)
class SessionCheckpoint:
    """Everything an embedded session gives its caller-owned checkpoint adapter."""

    conversation: tuple[dict[str, Any], ...]
    session: SessionSnapshot
    pending: PendingTurn | None = None

    def to_data(self) -> dict[str, Any]:
        """Encode this value for a storage or transport adapter."""
        return {
            "conversation": list(self.conversation),
            "session": self.session.to_data(),
            "pending": self.pending.to_data() if self.pending is not None else None,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> "SessionCheckpoint":
        """Decode the storage representation produced by :meth:`to_data`."""
        raw_session = data.get("session")
        raw_pending = data.get("pending")
        return cls(
            conversation=tuple(
                item for item in data.get("conversation", ()) if isinstance(item, dict)
            ),
            session=SessionSnapshot.from_data(raw_session)
            if isinstance(raw_session, Mapping)
            else SessionSnapshot(),
            pending=PendingTurn.from_data(raw_pending)
            if isinstance(raw_pending, Mapping)
            else None,
        )


__all__ = [
    "FeatureState",
    "PendingInput",
    "PendingTurn",
    "RenderedPrompt",
    "SessionCheckpoint",
    "SessionPhase",
    "SessionSnapshot",
    "SessionState",
]
