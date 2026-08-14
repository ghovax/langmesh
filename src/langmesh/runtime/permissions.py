"""Carrying out the boundary's verdict: raising gates, reviewing them, recording grants, offering retries."""

from __future__ import annotations

from langmesh.runtime.internals import (
    _coerce_structured_arguments,
    _PreflightGate,
    _ResolvedToolDecision,
    _ToolPlan,
)
from langmesh.base import confinement
from langmesh.base.confinement import Grant, parse_access_request
from langmesh.runtime.values import PermissionAnswer, PermissionReason
from langmesh.runtime.boundary import RULE_ALLOW, RULE_ASK, escape_of, verdict_for
from langmesh.runtime.locations import (
    _LOCATION_TOOLS,
    PermissionDecision,
    ResolvedLocation,
    ToolLocationError,
)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langmesh.base.model_errors import ContextWindowExceeded
from langmesh.base.instructions import instructions_payload
from typing import Any, Optional
import ast
import logging
import time
import uuid
from langmesh.base.serialization import compact
from langmesh.base.tuning import Tunable, active_tuning
from langmesh.runtime.cache_trace import cache_lane

logger = logging.getLogger(__name__)

# The screen primitives that change something. The set the gate is about, and the one the bridge sends.
MUTATING_SCREEN_PRIMITIVES = frozenset(
    {
        "click",
        "type",
        "choose",
        "upload",
        "drag",
        "evaluate",
        "press",
        "navigate",
        "new_tab",
        "close_tab",
        "caret",
        "select",
    }
)


def _screen_primitive(func: ast.expr) -> str:
    """The primitive a call node names, bare or through ``screen``."""
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _screen_mutations(script: str) -> tuple[str, ...]:
    """The state-changing primitives a script calls. Decides who is asked, never what is available."""
    try:
        tree = ast.parse(script)
    except SyntaxError:
        # Unparseable: the tool will fail to run it, so there is nothing to name.
        return ()
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _screen_primitive(node.func)
            if name in MUTATING_SCREEN_PRIMITIVES and name not in found:
                found.append(name)
    return tuple(found)


