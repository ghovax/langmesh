"""The runtime's compaction concern: when and how to compaction a conversation."""

from __future__ import annotations

import logging
import asyncio
from dataclasses import dataclass, replace
from itertools import accumulate, takewhile
from tempfile import TemporaryDirectory
from typing import Any, AsyncIterator, Literal

from pydantic import BaseModel, Field

from langmesh.base.message_content import forget_carried_reasoning
from langmesh.runtime.internals import (
    conversation_tokens,
    message_tokens,
    race_interrupt,
)
from langmesh.runtime.turn_events import (
    CompactionDone,
    CompactionStarted,
    Done,
    TurnEvent,
    Usage,
)
from langmesh.base.ports import CompactionState, CompactionSummaryState
from langmesh.runtime.cache_trace import cache_lane
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langmesh.base.errors import log_fields


logger = logging.getLogger(__name__)


class CompactionSummaryExhausted(RuntimeError):
    """The summarizer never submitted within its configured attempts; the fold cannot proceed without it."""


class CompactionSummary(BaseModel):
    """The durable summary a compaction instruction asks the model to submit."""

    summary: str = Field(
        description="The entire summary of the conversation compacted away, factual and specific."
    )


class ObservationCompactionPreparation:
    """Require an observational-memory revision to advance before compacting."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def instruction(self, default: str) -> str:
        return default

    async def baseline(self) -> int:
        try:
            return await self._store.revision()
        except Exception as error:  # noqa: BLE001 — repair is the preparation turn's job
            logger.warning("observation registry requires repair before compaction: %s", error)
            return 0

    async def completed(self, baseline: Any) -> bool:
        # An absent registry has nothing to hand off, so an empty baseline is a complete handoff.
        if not baseline:
            return True
        try:
            return await self._store.revision() > int(baseline)
        except Exception:  # noqa: BLE001 — an invalid registry is not a completed checkpoint
            return False

    async def describe(self) -> dict:
        return await self._store.describe()


class DirectCompactionPreparation:
    """Compaction directly, for applications that persist no external memory handoff."""

    def instruction(self, default: str) -> None:
        return None

    async def baseline(self) -> None:
        return None

    async def completed(self, baseline: Any) -> bool:
        return True

    async def describe(self) -> dict:
        return {}


@dataclass
class _CompactionControl:
    """The compaction handshake as one state value, so invalid flag combinations cannot accumulate."""

    phase: Literal["none", "waiting", "recorded", "preparation_failed", "compaction_failed"] = "none"
    reason: Literal["auto", "manual", "overflow"] = "manual"
    resume_after: bool = False
    preparation_token: Any = None
    failure: str | None = None
    # Whether the running compaction indicator has already been announced for this preparation.
    started: bool = False

    @property
    def idle(self) -> bool:
        return self.phase == "none"

    @property
    def waiting(self) -> bool:
        return self.phase == "waiting"

    @property
    def recorded(self) -> bool:
        return self.phase == "recorded"

    @property
    def active(self) -> bool:
        return self.phase in {"waiting", "recorded"}

    @property
    def failed(self) -> bool:
        return self.phase in {"preparation_failed", "compaction_failed"}

    def begin(self, *, reason: str, resume_after: bool) -> None:
        if reason not in {"auto", "manual", "overflow"}:
            raise ValueError(f"Invalid compaction reason: {reason}")
        self.phase = "waiting"
        self.reason = reason
        self.resume_after = resume_after
        self.preparation_token = None
        self.failure = None
        self.started = False

    def record(self) -> None:
        if self.phase != "waiting":
            raise RuntimeError(f"Cannot record compaction preparation from {self.phase}.")
        self.phase = "recorded"

    def fail_preparation(self, message: str) -> None:
        self.phase = "preparation_failed"
        self.failure = message

    def fail_compaction(self, message: str) -> None:
        self.phase = "compaction_failed"
        self.failure = message

    def retry_compaction(self) -> None:
        if self.phase != "compaction_failed":
            raise RuntimeError(f"Cannot retry a compaction from {self.phase}.")
        self.phase = "recorded"
        self.failure = None

    def clear(self) -> None:
        self.phase = "none"
        self.reason = "manual"
        self.resume_after = False
        self.preparation_token = None
        self.failure = None
        self.started = False

    def snapshot(self) -> dict:
        return {
            "phase": self.phase,
            "reason": self.reason,
            "resume_after": self.resume_after,
            "preparation_token": self.preparation_token,
            "failure": self.failure,
            "started": self.started,
        }

    @classmethod
    def restore(cls, value: object) -> "_CompactionControl":
        if not isinstance(value, dict):
            return cls()
        phase = value.get("phase")
        reason = value.get("reason")
        return cls(
            phase=phase
            if phase in {"none", "waiting", "recorded", "preparation_failed", "compaction_failed"}
            else "none",
            reason=reason if reason in {"auto", "manual", "overflow"} else "manual",
            resume_after=bool(value.get("resume_after", False)),
            preparation_token=value.get("preparation_token"),
            failure=(str(value["failure"]) if value.get("failure") else None),
            started=bool(value.get("started", False)),
        )


def _without_provider_reasoning(messages: list) -> list:
    """The same messages with the provider-native reasoning cut out, since the turns it explained are gone."""
    # A sweep for its effect, not a transformation: `forget_carried_reasoning` edits each message in place.
    for message in messages:
        forget_carried_reasoning(message)
    return messages


class _CompactsContext:
    """Keep a conversation inside its window after the agent checkpoints workspace knowledge."""

    def _usable_context(self) -> int:
        """How much of the window a conversation may occupy, leaving room for the answer and for the compact itself."""
        window = self._context_window
        if window <= 0:
            return 0
        return max(
            0, window - int(window * self._global_configuration.compaction.output_reserve_fraction)
        )

    def _recent_working_set(self, reason: str = "automatic", recent: list | None = None) -> int:
        """The tail kept verbatim rather than compacted, as a share of the usable window so it scales with the model."""
        fraction = self._global_configuration.compaction.recent_working_set_fraction
        budget = int(self._usable_context() * fraction)
        if budget > 0 and reason != "manual":
            return budget
        # Manual and overflow compaction must remain effective even if a provider failed to
        # report its window. The current conversation is still an honest upper bound.
        measured = int(
            conversation_tokens(self._conversation if recent is None else recent) * fraction
        )
        return min(budget, measured) if budget > 0 else measured

    @staticmethod
    def _without_compaction_preparation(messages: list) -> list:
        """Remove private preparation exchanges while retaining later user input accepted during a failed pass."""
        retained: list = []
        inside_preparation = False
        for message in messages:
            if message.additional_kwargs.get("compaction_preparation"):
                inside_preparation = True
                continue
            if inside_preparation:
                if not isinstance(message, HumanMessage) or message.additional_kwargs.get(
                    "reminder"
                ):
                    continue
                inside_preparation = False
            retained.append(message)
        return retained

    def _compaction_state(
        self, reason: str = "auto", *, context_tokens: int | None = None
    ) -> CompactionState:
        """Expose only the durable conversation to a caller-supplied compaction strategy."""
        return CompactionState(
            messages=self._without_compaction_preparation(self._conversation),
            context_window=self._context_window,
            context_tokens=(
                self._latest_context_tokens if context_tokens is None else context_tokens
            ),
            reason=reason,
        )

    def _next_request_tokens(self, pending_message=None) -> int:
        """Price the exact request being admitted, including a user message not yet appended."""
        messages = self._build_turn_messages()
        if pending_message is not None:
            messages.append(pending_message)
        return conversation_tokens(messages)

    def _at_compacting_threshold(self, next_request_tokens: int) -> bool:
        """Whether the next request is large enough that compacting is worth its cache invalidation."""
        usable = self._usable_context()
        return usable > 0 and next_request_tokens >= (
            self._global_configuration.compaction.reclaim_at_fraction * usable
        )

    def _should_compact(self, pending_message=None) -> bool:
        """The automatic trigger, measured against the usable window, unless a strategy answers it instead."""
        next_request_tokens = max(
            self._latest_context_tokens,
            self._next_request_tokens(pending_message),
        )
        if self._compaction is not None:
            return bool(
                self._compaction.should_compact(
                    self._compaction_state(context_tokens=next_request_tokens)
                )
            )
        compaction = self._global_configuration.compaction
        if not compaction.automatic or not self._at_compacting_threshold(next_request_tokens):
            return False
        return len(self._bounded_tail(self._conversation)) < len(self._conversation)

    def _begin_compaction_preparation(self, *, reason: str, resume_after: bool) -> None:
        """Begin the configured durable handoff before compacting."""
        self._compaction_control.begin(reason=reason, resume_after=resume_after)
        instruction = self._compaction_preparation.instruction(
            self._prompt_loader.load("prepare_compaction", {})
        )
        if instruction is None:
            self._compaction_control.record()
        else:
            self._conversation.append(
                SystemMessage(
                    content=instruction,
                    additional_kwargs={"compaction_preparation": True},
                )
            )
        self._mark_session_dirty()

    def _fail_compaction_preparation(
        self, error: str, *, error_code: str = "compaction_preparation_failed"
    ) -> list[TurnEvent]:
        """Make an incomplete recording handshake the same durable, visible blocker as a failed compaction."""
        messages = len(self._conversation)
        tokens = conversation_tokens(self._conversation)
        self._compaction_control.fail_preparation(error)
        self.discard_pending_steering()
        self._mark_session_dirty()
        events: list[TurnEvent] = []
        if not self._compaction_control.started:
            # A failure that never reached the model call still gets a running start, so the
            # interface never jumps straight from nothing to a failure without a visible phase.
            self._compaction_control.started = True
            events.append(
                CompactionStarted(reason="preparation", messages_before=messages, tokens_before=tokens)
            )
        events.append(
            CompactionDone(
                reason="preparation",
                ok=False,
                messages_before=messages,
                messages_after=messages,
                tokens_before=tokens,
                tokens_after=tokens,
                error_code=error_code,
            )
        )
        return events

    def _bounded_tail(self, recent: list, reason: str = "automatic") -> list:
        """The newest turns that fit the budget, taken whole: recency is not smallness, and none is cut in half."""
        budget = self._recent_working_set(reason, recent)
        if budget <= 0 or not recent:
            return recent
        return self._tail_within_budget(recent, budget)

    @staticmethod
    def _tail_within_budget(recent: list, budget: int) -> list:
        """Return the newest complete messages within an exact token budget."""
        if budget <= 0 or not recent:
            return []
        # How many of the newest turns fit: running totals from the end, taken while they stay inside the budget.
        running = accumulate(message_tokens(message) for message in reversed(recent))
        fitting = sum(1 for _ in takewhile(lambda total: total <= budget, running))
        # A tool result without the call it answers is not a conversation, so the tail never begins on one.
        keep_from = next(
            (
                index
                for index in range(len(recent) - fitting, len(recent))
                if not isinstance(recent[index], ToolMessage)
            ),
            len(recent),
        )
        if keep_from:
            logger.info(
                "recent turns bounded to their budget",
                extra=log_fields(dropped=keep_from, kept=len(recent) - keep_from, budget=budget),
            )
        return recent[keep_from:]

    async def compact(self, reason: str = "manual") -> AsyncIterator[TurnEvent]:
        """Reclaim the window after the explicit observational-memory handoff has completed."""
        if not self._compaction_control.recorded:
            for event in self._fail_compaction_preparation(
                "Compaction requires its configured durable preparation to complete first."
            ):
                yield event
            return
        # A supplied strategy replaces only the discard policy; the recording handshake and
        # visible success/failure boundary remain the runtime's.
        if self._compaction is not None:
            state = self._compaction_state(reason)
            messages_before = len(state.messages)
            tokens_before = self._latest_context_tokens
            original = list(self._conversation)
            yield CompactionStarted(
                reason=reason,
                messages_before=messages_before,
                tokens_before=tokens_before,
            )
            try:
                compacted = _without_provider_reasoning(await self._compaction.compact(state))
                if len(compacted) >= messages_before:
                    raise RuntimeError("The compaction strategy did not reclaim any messages.")
                self._conversation[:] = compacted
                self._latest_context_tokens = conversation_tokens(self._conversation)
            except Exception as error:  # noqa: BLE001 — failure becomes durable, visible turn state
                self._conversation[:] = original
                self._latest_context_tokens = tokens_before
                self._fail_compaction(str(error) or type(error).__name__)
                yield CompactionDone(
                    reason=reason,
                    ok=False,
                    messages_before=messages_before,
                    messages_after=len(self._conversation),
                    tokens_before=tokens_before,
                    tokens_after=self._latest_context_tokens,
                    error_code="compaction_strategy_failed",
                )
                return
            self._compaction_control.clear()
            self._mark_session_dirty()
            self._cached_system_prompt = None
            yield CompactionDone(
                reason=reason,
                ok=True,
                messages_before=messages_before,
                messages_after=len(self._conversation),
                tokens_before=tokens_before,
                tokens_after=self._latest_context_tokens,
            )
            return
        # The explicit handoff has committed. The compact itself only changes conversation state.
        original = list(self._conversation)
        compactable = self._without_compaction_preparation(original)
        messages_before = len(compactable)
        tokens_before = self._latest_context_tokens
        yield CompactionStarted(
            reason=reason, messages_before=messages_before, tokens_before=tokens_before
        )
        kept = self._bounded_tail(compactable, reason)
        if len(kept) >= messages_before:
            # A manual no-op is not a broken session. Automatic/overflow attempts that cannot
            # reclaim space are failures and stop future input until the user retries.
            ok = reason == "manual"
            error = "" if ok else "Compaction could not reclaim any messages."
            error_code = None if ok else "compaction_no_reclaim"
            if error:
                self._fail_compaction(error)
            if ok:
                try:
                    self._conversation[:] = compactable
                    self._latest_context_tokens = conversation_tokens(self._conversation)
                except Exception as failure:  # noqa: BLE001 — represented as a retryable UI blocker
                    self._conversation[:] = original
                    self._latest_context_tokens = tokens_before
                    error = str(failure) or type(failure).__name__
                    error_code = "compaction_failed"
                    self._fail_compaction(error)
                    ok = False
                else:
                    self._compaction_control.clear()
                    self._mark_session_dirty()
                    self._cached_system_prompt = None
            yield CompactionDone(
                reason=reason,
                ok=ok,
                messages_before=messages_before,
                messages_after=len(self._conversation) if ok else messages_before,
                tokens_before=tokens_before,
                tokens_after=self._latest_context_tokens,
                error_code=error_code,
            )
            return
        older = compactable[: len(compactable) - len(kept)]
        try:
            summary = await self._summarize_compacted(older, compactable) if older else None
        except CompactionSummaryExhausted as error:
            # The summary is the durable memory the tail resumes from: without it the fold must not
            # proceed, and the session stays blocked until the user retries the compaction.
            self._conversation[:] = original
            self._latest_context_tokens = tokens_before
            self._fail_compaction(str(error))
            yield CompactionDone(
                reason=reason,
                ok=False,
                messages_before=messages_before,
                messages_after=len(self._conversation),
                tokens_before=tokens_before,
                tokens_after=self._latest_context_tokens,
                error_code="compaction_summary_failed",
            )
            return
        if self._abort_event.is_set():
            # The person stopped the fold mid-summary; report a terminal, non-blocking cancellation.
            yield CompactionDone(
                reason=reason,
                ok=False,
                messages_before=messages_before,
                messages_after=len(self._conversation),
                tokens_before=tokens_before,
                tokens_after=self._latest_context_tokens,
                error_code="compaction_cancelled",
            )
            return
        retained = (
            [self._summary_message(summary), *kept] if summary else list(kept)
        )
        try:
            self._conversation[:] = _without_provider_reasoning(retained)
            self._latest_context_tokens = conversation_tokens(self._conversation)
        except Exception as error:  # noqa: BLE001 — represented as a retryable UI blocker
            self._conversation[:] = original
            self._latest_context_tokens = tokens_before
            self._fail_compaction(str(error) or type(error).__name__)
            yield CompactionDone(
                reason=reason,
                ok=False,
                messages_before=messages_before,
                messages_after=len(self._conversation),
                tokens_before=tokens_before,
                tokens_after=self._latest_context_tokens,
                error_code="compaction_failed",
            )
            return
        self._compaction_control.clear()
        self._mark_session_dirty()
        self._cached_system_prompt = None
        yield CompactionDone(
            reason=reason,
            ok=True,
            messages_before=messages_before,
            messages_after=len(self._conversation),
            tokens_before=tokens_before,
            tokens_after=self._latest_context_tokens,
        )

    @staticmethod
    def _summary_message(summary: str) -> HumanMessage:
        """One cache-stable message that replaces the compacted turns, invisible to the interface."""
        return HumanMessage(
            content=summary,
            additional_kwargs={"reminder": True, "summary": True},
        )

    async def _summarize_compacted(self, older: list, conversation: list) -> str | None:
        """Ask the model to distil the conversation before compacting, preserving its cache prefix."""
        state = CompactionSummaryState(
            messages=tuple(older),
            system_prompt=self._build_static_system_prompt(),
        )
        if self._compaction_summarizer is not None:
            try:
                with cache_lane("compaction-summary"):
                    summary = await self._compaction_summarizer.summarize(state)
            except Exception as error:  # noqa: BLE001 — a failed summary never blocks the compaction
                logger.warning("compaction summarizer failed; compacting without one: %s", error)
                return None
            return str(summary or "").strip() or None
        instruction = self._prompt_loader.load("compaction_summary", {})
        with TemporaryDirectory(prefix="langmesh-compaction-summary-") as scratch_directory:
            summarizer = self._compaction_summarizer_runtime(scratch_directory)
            try:
                attempts = 0
                last_text = ""
                while not self._abort_event.is_set():
                    streamed: dict[str, Any] = {"last_text": "", "last_usage": None}

                    async def _consume() -> None:
                        with cache_lane("compaction-summary"):
                            async for _event in summarizer.stream(
                                instruction, as_system_note=False, opens_exchange=True
                            ):
                                # The hidden session is private: nothing here is published.
                                if isinstance(_event, Done):
                                    streamed["last_text"] = _event.text
                                if isinstance(_event, Usage):
                                    streamed["last_usage"] = _event

                    stream_task = asyncio.create_task(_consume())
                    if await race_interrupt(stream_task, self._abort_event):
                        summarizer.abort()
                    await stream_task
                    if self._abort_event.is_set():
                        break
                    last_text = streamed["last_text"] or last_text
                    last_usage = streamed["last_usage"]
                    submitted = summarizer._submitted_compaction_summary
                    if submitted is not None:
                        if last_usage is not None:
                            logger.info(
                                "compaction summary cache lane=compaction-summary prefix_intact=%s cache_read_tokens=%d reachable_tokens=%d shared_segments=%d segments=%d input_tokens=%d output_tokens=%d",
                                last_usage.prefix_intact,
                                last_usage.cache_read_tokens,
                                last_usage.reachable_tokens,
                                last_usage.shared_segments,
                                last_usage.segments,
                                last_usage.input_tokens,
                                last_usage.output_tokens,
                            )
                        return str(submitted.summary or "").strip() or None
                    attempts += 1
                    summary_attempts = self._global_configuration.compaction.summary_attempts
                    if attempts >= summary_attempts:
                        logger.error(
                            "compaction summarizer did not submit after %d attempts; last text: %r",
                            attempts,
                            last_text,
                        )
                        raise CompactionSummaryExhausted(
                            f"The compaction summarizer did not submit a summary after {attempts} attempts, so the conversation was not compacted."
                        )
                    logger.warning(
                        "the compaction summarizer stopped without submitting its summary (attempt %d/%d); continuing it",
                        attempts,
                        summary_attempts,
                    )
                    self._require_compaction_summary_submission(summarizer)
                    instruction = self._prompt_loader.load("compaction_summary_missing", {})
            except CompactionSummaryExhausted:
                raise
            except Exception as error:  # noqa: BLE001 — a failed summary never blocks the compaction
                logger.exception("compaction summary failed; compacting without one: %s", error)
                return None
            finally:
                summarizer.abort()
        return None

    @staticmethod
    def _require_compaction_summary_submission(summarizer) -> None:
        """Constrain a summarizer that already reviewed the conversation to its one accepted verdict tool, including what the model is bound to."""
        summary_tool = next(
            tool for tool in summarizer._tools if tool.name == "submit_compaction_summary"
        )
        summarizer._tools = [summary_tool]
        summarizer._tool_schemas = {summary_tool.name: summary_tool.args_schema}
        summarizer._model_tools = [summary_tool]
        summarizer._bound_model = summarizer._model.bind_tools([summary_tool])

    def _compaction_summarizer_runtime(self, scratch_directory: str):
        from langmesh.runtime.tools.registry import submit_compaction_summary as submit_compaction_summary_tool
        """The hidden session that produces the compaction summary, mirroring the goal reviewer."""
        from langmesh.base.configuration import PermissionEvaluator
        from langmesh.runtime.composition import RuntimeComponents, RuntimeProfile
        from langmesh.runtime.runtime import AgentRuntime

        summarizer_configuration = self._agent_configuration.model_copy(
            update={"permission_mode": "automatic"}
        )
        summarizer_global_configuration = self._global_configuration.model_copy(
            update={
                "toolbox": self._global_configuration.toolbox.model_copy(update={"enabled": False}),
                # The hidden session inherits the full conversation, so it must never compact itself.
                "compaction": self._global_configuration.compaction.model_copy(
                    update={"automatic": False}
                ),
            }
        )
        summarizer_permissions = PermissionEvaluator(
            summarizer_configuration.model_copy(
                update={
                    "tools": summarizer_configuration.tools.model_copy(
                        update={
                            "bash": summarizer_configuration.tools.bash.model_copy(
                                update={"background_allowed": False}
                            ),
                            "disabled": sorted(
                                set(summarizer_configuration.tools.disabled)
                                | {tool.name for tool in self._tools}
                            ),
                        }
                    )
                }
            )
        )
        granted_sandbox = self._granted_profile()
        summarizer_sandbox = granted_sandbox.narrowed(
            writable=(scratch_directory,),
            network=granted_sandbox.network,
            workspace=self._working_directory,
        )
        summarizer_sandbox = replace(
            summarizer_sandbox,
            environment={
                **summarizer_sandbox.environment,
                "TMPDIR": scratch_directory,
                "XDG_CACHE_HOME": scratch_directory,
            },
        )
        summarizer = AgentRuntime(
            RuntimeProfile(
                agent=summarizer_configuration,
                configuration=summarizer_global_configuration,
                session_id=self._session_id,
                working_directory=self._working_directory,
                project_directory=self._project_directory,
                permission_mode="automatic",
                parent_session=self._parent_session,
                sandbox=summarizer_sandbox,
            ),
            RuntimeComponents(
                model=self._model,
                catalogue=self._catalogue,
                sessions=None,
                mcp_servers=self._tool_context.mcp_server_manager,
                # The hidden summarizer is one summary call: only its verdict tool is bound.
                toolset=(submit_compaction_summary_tool,),
                supplied_tool_gate=self._supplied_tool_gate,
                permissions=summarizer_permissions,
            ),
            conversation=list(self._conversation),
        )
        summarizer._locations = dict(self._locations)
        summarizer._locations_by_name = dict(self._locations_by_name)
        summarizer._tool_context = replace(summarizer._tool_context, toolbox=None)
        summarizer.restore_session(self.session_snapshot())
        # A fresh handshake keeps the hidden session from folding its own summary turn.
        summarizer._compaction_control = _CompactionControl()
        summarizer._cached_system_prompt = self._build_static_system_prompt()
        summarizer._attached_files = dict(self._attached_files)
        return summarizer

class KeepRecentTurns:
    """Keep the last `keep` exchanges and drop the rest, with no model call and no cost."""

    def __init__(self, keep: int = 20) -> None:
        if keep < 1:
            raise ValueError(f"keep must be at least 1, got {keep}.")
        self._keep = keep

    def should_compact(self, state) -> bool:
        return len(state.messages) > self._keep * 2

    async def compact(self, state) -> list:
        return list(state.messages[-self._keep * 2 :])


__all__ = [
    "DirectCompactionPreparation",
    "KeepRecentTurns",
    "ObservationCompactionPreparation",
]
