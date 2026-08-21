"""The seams a caller may replace, stated as interfaces rather than as our own classes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

if TYPE_CHECKING:  # pragma: no cover - import only for typing; `base` stays free of langchain
    from langchain_core.language_models.chat_models import BaseChatModel
    from langmesh.base.content.attachments import Attachment, ComposedAttachments
    from langmesh.runtime.session_control import SessionCheckpoint

    # The model seam as a type: every provider and every mock in that ecosystem already implements it.
    ChatModel = BaseChatModel


# Who decides whether a gated tool call proceeds.


@runtime_checkable
class DurableModelCache(Protocol):
    """Persists provider-native cache continuity beside the session checkpoint."""

    def model_cache_snapshot(self) -> object:
        """Return typed cache state owned by this model and session."""
        ...

    def restore_model_cache(self, snapshot: object) -> None:
        """Restore a prior snapshot or ignore one that does not belong to this model route."""
        ...


@dataclass(frozen=True)
class SuspensionGate:
    """One decision a turn is blocked on. Its fields are the runtime gate's, since one is built from the other."""

    request_id: str = ""
    tool_call_id: str = ""
    kind: str = "permission"
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    command: str = ""
    explanation: str = ""
    # Why approval is needed, as facts, so a client writes the sentence in its own language.
    reason: Any = None
    questions: list[dict[str, Any]] = field(default_factory=list)
    is_bash: bool = False
    deny_message: str = ""
    egress_agent: str = ""
    # The widening asked for, carried so approving records it rather than re-parsing the arguments.
    escape: Any = None
    # Whether approving lets this one command reach past the workspace, as a refused command is offered.
    whole_disk: bool = False
    denial_evidence: str = ""
    refused_result: Any = None
    grants_screen_mutations: bool = False
    # Whether the reviewer decides this one without a person; announced before the review.
    automatic_review: bool = False


@dataclass(frozen=True)
class Approval:
    """What an approver decided about one gate. A denial's ``reason`` is what the model is told."""

    allow: bool
    reason: str = ""
    answers: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class Approvals(Protocol):
    """Decides a gated call without a human. Absent, a gated turn suspends and waits to be resumed."""

    async def decide(self, gate: SuspensionGate) -> Optional[Approval]:
        """Decide one gate, or answer ``None`` to leave it to a human."""
        ...


# Where the audit trail goes.


@dataclass(frozen=True)
class Observation:
    """One transient harness audit signal, unrelated to workspace observational memory."""

    session_id: str
    kind: str
    at: datetime
    data: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class Observer(Protocol):
    """Receives transient audit signals. May return an awaitable for asynchronous handling."""

    def observe(self, observation: Observation) -> Awaitable[None] | None: ...


@dataclass(frozen=True)
class GoalReviewContext:
    """The stable identity and assignment of one independent goal review."""

    review_id: str
    session_id: str
    goal: str
    assignment: str
    created_at: datetime


@dataclass(frozen=True)
class GoalReviewOutcome:
    """The terminal state a goal-review journal records."""

    review_id: str
    session_id: str
    status: str
    standing: str | None
    completed_at: datetime


@runtime_checkable
class GoalReviewJournal(Protocol):
    """Records a linked review without making the core depend on a product transcript format."""

    async def open(self, context: GoalReviewContext) -> None: ...

    async def append(self, review_id: str, event: Any) -> None: ...

    async def close(self, outcome: GoalReviewOutcome) -> None: ...


# Where a session's resumable state lives.


@runtime_checkable
class Checkpoints(Protocol):
    """Where a session's resumable state lives, which is what makes it survive the process that ran it."""

    async def save(self, session_id: str, checkpoint: SessionCheckpoint) -> None: ...

    async def load(self, session_id: str) -> Optional[SessionCheckpoint]:
        """The last checkpoint, or ``None`` for a session that has never been saved."""
        ...


@runtime_checkable
class Attachments(Protocol):
    """Composes application-owned attachment values into one model-facing turn input."""

    def compose(
        self,
        message: str,
        attachments: Sequence[Attachment],
        model_identifier: str,
        inline_image_bytes: int,
    ) -> ComposedAttachments: ...


