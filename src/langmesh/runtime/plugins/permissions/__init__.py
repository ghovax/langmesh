"""The permission-review plugin: the boundary's verdict, the grants it records, and the gates.

Whether a call runs, is asked about, or is refused is one pluggable concern. The automatic
model evaluator is a separate plugin, injected here as the reviewer; the standing grants a
session earns are this plugin's own state, and the runtime reaches them through its accessor
alone.
"""

from __future__ import annotations

import ast
import logging
import uuid
from typing import Any, Optional, cast

from langmesh.base import confinement
from langmesh.base.confinement import Grant, parse_access_request
from langmesh.base.primitives.serialization import compact
from langmesh.runtime.internals import (
    _coerce_structured_arguments,
    _PreflightGate,
    _ResolvedToolDecision,
    _ToolPlan,
)
from langmesh.runtime.values import PermissionAnswer, PermissionReason
from langmesh.runtime.boundary import RULE_ALLOW, RULE_ASK, escape_of, verdict_for
from langmesh.runtime.locations import PermissionDecision
from langmesh.runtime.features import Feature, PluginContext, PluginHost

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


def screen_mutations(script: str) -> tuple[str, ...]:
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


def _screen_primitive(func: ast.expr) -> str:
    """The primitive a call node names, bare or through ``screen``."""
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


