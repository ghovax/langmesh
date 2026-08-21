"""Core value models shared with product serialization adapters."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolStatus(str, Enum):
    """The lifecycle state of one tool call."""

    RUNNING = "running"
    OK = "ok"
    ERROR = "error"


def tool_status_from_result(result: Any) -> ToolStatus:
    """Read an explicit lifecycle status, defaulting a synchronous result to success."""
    record = result if isinstance(result, dict) else {}
    explicit = record.get("status")
    if explicit in (ToolStatus.RUNNING.value, ToolStatus.OK.value, ToolStatus.ERROR.value):
        return ToolStatus(explicit)
    return ToolStatus.OK


class PermissionReason(BaseModel):
    """Why approval is needed, as localizable data rather than a sentence."""

    kind: str
    paths: list[str] = Field(default_factory=list)


class PermissionAnswer(BaseModel, strict=True):
    """One permission decision and the explanation delivered on denial."""

    allow: bool
    reason: str = ""
    #: Who decided: the person, the built-in permission reviewer, or a caller's approval service.
    actor: Literal["person", "reviewer", "approver"]


class TurnContext(BaseModel):
    """Session context captured in the cache-stable system prompt.

    Only the core's own fields are declared here. The plugins contribute their own
    context through ``compose_context``, which merges it into the prompt dict; the
    core never names a plugin's context.
    """

    now: str = ""
    pwd: str = ""
    locations: list[dict[str, Any]] = Field(default_factory=list)
    confinement: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "PermissionAnswer",
    "PermissionReason",
    "ToolStatus",
    "TurnContext",
    "tool_status_from_result",
]