class MemoryCheckpoints:
    """Checkpoints in a dictionary: the default, so a library session can resume without a store."""

    def __init__(self) -> None:
        self._states: dict[str, SessionCheckpoint] = {}

    async def save(self, session_id: str, checkpoint: SessionCheckpoint) -> None:
        self._states[session_id] = checkpoint

    async def load(self, session_id: str) -> Optional[SessionCheckpoint]:
        return self._states.get(session_id)


# Where background jobs are recorded so one survives a restart.


@runtime_checkable
class JobStore(Protocol):
    """Durable record of background jobs, so a long-running task survives a restart."""

    # Keyword-only and named for what the caller passes, since a structural protocol cannot catch a drift.
    def record_started(
        self,
        *,
        job_id: str,
        session_id: str,
        agent_name: str,
        kind: str,
        arguments: Mapping[str, Any],
        tool_call_id: str = "",
    ) -> None: ...

    def record_process_group(self, job_id: str, process_group: int) -> None: ...

    def record_finished(self, job_id: str, result: str, *, status: str = ...) -> None: ...

    def mark_delivered(self, job_id: str) -> None: ...

    def mark_abandoned(self, job_id: str, result: str) -> None: ...

    def running_jobs(self, agent_name: str | None = None) -> Sequence[Mapping[str, Any]]: ...

    def orphaned_process_groups(self) -> Sequence[int]: ...

    def undelivered_jobs(self, session_id: str, agent_name: str) -> Sequence[Mapping[str, Any]]: ...

    def has_undelivered_jobs(self, session_id: str, agent_name: str) -> bool: ...

    def sessions_requiring_resume(self) -> Sequence[str]: ...


class MemoryJobStore:
    """Background jobs in a dictionary: the default, durable across nothing, which is honest for a library."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}

    def record_started(
        self,
        *,
        job_id: str,
        session_id: str,
        agent_name: str,
        kind: str,
        arguments: Mapping[str, Any],
        tool_call_id: str = "",
    ) -> None:
        # The same keys the SQLite store writes, so a reader cannot tell the two apart.
        self._jobs[job_id] = {
            "job_id": job_id,
            "session_id": session_id,
            "agent_name": agent_name,
            "kind": kind,
            "arguments": dict(arguments),
            "tool_call_id": tool_call_id,
            "status": "running",
            "result": "",
            "process_group": 0,
        }

    def record_process_group(self, job_id: str, process_group: int) -> None:
        if job_id in self._jobs:
            self._jobs[job_id]["process_group"] = process_group

    def record_finished(self, job_id: str, result: str, *, status: str = "completed") -> None:
        if job_id in self._jobs:
            self._jobs[job_id].update(result=result, status=status)

    def mark_delivered(self, job_id: str) -> None:
        if job_id in self._jobs:
            self._jobs[job_id]["status"] = "delivered"

    def mark_abandoned(self, job_id: str, result: str) -> None:
        if job_id in self._jobs:
            self._jobs[job_id].update(result=result, status="abandoned")

    def running_jobs(self, agent_name: str | None = None) -> Sequence[Mapping[str, Any]]:
        return [
            job
            for job in self._jobs.values()
            if job["status"] == "running"
            and (agent_name is None or job["agent_name"] == agent_name)
        ]

    def orphaned_process_groups(self) -> Sequence[int]:
        # Nothing is orphaned here: this store dies with the process that owns the jobs.
        return []

    def undelivered_jobs(self, session_id: str, agent_name: str) -> Sequence[Mapping[str, Any]]:
        return [
            job
            for job in self._jobs.values()
            if job["status"] in {"completed", "abandoned"}
            and job["session_id"] == session_id
            and job["agent_name"] == agent_name
        ]

    def has_undelivered_jobs(self, session_id: str, agent_name: str) -> bool:
        return bool(self.undelivered_jobs(session_id, agent_name))

    def sessions_requiring_resume(self) -> Sequence[str]:
        return sorted(
            {
                job["session_id"]
                for job in self._jobs.values()
                if job["status"] in {"running", "completed", "abandoned"}
            }
        )


# The record of what a session actually did.


@dataclass(frozen=True)
class TurnSummary:
    """One completed turn: what was asked, what came back, and how it ended."""

    session_id: str
    turn_id: str
    started_at: datetime
    ended_at: datetime
    request: str
    response: str
    # "completed" | "cancelled" | "failed" | "input_required" — the runtime's own stop reason.
    outcome: str
    tools_called: Sequence[str] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    error: str = ""


@runtime_checkable
class Transcript(Protocol):
    """Where the record of a session's turns goes. Not a2a's `TaskStore`: an embedder owes nobody a Task."""

    async def record(self, turn: TurnSummary) -> None: ...

    async def turns(self, session_id: str) -> Sequence[TurnSummary]:
        """Every turn recorded for a session, oldest first."""
        ...


class MemoryTranscript:
    """Turns in a list. The default, and the whole of it."""

    def __init__(self) -> None:
        self._turns: dict[str, list[TurnSummary]] = {}

    async def record(self, turn: TurnSummary) -> None:
        self._turns.setdefault(turn.session_id, []).append(turn)

    async def turns(self, session_id: str) -> Sequence[TurnSummary]:
        return list(self._turns.get(session_id, ()))


# The account credentials a provider needs, when it uses an account rather than a key.


@runtime_checkable
class Credentials(Protocol):
    """Where an account provider's OAuth tokens are kept, rather than a fixed path under the user's home."""

    def load(self) -> Any:
        """The stored tokens, or ``None`` when nothing is signed in."""
        ...

    def save(self, tokens: Any) -> None: ...

    def clear(self) -> None: ...


# Where the prompt's material comes from.


@dataclass
class CompactionState:
    """What a compaction strategy decides with, passed rather than reached for off the runtime."""

    messages: list
    """The conversation as it stands, oldest first."""

    context_window: int
    """The live model's context window in tokens, or 0 when it is not known."""

    context_tokens: int
    """What the conversation currently occupies, as the last reply reported it."""

    reason: str = "auto"
    """``auto`` when the loop asked, ``manual`` when a person did."""


