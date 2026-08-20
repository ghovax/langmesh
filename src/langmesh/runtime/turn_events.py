"""The typed runtime-to-executor event contract, as a closed union rather than untyped dictionaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Literal, Union

from langmesh.base.contracts.ports import SuspensionGate
from langmesh.runtime.values import ToolStatus, tool_status_from_result


class EventType(str, Enum):
    STATUS = "status"
    THINKING = "thinking"
    THINKING_DONE = "thinking_done"
    TEXT_CHUNK = "text_chunk"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    MCP_EVENT = "mcp_event"
    USAGE = "usage"
    DONE = "done"
    SUSPENDED = "suspended"
    PERMISSION_REVIEWING = "permission_reviewing"
    CHECKPOINT = "checkpoint"
    ERROR = "error"
    DENIED_INJECTION = "denied_injection"
    RETRY_REQUESTED = "retry_requested"
    GROUP_STARTED = "group_started"
    RELAYED = "relayed"
    STEERING = "steering"
    COMPACTION_STARTED = "compaction_started"
    COMPACTION_DONE = "compaction_done"
    GOAL_REVIEW_STARTED = "goal_review_started"
    GOAL_REVIEW_PROGRESS = "goal_review_progress"
    GOAL_REVIEW_FINISHED = "goal_review_finished"


@dataclass(frozen=True)
class TurnEvent:
    """Base of the closed event union, matched on by variant class rather than by an untyped tag."""

    TYPE: ClassVar[EventType]

    @property
    def type(self) -> EventType:
        return self.TYPE


@dataclass(frozen=True)
class Status(TurnEvent):
    TYPE = EventType.STATUS
    code: str = ""


@dataclass(frozen=True)
class Thinking(TurnEvent):
    TYPE = EventType.THINKING
    text: str = ""
    block_id: str = ""


@dataclass(frozen=True)
class ThinkingDone(TurnEvent):
    TYPE = EventType.THINKING_DONE
    duration_milliseconds: int = 0


@dataclass(frozen=True)
class TextChunk(TurnEvent):
    TYPE = EventType.TEXT_CHUNK
    text: str = ""
    block_id: str = ""


@dataclass(frozen=True)
class ToolCall(TurnEvent):
    TYPE = EventType.TOOL_CALL
    id: str = ""
    name: str = ""
    arguments: Any = None
    arguments_complete: bool = True


@dataclass(frozen=True)
class ToolResult(TurnEvent):
    TYPE = EventType.TOOL_RESULT
    id: str = ""
    name: str = ""
    result: Any = None
    status: str = ""
    # Set when this result is a background job's completion, not a synchronous return.
    job_id: str = ""
    # Model-facing guidance appended after the contiguous tool-result block, never serialized into result data.
    model_guidance: str = ""
    # A tool result's payload is genuinely open, so it rides a typed envelope with an open tail.
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # An explicit status wins, else it is derived from the result payload.
        normalized = ToolStatus(self.status or tool_status_from_result(self.result)).value
        object.__setattr__(self, "status", normalized)


@dataclass(frozen=True)
class Mcp(TurnEvent):
    TYPE = EventType.MCP_EVENT
    id: str = ""
    name: str = ""
    server: str = ""
    tool: str = ""
    event: Any = None


@dataclass(frozen=True)
class Usage(TurnEvent):
    TYPE = EventType.USAGE
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    reasoning_tokens: int = 0
    context_window: int = 0
    context_window_estimated: bool = False
    cumulative: dict[str, Any] = field(default_factory=dict)
    #: Whether every shared byte was unchanged; ``None`` = unknown (no previous request in the lane).
    prefix_intact: bool | None = None
    #: How much of the prefix was unchanged, estimated with this harness's tokenizer rather than the provider's.
    reachable_tokens: int = 0
    #: How many segments the request had, and how many the previous one already carried unchanged.
    segments: int = 0
    shared_segments: int = 0
    #: The segment that moved, when one did, as fields rather than a sentence.
    divergence: dict[str, Any] | None = None


@dataclass(frozen=True)
class Done(TurnEvent):
    TYPE = EventType.DONE
    text: str = ""
    stop_reason: str = ""


@dataclass(frozen=True)
class Suspended(TurnEvent):
    TYPE = EventType.SUSPENDED
    # The gates awaiting a human, with the plans a resume rebuilds the batch from.
    interactions: list[SuspensionGate] = field(default_factory=list)
    plans: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class PermissionReviewing(TurnEvent):
    TYPE = EventType.PERMISSION_REVIEWING
    # Automatic-mode gates the reviewer is weighing, announced before the verdict so the call is visible.
    interactions: list[SuspensionGate] = field(default_factory=list)


@dataclass(frozen=True)
class Checkpoint(TurnEvent):
    TYPE = EventType.CHECKPOINT


@dataclass(frozen=True)
class Error(TurnEvent):
    TYPE = EventType.ERROR
    message: str = "error"
    id: str = ""
    code: str = ""
    tool: str = ""
    # Model-facing guidance appended after the contiguous tool-result block, never serialized into error data.
    model_guidance: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeniedInjection(TurnEvent):
    TYPE = EventType.DENIED_INJECTION
    command: str = ""
    id: str = ""


@dataclass(frozen=True)
class RetryRequested(TurnEvent):
    """A command the system refused, which somebody could let out of the box; internal to the turn loop."""

    TYPE = EventType.RETRY_REQUESTED
    id: str = ""
    command: str = ""
    denial_kind: str = ""
    denial_evidence: str = ""
    explanation: str = ""
    result: Any = None


@dataclass(frozen=True)
class Steering(TurnEvent):
    TYPE = EventType.STEERING
    text: str = ""
    #: The id the sender gave it, so a client can match this against the message it already has.
    message_id: str = ""
    #: The session that sent it, empty when the person did.
    peer_sender: str = ""


@dataclass(frozen=True)
class CompactionStarted(TurnEvent):
    TYPE = EventType.COMPACTION_STARTED
    reason: str = ""
    messages_before: int = 0
    tokens_before: int = 0


@dataclass(frozen=True)
class CompactionDone(TurnEvent):
    TYPE = EventType.COMPACTION_DONE
    reason: str = ""
    ok: bool = True
    messages_before: int = 0
    messages_after: int = 0
    tokens_before: int = 0
    # What the compaction actually reclaimed, reported by every strategy including a supplied one.
    tokens_after: int = 0
    # Present only on a failed compaction; the client resolves it through its locale catalogue.
    error_code: str | None = None


@dataclass(frozen=True)
class GoalReviewStarted(TurnEvent):
    """An independent review has begun for the current goal."""

    TYPE = EventType.GOAL_REVIEW_STARTED
    review_id: str
    goal: str
    purpose: str | None = None
    minimum_conditions: tuple[str, ...] = ()


@dataclass(frozen=True)
class GoalReviewProgress(TurnEvent):
    """One ordinary turn event emitted by the independent reviewer."""

    TYPE = EventType.GOAL_REVIEW_PROGRESS
    review_id: str
    event: TurnEvent


@dataclass(frozen=True)
class GoalReviewFinished(TurnEvent):
    """The independent review ended, with its public verdict when one landed."""

    TYPE = EventType.GOAL_REVIEW_FINISHED
    review_id: str
    status: Literal["completed", "canceled", "failed"]
    standing: Literal["unmet", "satisfied", "blocked"] | None = None
    assessment: str | None = None
    unmet: tuple[str, ...] = ()
    evidence: str | None = None
    blocker: str | None = None
    contract_status: Literal["complete", "needs_revision"] | None = None
    message: str | None = None


# The closed union of every turn event, so a consumer can prove exhaustiveness rather than fall through.
TurnEventUnion = Union[
    Status,
    Thinking,
    ThinkingDone,
    TextChunk,
    ToolCall,
    ToolResult,
    Mcp,
    Usage,
    Done,
    Suspended,
    PermissionReviewing,
    Checkpoint,
    Error,
    DeniedInjection,
    Steering,
    CompactionStarted,
    CompactionDone,
    GoalReviewStarted,
    GoalReviewProgress,
    GoalReviewFinished,
]