class PermissionReview(Feature):
    """Whether a call runs, is asked about, or is refused, and what approval a session keeps."""

    def __init__(self, reviewer: Any = None) -> None:
        # The automatic model evaluator, injected by the composer: a separate plugin that owns
        # the model call. None disables automatic review, leaving only the rule boundary.
        self._reviewer = reviewer

    def attach(self, context: PluginContext, host: PluginHost) -> None:
        self._context = context
        self._host = host
        self._prompts = context.prompts("permissions")

    @property
    def access_grants(self) -> list[Grant]:
        """The standing grants, kept on the core boundary so other plugins never need this one."""
        return list(self._host.boundary.access_grants())

    def granted_profile(self):
        """The session's confinement with every standing grant compacted in. What an escape is measured against."""
        return self._host.boundary.granted_profile()

    def terminate_tool_call(self, tool_call_id: str) -> bool:
        """Gates wait for an answer; they are not a process this plugin owns to kill."""
        return False

    async def review(self, gate: _PreflightGate) -> PermissionDecision:
        """The verdict on one gate, from the injected automatic reviewer or a denial when absent."""
        if self._reviewer is None:
            return PermissionDecision(
                action="deny",
                explanation="No automatic reviewer is installed, so this request was refused.",
                risk="medium",
            )
        return await self._reviewer.review(gate)

    def record_grant(self, grant: Grant) -> None:
        """Remember an approved widening on the core boundary, so one grant is not re-asked on every command."""
        self._host.boundary.record_grant(grant)
        self._host.bookkeeping.record_event(
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

    def _new_permission_request_id(self, tool_call_id: str = "") -> str:
        """The id an answer is filed under, derived from the call so a replanned gate is the same gate."""
        return f"perm-{self._context.session_id}-{tool_call_id or uuid.uuid4()}"

    def _new_question_request_id(self, tool_call_id: str = "") -> str:
        """Stable for the same reason, and by the same means, as the permission id above."""
        return f"q-{self._context.session_id}-{tool_call_id or uuid.uuid4()}"

    def _new_retry_request_id(self, tool_call_id: str) -> str:
        """The id for a second run, distinct from the preflight id since both can exist in one turn."""
        return f"retry-{self._context.session_id}-{tool_call_id}"

    async def plan_tool_calls(self, tool_calls: list[dict]) -> Any | None:
        """Plan one batch's calls: the plans and the gates they raised, if any."""
        plans, gates = await self.preflight(tool_calls)
        if not plans:
            return None
        return plans, gates

    def resolve_gates(self, plans: Any, answers: dict) -> dict:
        """Plans plus answers into one verdict per call."""
        return self.resolve_tool_decisions(plans, answers)

    async def preflight(
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
        schema = self._host.tools.tool_schemas.get(tool_name)
        if schema is not None:
            tool_arguments = _coerce_structured_arguments(schema, tool_arguments)

        call_site = self._host.boundary.resolve_execution(tool_name, tool_arguments)
        policy = self._host.boundary.call_policy(None)
        explanation = str(tool_arguments.get("explanation", "") or "")

        # `allow` mode: every call runs as if it had whatever it asked for. The boundary still
        # holds; only the gate is skipped.
        if not policy.gates:
            return plan

        # ask_user is the one call that is a question rather than an act.
        if tool_name == "ask_user":
            # The second lock: the tool is already withheld under `automatic`, but a stale plan could still name it.
            if not policy.asks:
                plan.refusal = self.refusal(self._prompts.load("nobody_to_ask", {}))
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
        # A remote call has no local sandbox to escape, while a named local target retains the local boundary.
        profile = None if call_site is not None and call_site.is_remote else self.granted_profile()
        request, _ = parse_access_request(tool_arguments.get("access_request"))
        escape = escape_of(request, profile, workspace=policy.working_directory)

        # A screen script's box is the primitives it was handed, so escaping it means changing something.
        mutations: tuple[str, ...] = ()
        if tool_name == "control_screen":
            mutations = screen_mutations(str(tool_arguments.get("script", "") or ""))
            if mutations and rule == RULE_ALLOW:
                plan.screen_mutations = True

        verdict = verdict_for(
            escape=escape,
            rule=rule,
            profile=profile,
            workspace=policy.working_directory,
        )
        if verdict.kind == "refuse":
            plan.refusal = self.refusal(
                self.hard_refusal_message(verdict.reason),
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
            explanation=self.escape_explanation(escape, explanation) if escape else explanation,
            reason=verdict.reason
            or (
                PermissionReason(kind="screen_mutation", paths=list(mutations))
                if mutations
                else PermissionReason(kind="rule_review")
            ),
            escape=escape,
            grants_screen_mutations=bool(mutations),
            is_bash=(tool_name == "bash"),
            deny_message=self.deny_message(tool_name),
        )
        # Under `automatic` the reviewer decides, but the gate is still announced so the call is visible while it is weighed; the review itself runs in the turn batch, after the event.
        if not policy.asks:
            gate.automatic_review = True
        plan.gates.append(gate)
        return plan

    async def review_automatic_gate(self, gate: _PreflightGate) -> Any | None:
        """The verdict for one automatic gate, from the injected reviewer plugin."""
        if self._reviewer is None:
            return None
        return await self._reviewer.review_automatic_gate(gate)

    def denial_subject(self, tool_name: str) -> str:
        """The concrete operation named in a model-facing permission denial."""
        if tool_name == "bash":
            return "command"
        if tool_name == "call_mcp_server_tool":
            return "MCP server tool call"
        if tool_name == "control_screen":
            return "screen action"
        return f"{tool_name} call"

    def escape_explanation(self, escape, explanation: str) -> str:
        """Render requested reach from structured values instead of assembling prose in Python."""
        return self._prompts.load(
            "permission_access_request",
            {
                "reads": compact(list(escape.reads)),
                "writes": compact(list(escape.writes)),
                "network": str(bool(escape.network)).lower(),
                "explanation": explanation or "No explanation was provided.",
            },
        )

    def hard_refusal_message(self, reason: Optional[PermissionReason]) -> str:
        """Render a configuration refusal from its structured reason."""
        if reason is not None and reason.kind == "path_denial":
            return self._prompts.load(
                "permission_path_denied",
                {"paths": compact(reason.paths)},
            )
        return self._prompts.load("permission_rule_denied", {})

    def resolved_denial_message(self, gate: _PreflightGate, answer: PermissionAnswer) -> str:
        """Render one denial without losing who decided it or the reason they supplied."""
        if answer.actor == "reviewer":
            if self._reviewer is not None:
                return self._reviewer.denied_message(answer.reason or "")
            return "The permission reviewer did not produce an approval."
        return self._prompts.load(
            "permission_person_denied"
            if answer.actor == "person"
            else "permission_approver_denied",
            {
                "subject": self.denial_subject(gate.tool_name),
                "reason": answer.reason or "No additional reason was provided.",
            },
        )

    def retry_refusal_result(
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
        tools = self._context.agent_configuration.tools
        if tool_name == "bash":
            command = str(tool_arguments.get("command", "") or "")
            return command, tools.bash.evaluate_permission(command, unmatched=RULE_ALLOW)
        if tool_name == "call_mcp_server_tool":
            subject = f"{tool_arguments.get('server', '')}.{tool_arguments.get('tool_name', '')}"
            return subject, tools.mcp.decide(subject, unmatched=RULE_ASK)
        if tool_name == "control_screen":
            mutations = screen_mutations(str(tool_arguments.get("script", "") or ""))
            subject = mutations[0] if mutations else "read"
            return subject, tools.screen.decide(
                subject, unmatched=RULE_ASK if mutations else RULE_ALLOW
            )
        if tool_name in self._host.tools.supplied_tool_names:
            # A supplied tool is unknown to the engine, so it is asked about unless the caller said otherwise.
            return tool_name, RULE_ALLOW if self._host.tools.tool_gate == "none" else RULE_ASK
        return tool_name, RULE_ALLOW

    def _command_of(self, tool_name: str, tool_arguments: dict) -> str:
        """What the person deciding is shown as *the thing being done*."""
        if tool_name == "bash":
            return str(tool_arguments.get("command", "") or "")
        if tool_name == "call_mcp_server_tool":
            return f"MCP server {tool_arguments.get('server', '')}.{tool_arguments.get('tool_name', '')}"
        return tool_name

    def deny_message(self, tool_name: str) -> str:
        """The model-facing explanation when the person answers a gate no."""
        return self._prompts.load(
            "permission_person_denied",
            {
                "subject": self.denial_subject(tool_name),
                "reason": "No additional reason was provided.",
            },
        )

    def refusal(
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

    def approve(
        self, gate: _PreflightGate, *, by: confinement.ApprovedBy, plan: Optional[_ToolPlan] = None
    ) -> None:
        """Carry out what approving a gate means, in one place, since it can grant two different things."""
        if gate.escape or gate.whole_disk:
            self.record_grant(
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

    def resolve_tool_decisions(
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
                        self.resolved_denial_message(gate, permission_answer)
                        if permission_answer is not None
                        else self._prompts.load("permission_invalid_answer", {})
                    )
                    if gate.refused_result is not None:
                        decision.completed = {
                            "result": self.retry_refusal_result(
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
                    "reviewer": confinement.APPROVED_BY_PERMISSION_REVIEWER,
                    "approver": confinement.APPROVED_BY_EXTERNAL_APPROVER,
                }[permission_answer.actor]
                self.approve(
                    gate,
                    by=cast(confinement.ApprovedBy, gate.approved_by or approved_by),
                )
                if gate.grants_screen_mutations:
                    decision.screen_mutations = True
                if gate.whole_disk:
                    decision.retry_grant = confinement.approved(
                        by=cast(confinement.ApprovedBy, gate.approved_by or approved_by),
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
            deny_message=self._prompts.load("permission_retry_person_denied", {}),
        )

    async def reconsider_gate(self, gate) -> dict[str, Any]:
        """Re-decide a gate the session is parked on, under the mode now in force."""
        if not self._host.boundary.call_policy(None).asks:
            return {}
        if gate.kind == "question":
            return {
                "declined": True,
                "actor": "session",
                "reason": self._prompts.load("question_unavailable_reason", {}),
            }
        decision = await self.review(
            _PreflightGate.from_dict(gate.to_dict() if hasattr(gate, "to_dict") else vars(gate))
        )
        return {
            "decision": decision.action,
            "reason": decision.explanation,
            "actor": "reviewer",
        }

    async def decide_retry(self, gate: _PreflightGate) -> tuple[str, Optional[Grant]]:
        """What to do with a retry gate: ask, run with a grant, or refuse. Three answers, not an optional grant."""
        if self._host.boundary.call_policy(None).asks:
            return "ask", None
        decision = await self.review(gate)
        if decision.action != "allow":
            gate.deny_message = (
                self._reviewer.denied_message(decision.explanation)
                if self._reviewer is not None
                else "The permission reviewer did not produce an approval."
            )
            self._host.bookkeeping.record_event(
                "retry_refused",
                {
                    "command": gate.command,
                    "reason": decision.explanation,
                },
            )
            return "refuse", None
        self._host.bookkeeping.record_event(
            "retry_allowed",
            {
                "command": gate.command,
                "reason": decision.explanation,
            },
        )
        return "run", confinement.approved(
            by=confinement.APPROVED_BY_PERMISSION_REVIEWER,
            purpose=gate.explanation,
            whole_disk=True,
        )


__all__ = ["MUTATING_SCREEN_PRIMITIVES", "PermissionReview", "screen_mutations"]
