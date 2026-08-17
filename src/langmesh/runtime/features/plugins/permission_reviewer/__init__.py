"""The automatic permission reviewer: the model-based evaluator that decides automatic gates.

Whether a call runs, is asked about, or is refused is the permissions plugin's concern. This
plugin is the one piece that calls a model for a verdict: given a gate the boundary did not
settle by rule, it asks the session's own model to allow or deny, and returns the typed
decision. The permissions plugin holds the boundary and calls this reviewer through a port;
the runtime can also reach it directly through the seam's ``review_automatic_gate``.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from langmesh.base.content.model_errors import ContextWindowExceeded
from langmesh.base.content.instructions import instructions_payload
from langmesh.base.primitives.serialization import compact
from langmesh.base.primitives.tuning import Tunable, active_tuning
from langmesh.runtime.features import Feature, PluginContext, PluginHost
from langmesh.runtime.features.plugins.permission_reviewer.tools import (
    permission_decision as permission_decision_tool,
)
from langmesh.runtime.internals import _PreflightGate
from langmesh.runtime.locations import PermissionDecision

logger = logging.getLogger(__name__)


class PermissionReviewer(Feature):
    """The model verdict for one automatic gate, and the evidence recorded about each review."""

    def attach(self, context: PluginContext, host: PluginHost) -> None:
        self._context = context
        self._host = host
        self._prompts = context.prompts("permission_reviewer")

    def contribute_tools(self) -> list:
        """The reviewer's own verdict tool."""
        from langmesh.runtime.features.plugins.permission_reviewer.tools import (
            permission_decision,
        )

        return [permission_decision]

    async def review(self, gate: _PreflightGate) -> PermissionDecision:
        """The reviewer's verdict on one gate. Takes a gate, so it cannot reach a call that raised none."""
        # The person's standing instructions, so the reviewer can judge a request against what they asked for.
        context = compact(
            {
                "tool": gate.tool_name,
                "working_directory": self._context.working_directory,
                "command": gate.command,
                "arguments": gate.arguments,
                "requested_access": {
                    "reads": list(gate.escape.reads),
                    "writes": list(gate.escape.writes),
                    "network": gate.escape.network,
                    "whole_disk": gate.whole_disk,
                },
                "model_explanation": gate.arguments.get("explanation", "") or gate.explanation,
                # The person's standing instructions, so the reviewer can tell user-requested reach from invention.
                "user_instructions": instructions_payload(self._context.catalogue.instructions()),
                "confinement": self._host.boundary.sandbox.describe(
                    workspace=self._context.working_directory
                ),
                # Only on a second run, so the reviewer knows the command hit a wall rather than merely failed.
                **({"denial_evidence": gate.denial_evidence} if gate.denial_evidence else {}),
                "allowed_actions": ["allow", "deny"],
            }
        )
        prompt = self._prompts.load(
            "permission_reviewer",
            {
                "thinking_language": self._prompts.load("thinking_language", {}).strip(),
                "toolbox": (
                    self._prompts.load("reviewer_toolbox", {})
                    if getattr(self._host.tools.tool_context, "toolbox", None) is not None
                    else ""
                ),
            },
        )
        # Preserve the exact main-session prefix and invariant tool schema for provider-cache reuse.
        request = [
            SystemMessage(content=self._host.turn.build_static_system_prompt()),
            *self._conversation_for_review(),
            SystemMessage(content=prompt),
            HumanMessage(content=context),
        ]
        try:
            self._host.turn.refuse_if_over_window(request)
        except ContextWindowExceeded:
            logger.warning(
                "the permission reviewer could not fit the session in the window; denying"
            )
            return PermissionDecision(
                action="deny",
                explanation="The conversation is too large to review safely, so this request was refused.",
                risk="medium",
            )
        # The reviewer is one verdict call: bind only its verdict tool, not the session's surface.
        model = self._host.conversation.model.bind_tools(
            [permission_decision_tool],
            tool_choice="auto",
            parallel_tool_calls=False,
        )
        attempts = active_tuning().amount(Tunable.permission_reviewer_attempts)
        started_at = time.perf_counter()
        from langmesh.runtime.verdict import collect_structured_call

        def _only_permission_call(response: Any) -> Any | None:
            calls = getattr(response, "tool_calls", None) or []
            if len(calls) != 1 or calls[0].get("name") != "permission_decision":
                return None
            return calls[0].get("args")

        decision = await collect_structured_call(
            model,
            request,
            tool_name="permission_decision",
            schema=PermissionDecision,
            attempts=attempts,
            cache_lane_name=f"permission-review/{gate.request_id}",
            reason=f"the permission reviewer for {gate.request_id}",
            select=_only_permission_call,
            # A verdict with no reason cannot be acted on. A failed attempt, not a refusal — the next may supply it.
            accept=lambda decision: bool(decision.explanation.strip()),
            on_success=lambda decision, response, attempt: self._record_review(
                decision, response=response, attempts=attempt, started_at=started_at
            ),
        )
        if decision is not None:
            return decision
        logger.warning("the permission reviewer did not decide in %d attempts; denying", attempts)
        decision = PermissionDecision(
            action="deny",
            explanation="The safety check could not run, so this request was refused.",
            risk="medium",
        )
        self._record_review(
            decision,
            response=None,
            attempts=attempts,
            started_at=started_at,
        )
        return decision

    def _record_review(
        self,
        decision: PermissionDecision,
        *,
        response: Any,
        attempts: int,
        started_at: float,
    ) -> None:
        """Record enough evidence to verify review latency, caching, and decision emission."""
        usage = getattr(response, "usage_metadata", None) or {}
        input_details = usage.get("input_token_details") or {}
        cache_trace = (
            getattr(response, "additional_kwargs", {}).get("cache_trace") or {}
            if response is not None
            else {}
        )
        metrics = {
            "action": decision.action,
            "reason": decision.explanation,
            "risk": decision.risk,
            "attempts": attempts,
            "duration_milliseconds": round((time.perf_counter() - started_at) * 1000),
            "decision_tool_calls": len(getattr(response, "tool_calls", None) or []),
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
            "cache_read_tokens": int(input_details.get("cache_read", 0) or 0),
            "prefix_intact": bool(cache_trace.get("prefix_intact", False)),
            "reachable_tokens": int(cache_trace.get("reachable_tokens", 0) or 0),
            "shared_segments": int(cache_trace.get("shared_segments", 0) or 0),
            "segments": int(cache_trace.get("segments", 0) or 0),
        }
        self._host.bookkeeping.record_event("permission_reviewed", metrics)
        logger.info(
            "permission review session=%s action=%s attempts=%d duration_ms=%d tool_calls=%d "
            "input_tokens=%d output_tokens=%d cache_read_tokens=%d prefix_intact=%s "
            "reachable_tokens=%d shared_segments=%d segments=%d",
            self._context.session_id,
            metrics["action"],
            metrics["attempts"],
            metrics["duration_milliseconds"],
            metrics["decision_tool_calls"],
            metrics["input_tokens"],
            metrics["output_tokens"],
            metrics["cache_read_tokens"],
            metrics["prefix_intact"],
            metrics["reachable_tokens"],
            metrics["shared_segments"],
            metrics["segments"],
        )

    def _conversation_for_review(self) -> list[Any]:
        """The conversation as the reviewer may see it: the pending tool call closed by a
        placeholder, since a proposal with no response is invalid to the provider."""
        conversation = list(self._host.conversation.messages)
        last = conversation[-1] if conversation else None
        if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
            for tool_call in last.tool_calls:
                conversation.append(
                    ToolMessage(
                        content=compact({"status": "pending_review"}),
                        tool_call_id=tool_call["id"],
                    )
                )
        return conversation

    def denied_message(self, reason: str) -> str:
        """The model-facing note when the reviewer denied a call."""
        return self._prompts.load(
            "reviewer_denied",
            {"reason": reason or "The permission reviewer did not produce an approval."},
        )

    async def review_automatic_gate(self, gate) -> Any | None:
        """The verdict for one automatic gate, announced before it runs."""
        from langmesh.base import confinement
        from langmesh.runtime.values import PermissionAnswer

        decision = await self.review(gate)
        if decision.action == "allow":
            gate.approved_by = confinement.APPROVED_BY_PERMISSION_REVIEWER
            self._host.bookkeeping.record_event(
                "access_allowed",
                {
                    "tool": gate.tool_name,
                    "reason": decision.explanation,
                    "risk": decision.risk,
                },
            )
            return PermissionAnswer(
                allow=True,
                reason=decision.explanation,
                actor="reviewer",
            )
        self._host.bookkeeping.record_event(
            "access_refused",
            {
                "tool": gate.tool_name,
                "reason": decision.explanation,
                "risk": decision.risk,
            },
        )
        return PermissionAnswer(
            allow=False,
            reason=decision.explanation or "The permission reviewer did not produce an approval.",
            actor="reviewer",
        )


__all__ = ["PermissionReviewer"]
