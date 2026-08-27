"""The helpers and small dataclasses the AgentRuntime mixins share, in a leaf module so the graph stays a DAG."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from models_provider import chatgpt_tokens, cursor_tokens
from langmesh.base.configuration import Configuration
from langmesh.base.confinement import ApprovedBy, Grant
from langmesh.runtime.values import ToolStatus, tool_status_from_result
from langmesh.base.identity.providers import resolve_api_key
from langmesh.base.content.message_content import message_text
from langmesh.base.content.models import find_model
from langmesh.runtime.boundary import Escape
from langmesh.base.primitives.limits import current_limits, clip_to_tokens, count_tokens
from langchain_core.messages import AIMessageChunk
from typing import Any, AsyncIterator, Callable, Optional
from langmesh.base.primitives.serialization import compact


def settled_arguments(parsed: dict, raw: str) -> dict:
    """Return only tool arguments whose values have finished streaming."""
    try:
        json.loads(raw)
    except ValueError:
        return dict(list(parsed.items())[:-1])
    return parsed


# What ``_stream_next`` returns at the end, since a StopAsyncIteration inside a Task is mishandled.
_STREAM_EXHAUSTED = object()


async def _stream_next(iterator: AsyncIterator) -> Any:
    """The next item, or ``_STREAM_EXHAUSTED``, so each read can be raced against the abort."""
    try:
        return await iterator.__anext__()
    except StopAsyncIteration:
        return _STREAM_EXHAUSTED


async def _race_interrupt(task: Any, interrupt_event: Any) -> bool:
    """Await `task` raced against an interrupt event; True when the interrupt fired first."""
    waiter = asyncio.ensure_future(interrupt_event.wait())
    try:
        finished, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        return task not in finished
    finally:
        waiter.cancel()
        if not waiter.done():
            with suppress(asyncio.CancelledError):
                await waiter


async def await_interruptible(
    task: Any,
    interrupt_event: Any,
    abort: Callable[[], None] | None = None,
) -> bool:
    """Run `task` until it finishes or `interrupt_event` fires.

    True when the interrupt won. The task is always retrieved, so a failure cannot look
    like a clean empty finish. `abort` runs only when the interrupt won, before that retrieve.
    """
    interrupted = await _race_interrupt(task, interrupt_event)
    if interrupted:
        if abort is not None:
            abort()
        with suppress(asyncio.CancelledError):
            await task
        return True
    await task
    return False


def model_is_authorized(
    model_identifier: str,
    global_configuration: Configuration,
) -> bool:
    """Whether we hold credentials for ``model_identifier``. The one authority, mirroring ``build_chat_model``."""
    provider_identifier = model_identifier.split("/", 1)[0]
    if provider_identifier == "chatgpt":
        return chatgpt_tokens() is not None
    if provider_identifier == "cursor":
        return cursor_tokens() is not None
    if provider_identifier == "custom":
        return True
    # models.dev providers are registered while the catalogue is resolved. Authorization must trigger the same ordered discovery as model construction on a cold worker.
    find_model(model_identifier)
    return bool(
        resolve_api_key(provider_identifier, global_configuration.configured_provider_keys())
    )


def _maybe_json(value: str) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


# Background handles from the tool registries. Not A2A tasks: their results are delivered when ready.
_BACKGROUND_HANDLE_PREFIXES = {
    "search-": "search_web",
    "bg-": "bash",
}


def _coerce_mcp_arguments(value: Any) -> dict:
    """An MCP server call's `arguments` as a dict, since models often emit the object as a JSON string."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _background_handle_kind(turn_id: str) -> str | None:
    """The background kind ``turn_id`` names, or ``None`` when it is a readable A2A task."""
    for prefix, kind in _BACKGROUND_HANDLE_PREFIXES.items():
        if turn_id.startswith(prefix):
            return kind
    return None


