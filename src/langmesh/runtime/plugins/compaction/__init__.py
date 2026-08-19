"""The compaction plugin: the durable handshake, the phased fold, and the summary turn.

Keeping a conversation inside its window is one self-contained concern: the phase machine,
the recording handoff, the bounded tail, and the hidden summarizer session. Its prompts and
the durable summary's schema description live beside this module so they are configurable,
and the strategy, preparation, and summarizer ports are whatever the caller composed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from itertools import accumulate, takewhile
from tempfile import TemporaryDirectory
from typing import Any, AsyncIterator, Literal

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langmesh.base.configuration import PermissionEvaluator
from langmesh.base.content.message_content import forget_carried_reasoning
from langmesh.base.contracts.ports import CompactionState, CompactionSummaryState
from langmesh.base.primitives.errors import log_fields
from langmesh.runtime.composition import RuntimeComponents, RuntimeProfile
from langmesh.runtime.features import Feature, PluginContext, PluginHost
from langmesh.runtime.features.events import MemoryHandoffFailed, MemoryHandoffVerified
from langmesh.runtime.plugins.bash import bash as bash_tool
from langmesh.runtime.plugins.compaction.tools import (
    submit_compaction_summary as submit_compaction_summary_tool,
)
from langmesh.runtime.plugins.continuation import Continuation
from langmesh.runtime.plugins.goal_review import GoalReviewFeature
from langmesh.runtime.plugins.compaction.ports import (
    CompactionSummary,
    DirectCompactionPreparation,
    KeepRecentTurns,
    ObservationCompactionPreparation,
)
from langmesh.runtime.runtime import AgentRuntime
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
from langmesh.runtime.cache_trace import cache_lane

logger = logging.getLogger(__name__)

class CompactionSummaryExhausted(RuntimeError):
    """The summarizer never submitted within its configured attempts; the fold cannot proceed without it."""

@dataclass
class CompactionControl:
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
    def restore(cls, value: object) -> "CompactionControl":
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

class Compaction(Feature):
    """Keep a conversation inside its window after the agent checkpoints workspace knowledge."""

    def contribute_tools(self) -> list:
        """The summary-submission tool this plugin owns."""
        return [submit_compaction_summary_tool]

    def __init__(
        self,
        *,
        strategy: Any = None,
        preparation: Any = None,
        summarizer: Any = None,
    ) -> None:
        # The caller's strategy and handoff replace only their own step; the fold stays this plugin's.
        self._strategy = strategy
        self._preparation = preparation if preparation is not None else DirectCompactionPreparation()
        self._summarizer = summarizer
        self._control = CompactionControl()
        self._submitted_summary: Any = None

    def attach(self, context: PluginContext, host: PluginHost) -> None:
        self._context = context
        self._host = host
        self._prompts = context.prompts("compaction")

    @property
    def control(self) -> CompactionControl:
        return self._control

    def restore_control(self, value: object) -> None:
        self._control = CompactionControl.restore(value)

    def fail_compaction(self, message: str) -> None:
        """Make a failed fold durable and visible, and release the senders it held outside the conversation."""
        self._control.fail_compaction(message)
        self._host.turn.discard_pending_steering()
        self._host.bookkeeping.note_state_changed()

    @property
    def failure(self) -> str | None:
        return self._control.failure

    def blocks_input(self) -> str | None:
        """A failed fold refuses new input until an explicit retry succeeds."""
        return self._control.failure

    def submit_summary(self, summary: Any) -> None:
        """The summarizer's verdict tool lands here, read once the summary turn ends."""
        self._submitted_summary = summary

    def invoke(self, name: str, *args, **kwargs):
        """Answer the compaction capabilities the core and tools ask for by name."""
        if name == "submit_compaction_summary":
            (summary,) = args
            self.submit_summary(summary)
            return True
        if name == "compaction_failure":
            return self.failure()
        return None

    @property
    def submitted_summary(self) -> Any:
        return self._submitted_summary

    def usable_context(self) -> int:
        """How much of the window a conversation may occupy, leaving room for the answer and for the compact itself."""
        window = self._host.window.context_window
        if window <= 0:
            return 0
        return max(
            0, window - int(window * self._context.global_configuration.compaction.output_reserve_fraction)
        )

    def _recent_working_set(self, reason: str = "automatic", recent: list | None = None) -> int:
        """The tail kept verbatim rather than compacted, as a share of the usable window so it scales with the model."""
        fraction = self._context.global_configuration.compaction.recent_working_set_fraction
        budget = int(self.usable_context() * fraction)
        if budget > 0 and reason != "manual":
            return budget
        # Manual and overflow compaction must remain effective even if a provider failed to report its window. The current conversation is still an honest upper bound.
        measured = int(
            conversation_tokens(self._host.conversation.messages if recent is None else recent) * fraction
        )
        return min(budget, measured) if budget > 0 else measured

    @staticmethod
    def without_preparation(messages: list) -> list:
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
            messages=self.without_preparation(self._host.conversation.messages),
            context_window=self._host.window.context_window,
            context_tokens=(
                self._host.window.latest_context_tokens if context_tokens is None else context_tokens
            ),
            reason=reason,
        )

    def _next_request_tokens(self, pending_message=None) -> int:
        """Price the exact request being admitted, including a user message not yet appended."""
        messages = self._host.turn.build_turn_messages()
        if pending_message is not None:
            messages.append(pending_message)
        return conversation_tokens(messages)

    def _at_compacting_threshold(self, next_request_tokens: int) -> bool:
        """Whether the next request is large enough that compacting is worth its cache invalidation."""
        usable = self.usable_context()
        return usable > 0 and next_request_tokens >= (
            self._context.global_configuration.compaction.reclaim_at_fraction * usable
        )

    def should_maintain(self, request_tokens: int) -> bool:
        """The automatic trigger, measured against the usable window, unless a strategy answers it instead."""
        if self._strategy is not None:
            return bool(
                self._strategy.should_compact(
                    self._compaction_state(context_tokens=request_tokens)
                )
            )
        compaction = self._context.global_configuration.compaction
        if not compaction.automatic or not self._at_compacting_threshold(request_tokens):
            return False
        return len(self.bounded_tail(self._host.conversation.messages)) < len(self._host.conversation.messages)

    def maintenance_active(self) -> bool:
        """Whether this plugin is currently holding the loop to reclaim context."""
        return bool(self._control.active)

    def begin_maintenance(self, *, reason: str, resume_after: bool) -> None:
        self.begin_preparation(reason=reason, resume_after=resume_after)

    def maintenance_ready(self) -> bool:
        return bool(self._control.recorded)

    def maintenance_reason(self) -> str:
        return self._control.reason

    def valid_during_maintenance(self, call: dict) -> bool:
        """Only the handoff protocol itself may run while the loop is held: local foreground Bash and read-only skill loading."""
        if call.get("name") == "bash":
            return not str((call.get("args") or {}).get("location") or "").strip() and not bool(
                (call.get("args") or {}).get("background")
            )
        return call.get("name") == "load_skill"

    def maintenance_tool_schemas(self) -> dict:
        """Bash is valid during the handoff even for a session whose profile omits it."""
        return {"bash": bash_tool.args_schema}

    def maintenance_violation_message(self) -> str:
        return self._prompts.load("compaction_preparation_violation", {})

    async def fail_maintenance(self, message: str):
        """The hold could not complete; make it the same durable, visible blocker as a failed fold."""
        for event in self.fail_preparation(message):
            yield event

    def record_maintenance_handoff(self) -> None:
        """The model declined to act during the handoff; record it and move on."""
        self.record_preparation()

    async def maintenance_describe(self) -> dict:
        return await self._preparation.describe()

    async def advance_maintenance(self):
        """Advance the recording handoff one step, announcing the phase when it begins."""
        if self._control.waiting and self._control.preparation_token is None:
            self._control.preparation_token = await self._preparation.baseline()
            self._host.bookkeeping.note_state_changed()
        if self._control.waiting and self._control.preparation_token is not None:
            if await self._preparation.completed(self._control.preparation_token):
                # The write may have committed just before a process stopped or a checkpoint was persisted. Its revision is the durable acknowledgement; do not ask the model to repeat a side effect merely because the in-memory state was lost.
                self.record_preparation()
        if self._control.waiting and not self._control.started:
            # The indicator opens when the recording handoff begins, not when the compaction finally runs: preparation is the long phase, and a session restart must not drop it.
            self._control.started = True
            self._host.bookkeeping.note_state_changed()
            yield CompactionStarted(
                reason=self._control.reason,
                messages_before=len(self.without_preparation(self._host.conversation.messages)),
                tokens_before=conversation_tokens(self._host.conversation.messages),
            )

    async def run_maintenance(self, *, reason: str):
        """Complete the held handoff and reclaim the window."""
        if self._control.recorded:
            try:
                metadata = await self._preparation.describe()
            except Exception as error:  # noqa: BLE001 — the fold's verification below remains authoritative
                self._context.bus.emit(
                    MemoryHandoffFailed(str(error) or type(error).__name__)
                )
            else:
                self._context.bus.emit(MemoryHandoffVerified(metadata))
        async for event in self.compact(reason):
            yield event

    def begin_preparation(self, *, reason: str, resume_after: bool) -> None:
        """Begin the configured durable handoff before compacting."""
        self._control.begin(reason=reason, resume_after=resume_after)
        instruction = self._preparation.instruction(
            self._prompts.load("prepare_compaction", {})
        )
        if instruction is None:
            self._control.record()
        else:
            self._host.conversation.messages.append(
                SystemMessage(
                    content=instruction,
                    additional_kwargs={"compaction_preparation": True},
                )
            )
        self._host.bookkeeping.note_state_changed()

    def record_preparation(self) -> None:
        self._control.record()
        self._host.bookkeeping.note_state_changed()

    def retry(self) -> str | None:
        """Reopen exactly the failed phase and return the operation to drive."""
        if self._control.phase == "compaction_failed":
            self._control.retry_compaction()
            self._host.bookkeeping.note_state_changed()
            return "compaction"
        if self._control.phase != "preparation_failed":
            return None
        # A retry gets one unambiguous preparation notice. Retain any accepted user message that followed the failed private segment while removing that segment's discarded work.
        self._host.conversation.messages[:] = self.without_preparation(self._host.conversation.messages)
        self.begin_preparation(
            reason=self._control.reason,
            resume_after=self._control.resume_after,
        )
        return "prepare"

    def begin_explicit(self) -> bool:
        """Begin an explicit compaction's recording handshake when no other compaction state is active."""
        if self._control.failure or not self._control.idle:
            return False
        self.begin_preparation(reason="manual", resume_after=False)
        return True

    def fail_preparation(
        self, error: str, *, error_code: str = "compaction_preparation_failed"
    ) -> list[TurnEvent]:
        """Make an incomplete recording handshake the same durable, visible blocker as a failed compaction."""
        messages = len(self._host.conversation.messages)
        tokens = conversation_tokens(self._host.conversation.messages)
        self._control.fail_preparation(error)
        self._host.turn.discard_pending_steering()
        self._host.bookkeeping.note_state_changed()
        events: list[TurnEvent] = []
        if not self._control.started:
            # A failure that never reached the model call still gets a running start, so the interface never jumps straight from nothing to a failure without a visible phase.
            self._control.started = True
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

    def bounded_tail(self, recent: list, reason: str = "automatic") -> list:
        """The newest turns that fit the budget, taken whole: recency is not smallness, and none is cut in half."""
        budget = self._recent_working_set(reason, recent)
        if budget <= 0 or not recent:
            return recent
        return self.tail_within_budget(recent, budget)

    @staticmethod
    def tail_within_budget(recent: list, budget: int) -> list:
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
        if not self._control.recorded:
            for event in self.fail_preparation(
                "Compaction requires its configured durable preparation to complete first."
            ):
                yield event
            return
        # A supplied strategy replaces only the discard policy; the recording handshake and visible success/failure boundary remain the plugin's.
        if self._strategy is not None:
            state = self._compaction_state(reason)
            messages_before = len(state.messages)
            tokens_before = self._host.window.latest_context_tokens
            original = list(self._host.conversation.messages)
            yield CompactionStarted(
                reason=reason,
                messages_before=messages_before,
                tokens_before=tokens_before,
            )
            try:
                compacted = _without_provider_reasoning(await self._strategy.compact(state))
                if len(compacted) >= messages_before:
                    raise RuntimeError("The compaction strategy did not reclaim any messages.")
                self._host.conversation.messages[:] = compacted
                self._host.window.set_latest_context_tokens(
                    conversation_tokens(self._host.conversation.messages)
                )
            except Exception as error:  # noqa: BLE001 — failure becomes durable, visible turn state
                self._host.conversation.messages[:] = original
                self._host.window.set_latest_context_tokens(tokens_before)
                self.fail_compaction(str(error) or type(error).__name__)
                yield CompactionDone(
                    reason=reason,
                    ok=False,
                    messages_before=messages_before,
                    messages_after=len(self._host.conversation.messages),
                    tokens_before=tokens_before,
                    tokens_after=self._host.window.latest_context_tokens,
                    error_code="compaction_strategy_failed",
                )
                return
            self._control.clear()
            self._host.bookkeeping.note_state_changed()
            self._host.window.refresh_cached_prompt()
            yield CompactionDone(
                reason=reason,
                ok=True,
                messages_before=messages_before,
                messages_after=len(self._host.conversation.messages),
                tokens_before=tokens_before,
                tokens_after=self._host.window.latest_context_tokens,
            )
            return
        # The explicit handoff has committed. The compact itself only changes conversation state.
        original = list(self._host.conversation.messages)
        compactable = self.without_preparation(original)
        messages_before = len(compactable)
        tokens_before = self._host.window.latest_context_tokens
        yield CompactionStarted(
            reason=reason, messages_before=messages_before, tokens_before=tokens_before
        )
        kept = self.bounded_tail(compactable, reason)
        if len(kept) >= messages_before:
            # A manual no-op is not a broken session. Automatic/overflow attempts that cannot reclaim space are failures and stop future input until the user retries.
            ok = reason == "manual"
            error = "" if ok else "Compaction could not reclaim any messages."
            error_code = None if ok else "compaction_no_reclaim"
            if error:
                self.fail_compaction(error)
            if ok:
                try:
                    self._host.conversation.messages[:] = compactable
                    self._host.window.set_latest_context_tokens(
                        conversation_tokens(self._host.conversation.messages)
                    )
                except Exception as failure:  # noqa: BLE001 — represented as a retryable UI blocker
                    self._host.conversation.messages[:] = original
                    self._host.window.set_latest_context_tokens(tokens_before)
                    error = str(failure) or type(failure).__name__
                    error_code = "compaction_failed"
                    self.fail_compaction(error)
                    ok = False
                else:
                    self._control.clear()
                    self._host.bookkeeping.note_state_changed()
                    self._host.window.refresh_cached_prompt()
            yield CompactionDone(
                reason=reason,
                ok=ok,
                messages_before=messages_before,
                messages_after=len(self._host.conversation.messages) if ok else messages_before,
                tokens_before=tokens_before,
                tokens_after=self._host.window.latest_context_tokens,
                error_code=error_code,
            )
            return
        older = compactable[: len(compactable) - len(kept)]
        try:
            summary = await self._summarize_compacted(older, compactable) if older else None
        except CompactionSummaryExhausted as error:
            # The summary is the durable memory the tail resumes from: without it the fold must not proceed, and the session stays blocked until the user retries the compaction.
            self._host.conversation.messages[:] = original
            self._host.window.set_latest_context_tokens(tokens_before)
            self.fail_compaction(str(error))
            yield CompactionDone(
                reason=reason,
                ok=False,
                messages_before=messages_before,
                messages_after=len(self._host.conversation.messages),
                tokens_before=tokens_before,
                tokens_after=self._host.window.latest_context_tokens,
                error_code="compaction_summary_failed",
            )
            return
        if self._host.turn.abort_event.is_set():
            # The person stopped the fold mid-summary; report a terminal, non-blocking cancellation.
            yield CompactionDone(
                reason=reason,
                ok=False,
                messages_before=messages_before,
                messages_after=len(self._host.conversation.messages),
                tokens_before=tokens_before,
                tokens_after=self._host.window.latest_context_tokens,
                error_code="compaction_cancelled",
            )
            return
        retained = (
            [self._summary_message(summary), *kept] if summary else list(kept)
        )
        try:
            self._host.conversation.messages[:] = _without_provider_reasoning(retained)
            self._host.window.set_latest_context_tokens(
                conversation_tokens(self._host.conversation.messages)
            )
        except Exception as error:  # noqa: BLE001 — represented as a retryable UI blocker
            self._host.conversation.messages[:] = original
            self._host.window.set_latest_context_tokens(tokens_before)
            self.fail_compaction(str(error) or type(error).__name__)
            yield CompactionDone(
                reason=reason,
                ok=False,
                messages_before=messages_before,
                messages_after=len(self._host.conversation.messages),
                tokens_before=tokens_before,
                tokens_after=self._host.window.latest_context_tokens,
                error_code="compaction_failed",
            )
            return
        self._control.clear()
        self._host.bookkeeping.note_state_changed()
        self._host.window.refresh_cached_prompt()
        yield CompactionDone(
            reason=reason,
            ok=True,
            messages_before=messages_before,
            messages_after=len(self._host.conversation.messages),
            tokens_before=tokens_before,
            tokens_after=self._host.window.latest_context_tokens,
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
            system_prompt=self._host.turn.build_static_system_prompt(),
        )
        if self._summarizer is not None:
            try:
                with cache_lane("compaction-summary"):
                    summary = await self._summarizer.summarize(state)
            except Exception as error:  # noqa: BLE001 — a failed summary never blocks the compaction
                logger.warning("compaction summarizer failed; compacting without one: %s", error)
                return None
            return str(summary or "").strip() or None
        instruction = self._prompts.load("compaction_summary", {})
        with TemporaryDirectory(prefix="langmesh-compaction-summary-") as scratch_directory:
            summarizer = self._compaction_summarizer_runtime(scratch_directory)
            try:
                from langmesh.runtime.verdict import drive_verdict_session

                summary_attempts = (
                    self._context.global_configuration.compaction.summary_attempts
                )
                last_text = ""

                async def _run_turn(current_instruction: str) -> bool:
                    nonlocal last_text
                    streamed: dict[str, Any] = {"last_text": "", "last_usage": None}

                    async def _consume() -> None:
                        with cache_lane("compaction-summary"):
                            async for _event in summarizer.stream(
                                current_instruction, as_system_note=False, opens_exchange=True
                            ):
                                # The hidden session is private: nothing here is published.
                                if isinstance(_event, Done):
                                    streamed["last_text"] = _event.text
                                if isinstance(_event, Usage):
                                    streamed["last_usage"] = _event

                    stream_task = asyncio.create_task(_consume())
                    if await race_interrupt(stream_task, self._host.turn.abort_event):
                        summarizer.abort()
                    await stream_task
                    last_text = streamed["last_text"] or last_text
                    last_usage = streamed["last_usage"]
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
                    return not self._host.turn.abort_event.is_set()

                def _submitted():
                    feature = summarizer._features.by_type(Compaction)
                    return feature.submitted_summary if feature is not None else None

                def _on_empty(attempt: int, maximum: int) -> None:
                    logger.warning(
                        "the compaction summarizer stopped without submitting its summary (attempt %d/%d); continuing it",
                        attempt,
                        maximum,
                    )

                def _on_exhausted():
                    logger.error(
                        "compaction summarizer did not submit after %d attempts; last text: %r",
                        summary_attempts,
                        last_text,
                    )
                    raise CompactionSummaryExhausted(
                        f"The compaction summarizer did not submit a summary after {summary_attempts} attempts, so the conversation was not compacted."
                    )

                submitted = await drive_verdict_session(
                    attempts=summary_attempts,
                    reason="compaction summary",
                    run_turn=_run_turn,
                    submitted=_submitted,
                    require_submission=lambda: self._require_summary_submission(summarizer),
                    missing_instruction=lambda: self._prompts.load(
                        "compaction_summary_missing", {}
                    ),
                    aborted=lambda: self._host.turn.abort_event.is_set(),
                    initial_instruction=instruction,
                    on_empty=_on_empty,
                    on_exhausted=_on_exhausted,
                )
                if submitted is not None:
                    return str(submitted.summary or "").strip() or None
            except CompactionSummaryExhausted:
                raise
            except Exception as error:  # noqa: BLE001 — a failed summary never blocks the compaction
                logger.exception("compaction summary failed; compacting without one: %s", error)
                return None
            finally:
                summarizer.abort()
        return None

    @staticmethod
    def _require_summary_submission(summarizer) -> None:
        """Constrain a summarizer that already reviewed the conversation to its one accepted verdict tool, including what the model is bound to."""
        summary_tool = next(
            tool for tool in summarizer.constrained_tool_named("submit_compaction_summary")
        )
        summarizer.constrain_toolset([summary_tool])

    def _compaction_summarizer_runtime(self, scratch_directory: str):
        """The hidden session that produces the compaction summary, mirroring the goal reviewer."""
        summarizer_configuration = self._context.agent_configuration.model_copy(
            update={"permission_mode": "automatic"}
        )
        summarizer_global_configuration = self._context.global_configuration.model_copy(
            update={
                "toolbox": self._context.global_configuration.toolbox.model_copy(update={"enabled": False}),
                # The hidden session inherits the full conversation, so it must never compact itself.
                "compaction": self._context.global_configuration.compaction.model_copy(
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
                            )
                        }
                    )
                }
            )
        )
        granted_sandbox = self._host.boundary.granted_profile()
        summarizer_sandbox = granted_sandbox.narrowed(
            writable=(scratch_directory,),
            network=granted_sandbox.network,
            workspace=self._context.working_directory,
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
                session_id=self._context.session_id,
                working_directory=self._context.working_directory,
                project_directory=self._context.project_directory,
                permission_mode="automatic",
                parent_session=self._context.parent_session,
                sandbox=summarizer_sandbox,
            ),
            RuntimeComponents(
                model=self._host.conversation.model,
                catalogue=self._context.catalogue,
                sessions=None,
                mcp_servers=self._host.tools.tool_context.mcp_server_manager,
                # The hidden summarizer is one summary call: only its verdict tool is bound.
                toolset=(submit_compaction_summary_tool,),
                tool_gate=self._host.tools.tool_gate,
                permissions=summarizer_permissions,
                features=[
                    feature_class()
                    for feature_class in self._host.turn.feature_classes(
                        GoalReviewFeature, Continuation
                    )
                ],
            ),
            conversation=list(self._host.conversation.messages),
        )
        summarizer._tool_context = replace(summarizer._tool_context, toolbox=None)
        summarizer.restore_session(self._host.bookkeeping.session_snapshot())
        # A fresh handshake keeps the hidden session from folding its own summary turn.
        summarizer_feature = summarizer._features.by_type(Compaction)
        if summarizer_feature is not None:
            summarizer_feature.control.clear()
        summarizer._cached_system_prompt = self._host.turn.build_static_system_prompt()
        summarizer._attached_files = dict(self._host.boundary.attached_files)
        return summarizer

    def preparation_violation_message(self) -> str:
        """The refusal the model is given for calling outside the private handshake."""
        return self._prompts.load("compaction_preparation_violation", {})

__all__ = [
    "Compaction",
    "CompactionControl",
    "CompactionSummaryExhausted",
    "CompactionSummary",
    "DirectCompactionPreparation",
    "KeepRecentTurns",
    "ObservationCompactionPreparation",
]
