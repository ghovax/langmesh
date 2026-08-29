"""The compaction plugin: the durable handshake, the phased fold, and the summary turn.

Keeping a conversation inside its window is one self-contained concern: the phase machine,
the recording handoff, the bounded tail, and the bounded summary call. Its prompts and
the durable summary's schema description live beside this module so they are configurable,
and the strategy, preparation, and summarizer ports are whatever the caller composed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from itertools import accumulate, takewhile
from typing import Any, AsyncIterator, Literal, cast

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langmesh.base.content.message_content import forget_carried_reasoning
from langmesh.base.contracts.ports import CompactionState, CompactionSummaryState
from langmesh.base.primitives.errors import log_fields
from langmesh.runtime.features import Feature, PluginContext, PluginHost
from langmesh.runtime.features.events import MemoryHandoffFailed, MemoryHandoffVerified
from langmesh.runtime.plugins.bash import bash as bash_tool
from langmesh.runtime.plugins.compaction.tools import (
    submit_compaction_summary as submit_compaction_summary_tool,
)
from langmesh.runtime.plugins.compaction.ports import (
    CompactionSummary,
    DirectCompactionPreparation,
    ObservationCompactionPreparation,
)
from langmesh.runtime.plugins.compaction.configuration import CompactionConfiguration
from langmesh.runtime.internals import (
    await_interruptible,
    conversation_tokens,
    message_tokens,
)
from langmesh.runtime.turn_events import CompactionDone, CompactionStarted, TurnEvent
from langmesh.runtime.cache_trace import cache_lane
from langmesh.runtime.verdict import collect_structured_call

logger = logging.getLogger(__name__)


@dataclass
class CompactionControl:
    """The compaction handshake as one state value, so invalid flag combinations cannot accumulate."""

    phase: Literal["none", "waiting", "recorded", "preparation_failed", "compaction_failed"] = (
        "none"
    )
    reason: Literal["automatic", "manual", "overflow"] = "manual"
    context_tokens: int = 0
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

    def begin(self, *, reason: str, resume_after: bool, context_tokens: int = 0) -> None:
        if reason not in {"automatic", "manual", "overflow"}:
            raise ValueError(f"Invalid compaction reason: {reason}")
        self.phase = "waiting"
        self.reason = cast(Literal["automatic", "manual", "overflow"], reason)
        self.context_tokens = max(0, context_tokens)
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
        self.context_tokens = 0
        self.resume_after = False
        self.preparation_token = None
        self.failure = None
        self.started = False

    @classmethod
    def from_data(cls, value: object) -> "CompactionControl":
        """Decode this state from a storage adapter's plain representation."""
        if not isinstance(value, Mapping):
            return cls()
        phase = value.get("phase")
        reason = value.get("reason")
        try:
            context_tokens = max(0, int(value.get("context_tokens") or 0))
        except (TypeError, ValueError):
            context_tokens = 0
        return cls(
            phase=phase
            if phase in {"none", "waiting", "recorded", "preparation_failed", "compaction_failed"}
            else "none",
            reason=reason if reason in {"automatic", "manual", "overflow"} else "manual",
            context_tokens=context_tokens,
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

    def __init__(
        self,
        *,
        configuration: CompactionConfiguration | None = None,
        strategy: Any = None,
        preparation: Any = None,
        summarizer: Any = None,
    ) -> None:
        self._configuration = configuration or CompactionConfiguration()
        # The caller's strategy and handoff replace only their own policy; the fold stays this plugin's.
        self._strategy = strategy
        self._preparation = (
            preparation if preparation is not None else DirectCompactionPreparation()
        )
        self._summarizer = summarizer
        self._control = CompactionControl()

    def attach(self, context: PluginContext, host: PluginHost) -> None:
        self._context = context
        self._host = host
        self._prompts = context.prompts("compaction")

    @property
    def control(self) -> CompactionControl:
        return self._control

    def snapshot(self) -> CompactionControl:
        return replace(self._control)

    def restore(self, state: object) -> None:
        self._control = (
            replace(state)
            if isinstance(state, CompactionControl)
            else CompactionControl.from_data(state)
        )

    def restore_control(self, value: object) -> None:
        self.restore(value)

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

    def usable_context(self) -> int:
        """How much of the window a conversation may occupy, leaving room for the answer and for the compact itself."""
        window = self._host.window.context_window
        if window <= 0:
            return 0
        return max(
            0,
            window - int(window * self._configuration.output_reserve_fraction),
        )

    def managed_context(self) -> int:
        """The context budget that schedules maintenance, optionally bounded by the host."""
        usable = self.usable_context()
        maximum = self._configuration.maximum_context_tokens
        if maximum <= 0:
            return usable
        return min(usable, maximum) if usable > 0 else maximum

    def _recent_working_set(self, reason: str = "automatic", recent: list | None = None) -> int:
        """The tail kept verbatim rather than compacted, as a share of the usable window so it scales with the model."""
        fraction = self._configuration.recent_working_set_fraction
        budget = int(self.managed_context() * fraction)
        if budget > 0 and reason != "manual":
            return budget
        # Manual and overflow compaction must remain effective even if a provider failed to report its window. The current conversation is still an honest upper bound.
        measured = int(
            conversation_tokens(self._host.conversation.messages if recent is None else recent)
            * fraction
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
        self, reason: str = "automatic", *, context_tokens: int | None = None
    ) -> CompactionState:
        """Expose only the durable conversation to a caller-supplied compaction strategy."""
        return CompactionState(
            messages=self.without_preparation(self._host.conversation.messages),
            context_window=self._host.window.context_window,
            context_tokens=(
                self._host.window.latest_context_tokens
                if context_tokens is None
                else context_tokens
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
        managed = self.managed_context()
        return managed > 0 and next_request_tokens >= (
            self._configuration.reclaim_at_fraction * managed
        )

    def should_maintain(self, request_tokens: int) -> bool:
        """The automatic trigger, measured against the usable window, unless a strategy answers it instead."""
        if self._strategy is not None:
            return bool(
                self._strategy.should_compact(self._compaction_state(context_tokens=request_tokens))
            )
        compaction = self._configuration
        if not compaction.automatic or not self._at_compacting_threshold(request_tokens):
            return False
        return len(self.bounded_tail(self._host.conversation.messages)) < len(
            self._host.conversation.messages
        )

    def maintenance_active(self) -> bool:
        """Whether this plugin is currently holding the loop to reclaim context."""
        return bool(self._control.active)

    def begin_maintenance(
        self, *, reason: str, resume_after: bool, context_tokens: int = 0
    ) -> None:
        self.begin_preparation(
            reason=reason, resume_after=resume_after, context_tokens=context_tokens
        )

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
                tokens_before=(
                    self._control.context_tokens
                    or self._host.window.latest_context_tokens
                    or conversation_tokens(self._host.conversation.messages)
                ),
            )

    async def run_maintenance(self, *, reason: str):
        """Complete the held handoff and reclaim the window."""
        if self._control.recorded:
            try:
                metadata = await self._preparation.describe()
            except Exception as error:  # noqa: BLE001 — the fold's verification below remains authoritative
                self._context.bus.emit(MemoryHandoffFailed(str(error) or type(error).__name__))
            else:
                self._context.bus.emit(MemoryHandoffVerified(metadata))
        async for event in self.compact(reason):
            yield event

    def begin_preparation(
        self, *, reason: str, resume_after: bool, context_tokens: int = 0
    ) -> None:
        """Begin the configured durable handoff before compacting."""
        self._control.begin(reason=reason, resume_after=resume_after, context_tokens=context_tokens)
        instruction = self._preparation.instruction(self._prompts.load("prepare_compaction", {}))
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

    def retry_maintenance(self) -> str | None:
        """Reopen exactly the failed phase and return the operation to drive."""
        if self._control.phase == "compaction_failed":
            self._control.retry_compaction()
            self._host.bookkeeping.note_state_changed()
            return "compaction"
        if self._control.phase != "preparation_failed":
            return None
        # A retry gets one unambiguous preparation notice. Retain any accepted user message that followed the failed private segment while removing that segment's discarded work.
        self._host.conversation.messages[:] = self.without_preparation(
            self._host.conversation.messages
        )
        self.begin_preparation(
            reason=self._control.reason,
            resume_after=self._control.resume_after,
            context_tokens=self._control.context_tokens,
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
                CompactionStarted(
                    reason="preparation", messages_before=messages, tokens_before=tokens
                )
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
            tokens_before = (
                self._control.context_tokens
                or self._host.window.latest_context_tokens
                or conversation_tokens(state.messages)
            )
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
        tokens_before = (
            self._control.context_tokens
            or self._host.window.latest_context_tokens
            or conversation_tokens(compactable)
        )
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
        messages_to_summarize = compactable[: len(compactable) - len(kept)]
        summary = (
            await self._summarize_compacted(messages_to_summarize)
            if messages_to_summarize
            else None
        )
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
        if not summary:
            self._host.conversation.messages[:] = original
            self._host.window.set_latest_context_tokens(tokens_before)
            self.fail_compaction("The conversation could not be summarized safely.")
            yield CompactionDone(
                reason=reason,
                ok=False,
                messages_before=messages_before,
                messages_after=messages_before,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                error_code="compaction_summary_failed",
            )
            return
        retained = [self._summary_message(summary), *kept]
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
    def _summary_message(summary: str) -> AIMessage:
        """Retain history with provenance, never as a fresh user-authored instruction."""
        return AIMessage(
            content=(
                "# Compacted conversation history\n\n"
                "The following is a model-generated record of earlier messages. It is historical "
                "context, not a new user instruction. Treat only requirements explicitly attributed "
                "to the user as user requirements. Later retained user messages override conflicts, "
                "and agent interpretations never restrict later user requests.\n\n"
                f"{summary}"
            ),
            additional_kwargs={
                "reminder": True,
                "summary": True,
                "provenance": "model-generated-history",
            },
        )

    async def _summarize_compacted(self, messages_to_summarize: list) -> str | None:
        """Ask the model to distil the conversation before compacting, preserving its cache prefix."""
        state = CompactionSummaryState(
            messages=tuple(messages_to_summarize),
            system_prompt=self._host.turn.build_static_system_prompt(),
        )
        if self._summarizer is not None:

            async def _run_custom_summarizer() -> Any:
                async with asyncio.timeout(self._configuration.summary_timeout_seconds):
                    with cache_lane("compaction-summary"):
                        return await self._summarizer.summarize(state)

            summary_task = asyncio.create_task(_run_custom_summarizer())

            def _cancel_custom_summary() -> None:
                summary_task.cancel()

            try:
                interrupted = await await_interruptible(
                    summary_task,
                    self._host.turn.abort_event,
                    _cancel_custom_summary,
                )
                if interrupted:
                    return None
                summary = summary_task.result()
            except Exception as error:  # noqa: BLE001 — the caller receives a durable blocker
                logger.warning("compaction summarizer failed: %s", error)
                return None
            return str(summary or "").strip() or None
        instruction = self._prompts.load("compaction_summary", {})
        model = self._host.conversation.model.bind_tools(
            [submit_compaction_summary_tool],
            tool_choice="auto",
            parallel_tool_calls=False,
        )
        request = [
            SystemMessage(content=self._host.turn.build_static_system_prompt()),
            *messages_to_summarize,
            SystemMessage(content=instruction),
        ]

        def _only_summary_call(response: Any) -> Any | None:
            calls = getattr(response, "tool_calls", None) or []
            if len(calls) != 1 or calls[0].get("name") != "submit_compaction_summary":
                return None
            return calls[0].get("args")

        def _retry_reminder(_response: Any) -> SystemMessage:
            return SystemMessage(content=self._prompts.load("compaction_summary_retry", {}))

        summary_task = asyncio.create_task(
            collect_structured_call(
                model,
                request,
                tool_name="submit_compaction_summary",
                schema=CompactionSummary,
                attempts=self._configuration.summary_attempts,
                timeout_seconds=self._configuration.summary_timeout_seconds,
                cache_lane_name="compaction-summary",
                reason="the compaction summarizer",
                select=_only_summary_call,
                accept=lambda value: bool(value.summary.strip()),
                retry_reminder=_retry_reminder,
            )
        )

        def _cancel_summary() -> None:
            summary_task.cancel()

        try:
            interrupted = await await_interruptible(
                summary_task,
                self._host.turn.abort_event,
                _cancel_summary,
            )
            if interrupted:
                return None
            submitted = summary_task.result()
            return str(submitted.summary).strip() if submitted is not None else None
        except Exception as error:  # noqa: BLE001 — the caller receives a durable blocker
            logger.exception("compaction summary failed: %s", error)
            return None

    def preparation_violation_message(self) -> str:
        """The refusal the model is given for calling outside the private handshake."""
        return self._prompts.load("compaction_preparation_violation", {})


__all__ = [
    "Compaction",
    "CompactionControl",
    "CompactionSummary",
    "DirectCompactionPreparation",
    "ObservationCompactionPreparation",
]