def _cap_model_result_payload(result: str, *, code: str = "tool_result_truncated") -> str:
    """Bound a model-facing result to the output budget, by dropping whole fields and saying which went."""
    budget = current_limits().output_tokens
    _, was_truncated = clip_to_tokens(result, budget)
    if not was_truncated:
        return result

    parsed = _maybe_json(result)
    if not isinstance(parsed, dict):
        excerpt, _ = clip_to_tokens(result, budget)
        return compact(
            {
                "code": code,
                "truncated": True,
                "omitted_characters": len(result) - len(excerpt),
                "output_excerpt": excerpt,
            }
        )

    kept = dict(parsed)
    omitted: dict[str, int] = {}

    def rendered_with(fields: dict) -> str:
        return compact({**fields, "truncated": True, **({"omitted": omitted} if omitted else {})})

    def over(fields: dict) -> bool:
        return clip_to_tokens(rendered_with(fields), budget)[1]

    # Largest first, but never the fields that say what happened: a failure is what a model can act on.
    essential = {"ok", "error", "error_code", "code", "status"}
    for key in sorted(kept, key=lambda key: len(compact(kept[key])), reverse=True):
        if not over(kept):
            break
        if key not in essential:
            omitted[key] = len(compact(kept.pop(key)))

    if not over(kept):
        return rendered_with(kept)

    # One field enormous on its own: clip its text in place, so the result keeps its shape.
    for key in sorted(kept, key=lambda key: len(compact(kept[key])), reverse=True):
        if not over(kept) or not isinstance(kept[key], str):
            continue
        elsewhere = count_tokens(
            rendered_with({other: value for other, value in kept.items() if other != key})
        )
        excerpt, clipped = clip_to_tokens(kept[key], max(1, budget - elsewhere))
        if clipped:
            omitted[f"{key} (clipped)"] = len(kept[key]) - len(excerpt)
            kept[key] = excerpt
    return rendered_with(kept)


def message_tokens(message: Any) -> int:
    """How much window one message occupies, counting the tool calls and results sent with it."""
    total = count_tokens(message_text(message))
    for tool_call in getattr(message, "tool_calls", None) or []:
        arguments = tool_call.get("args")
        total += count_tokens(arguments if isinstance(arguments, str) else compact(arguments))
        total += count_tokens(str(tool_call.get("name") or ""))
    return total


def conversation_tokens(messages: Any) -> int:
    """:func:`message_tokens` over a whole message list."""
    return sum(message_tokens(message) for message in messages)


def _utc_timestamp(datetime_value: datetime) -> str:
    return datetime_value.isoformat()


def _tool_timing_metadata(
    *,
    tool_name: str,
    tool_call_identifier: str,
    started_at: datetime,
    completed_at: datetime,
    duration_milliseconds: int,
    background_job_id: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "tool_name": tool_name,
        "tool_call_id": tool_call_identifier,
        "started_at": _utc_timestamp(started_at),
        "completed_at": _utc_timestamp(completed_at),
        "duration_milliseconds": duration_milliseconds,
    }
    if background_job_id:
        metadata["background_job_id"] = background_job_id
    return metadata


def _model_result_status(content: str, *, ok: bool, backgrounded: bool) -> tuple[str, str | None]:
    """The (status, code) for a model-facing result: failed is error, backgrounded is still running."""
    parsed = _maybe_json(content)
    code = parsed.get("code") if isinstance(parsed, dict) else None
    if not ok:
        return ToolStatus.ERROR.value, code
    if backgrounded:
        return ToolStatus.RUNNING.value, code
    return tool_status_from_result(parsed).value, code


def _container_origins(annotation: Any) -> set:
    """The container origins an annotation can be, so a string argument can be JSON-parsed or not."""
    import types as types_module
    import typing

    origins: set = set()

    def visit(current: Any) -> None:
        origin = typing.get_origin(current)
        if origin in (list, dict):
            origins.add(origin)
        elif origin is typing.Union or origin is getattr(types_module, "UnionType", None):
            for argument in typing.get_args(current):
                visit(argument)

    visit(annotation)
    return origins