@runtime_checkable
class Compaction(Protocol):
    """Decides when a conversation is compacted, and how."""

    def should_compact(self, state: CompactionState) -> bool:
        """Whether to compact now. Called before each model call; must be cheap."""
        ...

    async def compact(self, state: CompactionState) -> list:
        """Return the conversation to carry forward, oldest first."""
        ...


@dataclass
class CompactionSummaryState:
    """The compacted-away turns a summarizer distils, and what constrains that distillation."""

    messages: Sequence[Any]
    """The older turns being replaced, oldest first."""

    system_prompt: str
    """The cache-stable system prompt the session runs with, so the summary matches its voice."""


@runtime_checkable
class CompactionSummarizer(Protocol):
    """Produces the durable summary that replaces older turns after a compaction."""

    async def summarize(self, state: CompactionSummaryState) -> str | None:
        """Return the summary prose, or ``None`` to compact without one."""
        ...


@runtime_checkable
class CompactionPreparation(Protocol):
    """Defines and verifies the durable handoff required before old conversation is discarded."""

    def instruction(self, default: str) -> str | None:
        """The private instruction to run, or ``None`` when no handoff is required."""
        ...

    async def baseline(self) -> Any: ...

    async def completed(self, baseline: Any) -> bool: ...

    async def describe(self) -> Mapping[str, Any]: ...


@runtime_checkable
class ContinuationPolicy(Protocol):
    """Decides whether unfinished goals and tracked tasks may open another autonomous turn."""

    def continue_goal(self, goal: Any, completed_turns: int) -> bool: ...

    def continue_tasks(
        self,
        unfinished_tasks: Sequence[Mapping[str, Any]],
        completed_turns: int,
    ) -> bool: ...


@runtime_checkable
class BeforeModelHook(Protocol):
    """May transform the request assembled for one model call."""

    async def before_model(self, messages: list) -> list: ...


@runtime_checkable
class BeforeToolsHook(Protocol):
    """May narrow the already-approved tool batch before execution."""

    async def before_tools(self, calls: list[dict]) -> list[dict]: ...


@runtime_checkable
class AfterTurnHook(Protocol):
    """Observes the immutable summary after one turn ends."""

    async def after_turn(self, summary: TurnSummary) -> None: ...


TurnHook = BeforeModelHook | BeforeToolsHook | AfterTurnHook


@dataclass
class ToolInvocation:
    """One mutable call as middleware sees it, before its handler runs."""

    name: str
    arguments: dict[str, Any]


@runtime_checkable
class ToolMiddleware(Protocol):
    """Wraps one tool call, ours and the caller's alike, so a cross-cutting concern is one testable layer."""

    async def run(
        self,
        call: ToolInvocation,
        proceed: Callable[[ToolInvocation], Awaitable[Any]],
    ) -> Any:
        """Run `call`, or don't, or run it and do something around it."""
        ...


