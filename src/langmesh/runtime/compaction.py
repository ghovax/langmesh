"""The runtime's compaction concern: when and how to fold a conversation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from itertools import accumulate, takewhile
from typing import AsyncIterator, Literal

from langmesh.base.message_content import forget_carried_reasoning
from langmesh.runtime.internals import (
    conversation_tokens,
    message_tokens,
)
from langmesh.runtime.turn_events import CompactionDone, CompactionStarted, TurnEvent
from langmesh.base.ports import CompactionState
from langchain_core.messages import HumanMessage, ToolMessage
from langmesh.base.errors import log_fields


logger = logging.getLogger(__name__)


@dataclass
class _CompactionControl:
    """The fold handshake as one state value, so invalid flag combinations cannot accumulate."""

    phase: Literal["none", "waiting", "recorded", "preparation_failed", "fold_failed"] = "none"
    reason: Literal["auto", "manual", "overflow"] = "manual"
    resume_after: bool = False
    registry_revision: int | None = None
    failure: str | None = None
    # Whether the running compaction indicator has already been announced for this preparation.
    started: bool = False

    def begin(self, *, reason: str, resume_after: bool) -> None:
        if reason not in {"auto", "manual", "overflow"}:
            raise ValueError(f"Invalid compaction reason: {reason}")
        self.phase = "waiting"
        self.reason = reason
        self.resume_after = resume_after
        self.registry_revision = None
        self.failure = None
        self.started = False

    def record(self) -> None:
        if self.phase != "waiting":
            raise RuntimeError(f"Cannot record compaction preparation from {self.phase}.")
        self.phase = "recorded"

    def fail_preparation(self, message: str) -> None:
        self.phase = "preparation_failed"
        self.failure = message

    def fail_fold(self, message: str) -> None:
        self.phase = "fold_failed"
        self.failure = message

    def retry_fold(self) -> None:
        if self.phase != "fold_failed":
            raise RuntimeError(f"Cannot retry a fold from {self.phase}.")
        self.phase = "recorded"
        self.failure = None

    def clear(self) -> None:
        self.phase = "none"
        self.reason = "manual"
        self.resume_after = False
        self.registry_revision = None
        self.failure = None
        self.started = False

    def snapshot(self) -> dict:
        return {
            "phase": self.phase,
            "reason": self.reason,
            "resume_after": self.resume_after,
            "registry_revision": self.registry_revision,
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
            if phase in {"none", "waiting", "recorded", "preparation_failed", "fold_failed"}
            else "none",
            reason=reason if reason in {"auto", "manual", "overflow"} else "manual",
            resume_after=bool(value.get("resume_after", False)),
            registry_revision=(
                max(0, int(value["registry_revision"]))
                if value.get("registry_revision") is not None
                else None
            ),
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
        """How much of the window a conversation may occupy, leaving room for the answer and for the fold itself."""
        window = self._context_window
        if window <= 0:
            return 0
        return max(
            0, window - int(window * self._global_configuration.compaction.output_reserve_fraction)
        )

    def _recent_working_set(self, reason: str = "automatic", recent: list | None = None) -> int:
        """The tail kept verbatim rather than folded, as a share of the usable window so it scales with the model."""
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

    def _at_folding_threshold(self, pending_message=None) -> bool:
        """Whether the next request is large enough that folding is worth its cache invalidation."""
        usable = self._usable_context()
        current = self._next_request_tokens(pending_message)
        return usable > 0 and max(self._latest_context_tokens, current) >= (
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
        if not compaction.automatic or not self._at_folding_threshold(pending_message):
            return False
        return len(self._bounded_tail(self._conversation)) < len(self._conversation)

    def _begin_compaction_preparation(self, *, reason: str, resume_after: bool) -> None:
        """Append the one private handoff that asks the agent to preserve durable work before folding."""
        self._conversation.append(
            self._reminder_message(
                self._prompt_loader.load("prepare_compaction", {}),
                marks={"compaction_preparation": True},
            )
        )
        self._compaction_control.begin(reason=reason, resume_after=resume_after)
        self._mark_session_dirty()

    def _fail_compaction_preparation(
        self, error: str, *, error_code: str = "compaction_preparation_failed"
    ) -> list[TurnEvent]:
        """Make an incomplete recording handshake the same durable, visible blocker as a failed fold."""
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
        if self._compaction_control.phase != "recorded":
            for event in self._fail_compaction_preparation(
                "Compaction requires a successful observational-memory checkpoint segment first."
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
        # The explicit handoff has committed. The fold itself only changes conversation state.
        original = list(self._conversation)
        foldable = self._without_compaction_preparation(original)
        messages_before = len(foldable)
        tokens_before = self._latest_context_tokens
        yield CompactionStarted(
            reason=reason, messages_before=messages_before, tokens_before=tokens_before
        )
        kept = self._bounded_tail(foldable, reason)
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
                    self._conversation[:] = foldable
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
        try:
            self._conversation[:] = _without_provider_reasoning(kept)
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


__all__ = ["KeepRecentTurns"]