def _coerce_structured_arguments(schema: Any, arguments: dict) -> dict:
    """Parse arguments a model serialized as JSON strings back into the containers the schema types."""
    if not isinstance(arguments, dict):
        return arguments
    model_fields = getattr(schema, "model_fields", {})
    coerced = dict(arguments)
    for name, value in arguments.items():
        if not isinstance(value, str):
            continue
        field = model_fields.get(name)
        if field is None:
            continue
        origins = _container_origins(field.annotation)
        if not origins:
            continue
        text = value.strip()
        if not text or text[0] not in "[{":
            continue
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            continue
        if (list in origins and isinstance(parsed, list)) or (
            dict in origins and isinstance(parsed, dict)
        ):
            coerced[name] = parsed
    return coerced


def _escape_to_dict(escape: Any) -> dict:
    """An :class:`~langmesh.runtime.boundary.Escape` as plain data, since this is about to be JSON."""
    return {
        "reads": list(escape.reads),
        "writes": list(escape.writes),
        "network": escape.network,
    }


def _escape_from_dict(data: Any) -> Any:
    """The inverse, passing through a value that is already an ``Escape``."""
    if isinstance(data, Escape):
        return data
    data = data or {}
    return Escape(
        reads=tuple(data.get("reads") or ()),
        writes=tuple(data.get("writes") or ()),
        network=bool(data.get("network", False)),
    )


@dataclass
class _PreflightGate:
    """One interaction a call needs before it can run: a permission prompt, or an ``ask_user`` question."""

    request_id: str
    tool_call_id: str
    kind: str  # "permission" | "question"
    # The call this gate stands in front of, since it is shown before that call is announced.
    tool_name: str = ""
    arguments: dict = field(default_factory=dict)
    command: str = ""
    # Why approval is needed, as against ``arguments["explanation"]``, which is why the model wants the call.
    explanation: str = ""
    # The same, as facts rather than prose, so a client writes the sentence in its own language.
    reason: Any = None
    questions: list = field(default_factory=list)
    # A bash command approval remembers an "always allow" as a session rule.
    is_bash: bool = False
    # The model-facing error if the gate is answered no.
    deny_message: str = ""
    # Who supplied an approval. One of the APPROVED_BY_* values once resolved; empty until resolution.
    approved_by: "ApprovedBy | str" = ""
    # For an egress gate, the remote agent name (an "always allow" is remembered).
    egress_agent: str = ""
    # The widening asked for, carried so approving records exactly what the planner worked out.
    escape: Any = field(default_factory=lambda: _escape_from_dict(None))
    # Whether approving lets this one command reach past the workspace. Set only by a retry gate.
    whole_disk: bool = False
    # What the refusal looked like, so the reviewer judges a command that hit a wall.
    denial_evidence: str = ""
    # What the confined run produced, which a refused retry still owes the model.
    refused_result: Any = None
    # Whether approving this lets a screen script call the primitives that change something.
    grants_screen_mutations: bool = False
    # Whether the reviewer decides this one without a person; announced before the review.
    automatic_review: bool = False

    def to_dict(self) -> dict:
        """Every field as plain data: this dict crosses to a client and into the durable plan, so an omission disappears."""
        return {
            "request_id": self.request_id,
            "tool_call_id": self.tool_call_id,
            "kind": self.kind,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "command": self.command,
            "explanation": self.explanation,
            "reason": self.reason.model_dump()
            if hasattr(self.reason, "model_dump")
            else self.reason,
            "questions": self.questions,
            "is_bash": self.is_bash,
            "deny_message": self.deny_message,
            "egress_agent": self.egress_agent,
            "escape": _escape_to_dict(self.escape),
            "whole_disk": self.whole_disk,
            "denial_evidence": self.denial_evidence,
            "refused_result": self.refused_result,
            "grants_screen_mutations": self.grants_screen_mutations,
            "automatic_review": self.automatic_review,
        }

    @classmethod
    def from_dict(cls, data: dict) -> _PreflightGate:
        return cls(
            request_id=str(data.get("request_id", "")),
            tool_call_id=str(data.get("tool_call_id", "")),
            kind=str(data.get("kind", "permission")),
            tool_name=str(data.get("tool_name", "")),
            arguments=dict(data.get("arguments") or {}),
            command=str(data.get("command", "")),
            explanation=str(data.get("explanation", "")),
            reason=data.get("reason"),
            questions=list(data.get("questions", []) or []),
            is_bash=bool(data.get("is_bash", False)),
            deny_message=str(data.get("deny_message", "")),
            egress_agent=str(data.get("egress_agent", "")),
            # Rebuilt as the real thing: `_approve` reads `.reads`, `.writes` and `.network` off it.
            escape=_escape_from_dict(data.get("escape")),
            whole_disk=bool(data.get("whole_disk", False)),
            denial_evidence=str(data.get("denial_evidence", "")),
            refused_result=data.get("refused_result"),
            grants_screen_mutations=bool(data.get("grants_screen_mutations", False)),
            automatic_review=bool(data.get("automatic_review", False)),
        )