@runtime_checkable
class PermissionPolicy(Protocol):
    """Enables tools and rejects disallowed argument shapes."""

    def check_bash_background(self) -> None: ...


@runtime_checkable
class FileLeases(Protocol):
    """Coordinates concurrent filesystem mutation across composed sessions."""

    async def acquire(
        self,
        *,
        owner_session_id: str,
        scope: str,
        path: str,
        working_directory: str,
        description: str,
        timeout: float = ...,
    ) -> str: ...

    def release(self, token: str) -> None: ...


@runtime_checkable
class SessionAccess(Protocol):
    """Creates and addresses peer sessions without coupling the core to a host transport."""

    session_id: str
    working_directory: str
    permission_mode: str

    async def create(
        self,
        *,
        agent: str,
        working_directory: str,
        inherited_conversation: list[dict[str, Any]],
    ) -> dict: ...

    async def send(self, session_id: str, text: str) -> None: ...

    async def get(self, session_id: str) -> dict: ...

    async def children(self) -> list[dict]: ...

    async def end(self, session_id: str) -> dict: ...

    async def remote_list(self) -> list[dict]: ...

    async def remote_send(self, name: str, text: str) -> dict: ...


@runtime_checkable
class MCPServers(Protocol):
    """The initialized MCP server connections a runtime may query and call."""

    async def list_tools(self, server: str = "") -> Any: ...

    async def call_tool(self, server: str, tool: str, arguments: Mapping[str, Any]) -> Any: ...

    async def list_resources(self, server: str = "") -> Any: ...

    async def read_resource(self, server: str, uri: str) -> Any: ...


@runtime_checkable
class CatalogueLike(Protocol):
    """The source of everything the prompt is assembled from: profiles, skills, memories, instructions, templates."""

    def agent(self, name: str) -> Any:
        """The named agent profile, or ``None`` if this catalogue has no such agent."""
        ...

    def agents(self) -> Sequence[str]:
        """Every agent name this catalogue can supply, for listing and for error messages."""
        ...

    def skills(self) -> Sequence[Any]: ...

    def memories(self) -> Sequence[Any]: ...

    def instructions(self) -> Sequence[Any]:
        """The project's own conventions, as `Instruction` values."""
        ...

    def prompt(self, name: str, variables: Mapping[str, str]) -> str:
        """One rendered prompt template, or ``""`` when this catalogue has no such template."""
        ...


@dataclass(frozen=True)
class PromptLayer:
    """One named piece of the cache-stable system prompt."""

    name: str
    content: str


@runtime_checkable
class PromptComposer(Protocol):
    """Places and formats the named static prompt layers assembled by the runtime."""

    def compose(self, layers: Sequence[PromptLayer]) -> str: ...


def describe_unmet(port: type, candidate: Any) -> str:
    """Which of a port's methods ``candidate`` is missing, as a sentence, so a refusal can be acted on."""
    missing = sorted(
        name for name in getattr(port, "__protocol_attrs__", ()) if not hasattr(candidate, name)
    )
    if not missing:
        return ""
    described = ", ".join(f"`{name}`" for name in missing)
    return (
        f"{type(candidate).__name__} does not satisfy {port.__name__}: it is missing {described}."
    )


__all__ = [
    "Approval",
    "Approvals",
    "Attachments",
    "AfterTurnHook",
    "BeforeModelHook",
    "BeforeToolsHook",
    "CatalogueLike",
    "Checkpoints",
    "Credentials",
    "DurableModelCache",
    "GoalReviewContext",
    "GoalReviewJournal",
    "GoalReviewOutcome",
    "FileLeases",
    "JobStore",
    "MemoryCheckpoints",
    "MemoryJobStore",
    "MemoryTranscript",
    "MCPServers",
    "Observation",
    "Observer",
    "PermissionPolicy",
    "PromptComposer",
    "PromptLayer",
    "SessionAccess",
    "Compaction",
    "CompactionPreparation",
    "CompactionState",
    "CompactionSummarizer",
    "CompactionSummaryState",
    "ContinuationPolicy",
    "SuspensionGate",
    "ToolMiddleware",
    "ToolInvocation",
    "TurnHook",
    "Transcript",
    "TurnSummary",
    "describe_unmet",
]
