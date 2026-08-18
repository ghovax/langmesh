"""The core's call-execution policy value, and the permissions contract's verdict model.

Execution locations themselves are the locations plugin's concern; the core keeps only the
per-call policy (working directory + permission mode) and the typed verdict the permissions
plugin's reviewer submits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from langmesh.base.configuration.permission_mode import PermissionMode


@dataclass(frozen=True)
class CallExecutionPolicy:
    """One call's execution policy, threaded as a value so concurrent calls cannot cross."""

    working_directory: str
    mode: PermissionMode

    @property
    def asks(self) -> bool:
        """Whether a gate raised by this call goes to a person rather than the reviewer."""
        return self.mode.asks

    @property
    def gates(self) -> bool:
        """Whether this call is gated at all. ``allow`` mode skips every gate."""
        return self.mode.gates


class PermissionDecision(BaseModel):
    """The reviewer's verdict. Its ``risk`` is its own reading, which the agent cannot see and did not supply."""

    action: Literal["allow", "deny"]
    explanation: str
    risk: Literal["low", "medium", "high"]