@dataclass
class _ToolPlan:
    """The verdict for one call: a refusal, some gates, or neither. Computed once, so execution only carries it out."""

    tool_call_id: str
    refusal: Optional[dict] = (
        None  # {"code", "message", "denied_injection", "raw_command", "reason"}
    )
    gates: list[_PreflightGate] = field(default_factory=list)
    # Whether a screen script may change something. False by default, so the narrow set is what a call gets.
    screen_mutations: bool = False
    # Set when this call is a second run of a command the operating system refused.
    retry_grant: Any = None
    # A finished call's outcome, held across a suspension so the resumed batch replays rather than re-runs.
    completed: Optional[dict] = None

    @property
    def needs_human(self) -> bool:
        return bool(self.gates)

    @property
    def approved(self) -> bool:
        return self.refusal is None and not self.gates

    def to_dict(self) -> dict:
        return {
            "tool_call_id": self.tool_call_id,
            "refusal": self.refusal,
            "gates": [gate.to_dict() for gate in self.gates],
            "screen_mutations": self.screen_mutations,
            "retry_grant": self.retry_grant.as_dict() if self.retry_grant is not None else None,
            "completed": self.completed,
        }

    @classmethod
    def from_dict(cls, data: dict) -> _ToolPlan:
        retry = data.get("retry_grant")
        return cls(
            tool_call_id=str(data.get("tool_call_id", "")),
            refusal=data.get("refusal"),
            gates=[_PreflightGate.from_dict(gate) for gate in (data.get("gates") or [])],
            screen_mutations=bool(data.get("screen_mutations", False)),
            retry_grant=Grant.from_dict(retry) if retry else None,
            completed=data.get("completed"),
        )


@dataclass
class _ResolvedToolDecision:
    """The verdict a batch runner hands each tool: run it, deny it, or return the ``ask_user`` answers."""

    tool_call_id: str
    approved: bool = True
    denial: Optional[dict] = (
        None  # {"code", "message", "denied_injection", "raw_command", "reason"}
    )
    answers: Any = None  # ask_user: the answers list, or the decline sentinel
    # Whether a screen script may change something. False unless a rule or an answer said otherwise.
    screen_mutations: bool = False
    # The widening an approved second run uses.
    retry_grant: Any = None
    # A finished call's outcome, replayed rather than re-run, so a suspension repeats no side effects.
    completed: Optional[dict] = None


# How a turn phase tells the driver what to do next: a generator cannot return through ``async for``.
_PROCEED = "proceed"  # fall through to the rest of the iteration
_CONTINUE = "continue"  # the phase already advanced loop bookkeeping; loop again
_STOP = "stop"  # the turn is over (a terminal event was already yielded); return


@dataclass
class _ModelCallOutcome:
    """What one streamed model call produced: the response, or the terminal condition the loop must act on."""

    response: Optional[AIMessageChunk] = None
    cancelled: bool = False


@dataclass
class _StepOutcome:
    """The loop directive one step of a turn hands back (see ``_PROCEED``/``_CONTINUE``/``_STOP``)."""

    directive: str = _PROCEED