class _DecidesPermissions:
    """Whether a call runs, is asked about, or is refused."""

    async def _review(self, gate: _PreflightGate) -> PermissionDecision:
        """The reviewer's verdict on one gate. Takes a gate, so it cannot reach a call that raised none."""
        # The person's standing instructions, so the reviewer can judge a request against what they asked for.
        context = compact(
            {
                "tool": gate.tool_name,
                "working_directory": self._working_directory,
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
                "user_instructions": instructions_payload(self._catalogue.instructions()),
                "confinement": self._sandbox.describe(workspace=self._working_directory),
                # Only on a second run, so the reviewer knows the command hit a wall rather than merely failed.
                **({"denial_evidence": gate.denial_evidence} if gate.denial_evidence else {}),
                "allowed_actions": ["allow", "deny"],
            }
        )
        prompt = self._prompt_loader.load(
            "permission_reviewer",
            {
                "thinking_language": self._prompt_loader.load("thinking_language", {}).strip(),
                "toolbox": (
                    self._prompt_loader.load("reviewer_toolbox", {})
                    if getattr(self._tool_context, "toolbox", None) is not None
                    else ""
                ),
            },
        )
        # Preserve the exact main-session prefix and invariant tool schema for provider-cache reuse.
        request = [
            SystemMessage(content=self._build_static_system_prompt()),
            *self._conversation_for_review(),
            SystemMessage(content=prompt),
            HumanMessage(content=context),
        ]
        try:
            self._refuse_if_over_window(request)
        except ContextWindowExceeded:
            logger.warning(
                "the permission reviewer could not fit the session in the window; denying"
            )
            return PermissionDecision(
                action="deny",
                explanation="The conversation is too large to review safely, so this request was refused.",
                risk="medium",
            )
        model = self._model.bind_tools(
            self._model_tools,
            tool_choice="auto",
            parallel_tool_calls=False,
        )
        attempts = active_tuning().amount(Tunable.permission_reviewer_attempts)
        started_at = time.perf_counter()
        with cache_lane(f"permission-review/{gate.request_id}"):
            for attempt in range(1, attempts + 1):
                try:
                    response = await model.ainvoke(request)
                except Exception:  # noqa: BLE001 — one dropped call is not a verdict
                    logger.warning(
                        "the permission reviewer could not be reached (attempt %d of %d)",
                        attempt,
                        attempts,
                        exc_info=True,
                    )
                    continue
                if len(response.tool_calls) != 1:
                    logger.warning(
                        "the permission reviewer returned %d decisions (attempt %d of %d)",
                        len(response.tool_calls),
                        attempt,
                        attempts,
                    )
                    continue
                if response.tool_calls[0].get("name") != "permission_decision":
                    logger.warning(
                        "the permission reviewer called %s instead of %s (attempt %d of %d)",
                        response.tool_calls[0].get("name"),
                        "permission_decision",
                        attempt,
                        attempts,
                    )
                    continue
                try:
                    decision = PermissionDecision.model_validate(response.tool_calls[0]["args"])
                except Exception:  # noqa: BLE001 — a malformed verdict is not a verdict either
                    logger.warning(
                        "the permission reviewer returned a malformed decision (attempt %d of %d)",
                        attempt,
                        attempts,
                        exc_info=True,
                    )
                    continue
                # A verdict with no reason cannot be acted on. A failed attempt, not a refusal — the next may supply it.
                if not decision.explanation.strip():
                    logger.warning(
                        "the permission reviewer gave no reason for its decision (attempt %d of %d)",
                        attempt,
                        attempts,
                    )
                    continue
                self._record_permission_review(
                    decision,
                    response=response,
                    attempts=attempt,
                    started_at=started_at,
                )
                return decision
        logger.warning("the permission reviewer did not decide in %d attempts; denying", attempts)
        decision = PermissionDecision(
            action="deny",
            explanation="The safety check could not run, so this request was refused.",
            risk="medium",
        )
        self._record_permission_review(
            decision,
            response=None,
            attempts=attempts,
            started_at=started_at,
        )
        return decision

    def _record_permission_review(
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
        self._record_event("permission_reviewed", metrics)
        logger.info(
            "permission review session=%s action=%s attempts=%d duration_ms=%d tool_calls=%d "
            "input_tokens=%d output_tokens=%d cache_read_tokens=%d prefix_intact=%s "
            "reachable_tokens=%d shared_segments=%d segments=%d",
            self._session_id,
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
        conversation = list(self._conversation)
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

    def _record_grant(self, grant: Grant) -> None:
        """Remember an approved widening for the session, so one grant is not re-asked on every command."""
        self._access_grants.append(grant)
        self._record_event(
            "access_granted",
            {
                "reads": list(grant.reads),
                "writes": list(grant.writes),
                "network": grant.network,
                "whole_disk": grant.whole_disk,
                "purpose": grant.purpose,
                "approved_by": grant.approved_by,
            },
        )

    def _granted_profile(self):
        """The session's confinement with every standing grant compacted in. What an escape is measured against."""
        profile = self._sandbox
        for grant in self._access_grants:
            profile = profile.with_grant(grant, workspace=self._working_directory or "")
        return profile

    def _new_permission_request_id(self, tool_call_id: str = "") -> str:
        """The id an answer is filed under, derived from the call so a replanned gate is the same gate."""
        return f"perm-{self._session_id}-{tool_call_id or uuid.uuid4()}"

    def _new_question_request_id(self, tool_call_id: str = "") -> str:
        """Stable for the same reason, and by the same means, as the permission id above."""
        return f"q-{self._session_id}-{tool_call_id or uuid.uuid4()}"

    def _new_retry_request_id(self, tool_call_id: str) -> str:
        """The id for a second run, distinct from the preflight id since both can exist in one turn."""
        return f"retry-{self._session_id}-{tool_call_id}"

    async def _preflight_permissions(
        self, tool_calls: list[dict]
    ) -> tuple[dict[str, _ToolPlan], list[_PreflightGate]]:
        """Every call's verdict, resolved before any tool runs, so a pause can be checkpointed durably."""
        plans: dict[str, _ToolPlan] = {}
        pending: list[_PreflightGate] = []
        for tool_call_data in tool_calls:
            plan = await self._plan_call(
                tool_call_data["name"],
                tool_call_data["args"],
                tool_call_data["id"],
            )
            # A gate is raised before its call is announced, so it carries what is being asked for.
            for gate in plan.gates:
                gate.tool_name = tool_call_data["name"]
                gate.arguments = dict(tool_call_data["args"] or {})
            plans[tool_call_data["id"]] = plan
            pending.extend(plan.gates)
        return plans, pending

    async def _plan_call(
        self,
        tool_name: str,
        tool_arguments: dict,
        tool_call_identifier: str,
    ) -> _ToolPlan:
        """The verdict for one call. One path for every tool; only the rule table and the escape differ."""
        plan = _ToolPlan(tool_call_id=tool_call_identifier)
        # Keep verdict schemas cache-stable but inert outside their dedicated review capability.
        if tool_name == "permission_decision" or (
            tool_name == "submit_goal_review" and not self._accepts_goal_review
        ):
            return plan
        if self._compaction_control.phase == "waiting" and tool_name in {"bash", "load_skill"}:
            # The handoff protocol itself: local foreground Bash and read-only skill loading; the turn loop rejects every other shape.
            return plan
        schema = self._tool_schemas.get(tool_name)
        if schema is not None:
            tool_arguments = _coerce_structured_arguments(schema, tool_arguments)

        resolved_location: ResolvedLocation | None = None
        if tool_name in _LOCATION_TOOLS:
            tool_arguments = dict(tool_arguments)
            location_value = tool_arguments.pop("location", None) or None
            try:
                resolved_location = self._resolve_location(location_value)
            except ToolLocationError:
                # A bad location is an execution error, raised by _execute_tool rather than decided here.
                return plan
        policy = self._call_policy(resolved_location)
        explanation = str(tool_arguments.get("explanation", "") or "")

        # ask_user is the one call that is a question rather than an act.
        if tool_name == "ask_user":
            # The second lock: the tool is already withheld under `automatic`, but a stale plan could still name it.
            if not policy.asks:
                plan.refusal = self._refusal(self._prompt_loader.load("nobody_to_ask", {}))
                return plan
            plan.gates.append(
                _PreflightGate(
                    request_id=self._new_question_request_id(tool_call_identifier),
                    tool_call_id=tool_call_identifier,
                    kind="question",
                    questions=tool_arguments.get("questions", []) or [],
                )
            )
            return plan

        subject, rule = self._rule_for(tool_name, tool_arguments)
        # A remote call has no box here to escape, so the rules are the whole policy.
        profile = None if policy.is_remote else self._granted_profile()
        request, _ = parse_access_request(tool_arguments.get("access_request"))
        escape = escape_of(request, profile, workspace=policy.working_directory)

        # A screen script's box is the primitives it was handed, so escaping it means changing something.
        mutations: tuple[str, ...] = ()
        if tool_name == "control_screen":
            mutations = _screen_mutations(str(tool_arguments.get("script", "") or ""))
            if mutations and rule == RULE_ALLOW:
                plan.screen_mutations = True

        verdict = verdict_for(
            escape=escape,
            rule=rule,
            profile=profile,
            workspace=policy.working_directory,
        )
        if verdict.kind == "refuse":
            plan.refusal = self._refusal(
                self._hard_refusal_message(verdict.reason),
                reason=verdict.reason,
                subject=subject,
            )
            return plan
        needs_screen_gate = bool(mutations) and rule != RULE_ALLOW
        if verdict.runs and not needs_screen_gate:
            return plan

        gate = _PreflightGate(
            request_id=self._new_permission_request_id(tool_call_identifier),
            tool_call_id=tool_call_identifier,
            kind="permission",
            command=self._command_of(tool_name, tool_arguments) or subject,
            explanation=self._escape_explanation(escape, explanation) if escape else explanation,
            reason=verdict.reason
            or (
                PermissionReason(kind="screen_mutation", paths=list(mutations))
                if mutations
                else PermissionReason(kind="rule_review")
            ),
            escape=escape,
            grants_screen_mutations=bool(mutations),
            is_bash=(tool_name == "bash"),
            deny_message=self._deny_message(tool_name),
        )
        # Under `automatic` the reviewer decides, but the gate is still announced so the call is
        # visible while it is weighed; the review itself runs in the turn batch, after the event.
        if not policy.asks:
            gate.automatic_review = True
        plan.gates.append(gate)
        return plan

    async def _review_auto_gate(self, gate: _PreflightGate) -> PermissionAnswer:
        """Review one automatic gate and return the typed answer the shared resolver consumes."""
        decision = await self._review(gate)
        if decision.action == "allow":
            gate.approved_by = confinement.APPROVED_BY_REVIEWER
            self._record_event(
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
        self._record_event(
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

    def _denial_subject(self, tool_name: str) -> str:
        """The concrete operation named in a model-facing permission denial."""
        if tool_name == "bash":
            return "command"
        if tool_name == "call_mcp_server_tool":
            return "MCP server tool call"
        if tool_name == "control_screen":
            return "screen action"
        return f"{tool_name} call"

    def _escape_explanation(self, escape, explanation: str) -> str:
        """Render requested reach from structured values instead of assembling prose in Python."""
        return self._prompt_loader.load(
            "permission_access_request",
            {
                "reads": compact(list(escape.reads)),
                "writes": compact(list(escape.writes)),
                "network": str(bool(escape.network)).lower(),
                "explanation": explanation or "No explanation was provided.",
            },
        )

    def _hard_refusal_message(self, reason: Optional[PermissionReason]) -> str:
        """Render a configuration refusal from its structured reason."""
        if reason is not None and reason.kind == "path_denial":
            return self._prompt_loader.load(
                "permission_path_denied",
                {"paths": compact(reason.paths)},
            )
        return self._prompt_loader.load("permission_rule_denied", {})

    def _resolved_denial_message(self, gate: _PreflightGate, answer: PermissionAnswer) -> str:
        """Render one denial without losing who decided it or the reason they supplied."""
        if answer.actor == "reviewer":
            return self._prompt_loader.load(
                "reviewer_denied",
                {"reason": answer.reason or "The permission reviewer did not produce an approval."},
            )
        return self._prompt_loader.load(
            "permission_person_denied"
            if answer.actor == "person"
            else "permission_approver_denied",
            {
                "subject": self._denial_subject(gate.tool_name),
                "reason": answer.reason or "No additional reason was provided.",
            },
        )

    def _retry_refusal_result(
        self,
        gate: _PreflightGate,
        *,
        actor: str = "reviewer",
        reason: str = "",
    ) -> dict:
        """Preserve the confined attempt while making the declined broader retry explicit."""
        return {
            "code": "sandbox_retry_declined",
            "status": "error",
            "decision": {
                "actor": actor,
                "reason": reason or None,
            },
            "confined_attempt": gate.refused_result,
        }

    def _rule_for(self, tool_name: str, tool_arguments: dict) -> tuple[str, str]:
        """What the configuration says about this call. The subject differs by tool, because the calls do."""
        tools = self._agent_configuration.tools
        if tool_name == "bash":
            command = str(tool_arguments.get("command", "") or "")
            return command, tools.bash.evaluate_permission(command, unmatched=RULE_ALLOW)
        if tool_name == "call_mcp_server_tool":
            subject = f"{tool_arguments.get('server', '')}.{tool_arguments.get('tool_name', '')}"
            return subject, tools.mcp.decide(subject, unmatched=RULE_ASK)
        if tool_name == "control_screen":
            mutations = _screen_mutations(str(tool_arguments.get("script", "") or ""))
            subject = mutations[0] if mutations else "read"
            return subject, tools.screen.decide(
                subject, unmatched=RULE_ASK if mutations else RULE_ALLOW
            )
        if tool_name in self._extra_tools:
            # A supplied tool is unknown to the engine, so it is asked about unless the caller said otherwise.
            return tool_name, RULE_ALLOW if self._supplied_tool_gate == "none" else RULE_ASK
        return tool_name, RULE_ALLOW

    def _command_of(self, tool_name: str, tool_arguments: dict) -> str:
        """What the person deciding is shown as *the thing being done*."""
        if tool_name == "bash":
            return str(tool_arguments.get("command", "") or "")
        if tool_name == "call_mcp_server_tool":
            return f"MCP server {tool_arguments.get('server', '')}.{tool_arguments.get('tool_name', '')}"
        return tool_name

    def _deny_message(self, tool_name: str) -> str:
        """The model-facing explanation when the person answers a gate no."""
        return self._prompt_loader.load(
            "permission_person_denied",
            {
                "subject": self._denial_subject(tool_name),
                "reason": "No additional reason was provided.",
            },
        )

    def _refusal(
        self,
        message: str,
        *,
        reason: Optional[PermissionReason] = None,
        subject: str = "",
    ) -> dict:
        """A hard refusal, in the shape the dispatcher surfaces."""
        return {
            "code": "",
            "message": message,
            "denied_injection": bool(subject),
            "raw_command": subject,
            "reason": reason.model_dump() if reason is not None else None,
            "decision": {
                "actor": "configuration" if reason is not None else "session",
                "reason": reason.model_dump() if reason is not None else None,
            },
        }

    def _approve(self, gate: _PreflightGate, *, by: str, plan: Optional[_ToolPlan] = None) -> None:
        """Carry out what approving a gate means, in one place, since it can grant two different things."""
        if gate.escape or gate.whole_disk:
            self._record_grant(
                confinement.approved(
                    confinement.AccessRequest(
                        mutates=True,
                        reads=gate.escape.reads,
                        writes=gate.escape.writes,
                        network=gate.escape.network,
                    ),
                    by=by,
                    purpose=gate.arguments.get("explanation", "") or gate.explanation,
                    whole_disk=gate.whole_disk,
                )
            )
        if gate.grants_screen_mutations and plan is not None:
            plan.screen_mutations = True

    def _resolve_tool_decisions(
        self, plans: dict[str, _ToolPlan], answers: dict[str, Any]
    ) -> dict[str, _ResolvedToolDecision]:
        """Plans plus answers into one verdict per tool. A tool runs only if every gate it raised was approved."""
        decisions: dict[str, _ResolvedToolDecision] = {}
        for tool_call_id, plan in plans.items():
            decision = _ResolvedToolDecision(tool_call_id=tool_call_id)
            decision.screen_mutations = plan.screen_mutations
            decision.retry_grant = plan.retry_grant
            if plan.refusal is not None:
                decision.approved = False
                decision.denial = plan.refusal
                decisions[tool_call_id] = decision
                continue
            for gate in plan.gates:
                answer = answers.get(gate.request_id)
                if gate.kind == "question":
                    # ask_user: the answers list, or the decline sentinel from the resolver.
                    decision.answers = answer
                    continue
                try:
                    permission_answer = PermissionAnswer.model_validate(answer)
                except Exception:  # noqa: BLE001 — invalid or absent input is never permission
                    permission_answer = None
                if permission_answer is None or not permission_answer.allow:
                    denial_message = (
                        self._resolved_denial_message(gate, permission_answer)
                        if permission_answer is not None
                        else self._prompt_loader.load("permission_invalid_answer", {})
                    )
                    if gate.refused_result is not None:
                        decision.completed = {
                            "result": self._retry_refusal_result(
                                gate,
                                actor=permission_answer.actor if permission_answer else "gate",
                                reason=permission_answer.reason if permission_answer else "",
                            ),
                            "model_guidance": denial_message,
                        }
                        break
                    decision.approved = False
                    decision.denial = {
                        "code": "",
                        "message": denial_message,
                        "denied_injection": False,
                        "raw_command": gate.command,
                        "reason": None,
                        "decision": {
                            "actor": permission_answer.actor if permission_answer else "gate",
                            "reason": permission_answer.reason if permission_answer else None,
                        },
                    }
                    break
                # Recorded here, the only point that knows somebody said yes; preflight alone grants nothing.
                approved_by = {
                    "person": confinement.APPROVED_BY_PERSON,
                    "reviewer": confinement.APPROVED_BY_REVIEWER,
                    "approver": confinement.APPROVED_BY_APPROVER,
                }[permission_answer.actor]
                self._approve(
                    gate,
                    by=gate.approved_by or approved_by,
                )
                if gate.grants_screen_mutations:
                    decision.screen_mutations = True
                if gate.whole_disk:
                    decision.retry_grant = confinement.approved(
                        by=gate.approved_by or approved_by,
                        purpose=gate.arguments.get("explanation", "") or gate.explanation,
                        whole_disk=True,
                    )
            decisions[tool_call_id] = decision
        return decisions

    def retry_gate(
        self,
        *,
        tool_call_id: str,
        command: str,
        denial: confinement.Denial,
        explanation: str,
    ) -> _PreflightGate:
        """The gate a refused command raises. The offer is whole-disk because no backend reports the path."""
        return _PreflightGate(
            request_id=self._new_retry_request_id(tool_call_id),
            tool_call_id=tool_call_id,
            kind="permission",
            command=command,
            explanation=explanation,
            is_bash=True,
            whole_disk=True,
            reason=PermissionReason(kind="confinement_refusal", paths=[denial.kind]),
            denial_evidence=denial.evidence,
            deny_message=self._prompt_loader.load("permission_retry_person_denied", {}),
        )

    async def reconsider_gate(self, gate) -> dict[str, Any]:
        """Re-decide a gate the session is parked on, under the mode now in force."""
        if self._call_policy(None).asks:
            return {}
        if gate.kind == "question":
            return {
                "declined": True,
                "actor": "session",
                "reason": self._prompt_loader.load("question_unavailable_reason", {}),
            }
        decision = await self._review(
            _PreflightGate.from_dict(gate.to_dict() if hasattr(gate, "to_dict") else vars(gate))
        )
        return {
            "decision": decision.action,
            "reason": decision.explanation,
            "actor": "reviewer",
        }

    async def decide_retry(self, gate: _PreflightGate) -> tuple[str, Optional[Grant]]:
        """What to do with a retry gate: ask, run with a grant, or refuse. Three answers, not an optional grant."""
        if self._call_policy(None).asks:
            return "ask", None
        decision = await self._review(gate)
        if decision.action != "allow":
            gate.deny_message = self._prompt_loader.load(
                "reviewer_denied",
                {
                    "reason": decision.explanation
                    or "The permission reviewer did not produce an approval."
                },
            )
            self._record_event(
                "retry_refused",
                {
                    "command": gate.command,
                    "reason": decision.explanation,
                },
            )
            return "refuse", None
        self._record_event(
            "retry_allowed",
            {
                "command": gate.command,
                "reason": decision.explanation,
            },
        )
        return "run", confinement.approved(
            by=confinement.APPROVED_BY_REVIEWER,
            purpose=gate.explanation,
            whole_disk=True,
        )
