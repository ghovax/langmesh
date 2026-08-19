"""The configurable core runtime and its public composition values."""

from typing import TYPE_CHECKING

from langmesh.runtime.composition import RuntimeComponents, RuntimeProfile, SessionComponents
from langmesh.runtime.session_control import PendingTurn, SessionPhase, SessionState

if TYPE_CHECKING:
    from langmesh.runtime.runtime import AgentRuntime

__all__ = [
    "AgentRuntime",
    "PendingTurn",
    "RuntimeComponents",
    "RuntimeProfile",
    "SessionComponents",
    "SessionPhase",
    "SessionState",
]


def __getattr__(name: str):
    if name == "AgentRuntime":
        from langmesh.runtime.runtime import AgentRuntime

        return AgentRuntime
    raise AttributeError(name)
