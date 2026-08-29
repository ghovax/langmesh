"""The goal plugin's own tools: the review submission and the goal update, defined where the plugin lives."""

from __future__ import annotations

from copy import copy
from pathlib import Path
from typing import Any

from langchain.tools import tool
from langchain_core.tools import StructuredTool

from langmesh.runtime.plugins.goal_review.configuration import GoalReviewConfiguration
from langmesh.base.content.prompts import PackagePromptLoader
from langmesh.base.primitives.serialization import compact
from langmesh.runtime.plugins.goal_review.capability import GoalCapability
from langmesh.runtime.plugins.goal_review.models import GoalReview
from langmesh.runtime.plugins.goal_review.goal import Goal
from langmesh.runtime.tools.execution import current_tool_services
from langmesh.runtime.values import ToolStatus

#: The tools' model-facing descriptions, read from this plugin's own prompts directory.
_DESCRIPTIONS = PackagePromptLoader(Path(__file__).parent / "prompts")


def update_goal_description(settlement: str) -> str:
    """The model-facing `update_goal` text for who settles a mark."""
    reviewer = settlement != GoalReviewConfiguration.AGENT
    return _DESCRIPTIONS.load(
        "update_goal",
        {
            "reviewer_clause": (
                _DESCRIPTIONS.load("update_goal_reviewer_clause", {}).strip() if reviewer else ""
            ),
            "agent_clause": (
                _DESCRIPTIONS.load("update_goal_agent_clause", {}).strip() if not reviewer else ""
            ),
            "satisfied_consequence": (
                "the review confirms rather than trusting your word"
                if reviewer
                else "that mark ends the session"
            ),
        },
    ).strip()


def described_update_goal(*, settlement: str):
    """`update_goal` bound to the settlement the session actually uses, without mutating the shared tool."""
    description = update_goal_description(settlement)
    if description == update_goal.description:
        return update_goal
    if hasattr(update_goal, "model_copy"):
        return update_goal.model_copy(update={"description": description})
    cloned = copy(update_goal)
    cloned.description = description
    return cloned


async def _submit_goal_review(**arguments: Any) -> str:
    services = current_tool_services()
    services.features.require(GoalCapability).submit(GoalReview.model_validate(arguments))
    services.abort_event.set()
    return compact({"code": "goal_review_submitted", "status": ToolStatus.OK.value})


submit_goal_review = StructuredTool.from_function(
    coroutine=_submit_goal_review,
    name="submit_goal_review",
    description=_DESCRIPTIONS.load("submit_goal_review", {}).strip(),
    args_schema=GoalReview,
)


@tool
async def update_goal(
    *,
    goal: str,
    purpose: str,
    requirements: list[str],
    status: str = Goal.ACTIVE,
) -> str:
    """Set the goal; described in descriptions/update_goal.md."""
    services = current_tool_services()
    goal_text = goal.strip()
    purpose_text = purpose.strip()
    requirement_lines = [line for line in (str(entry).strip() for entry in requirements) if line]

    def refuse(message: str) -> dict[str, Any]:
        return {"code": "goal_update_error", "status": "error", "message": message}

    status_text = str(status).strip().lower()
    if status_text not in {
        Goal.ACTIVE,
        Goal.SATISFIED,
        Goal.BLOCKED,
        Goal.PARKED,
        Goal.CLEARED,
    }:
        result = refuse(
            f"Unknown goal status {status!r}: use active, satisfied, blocked, parked or cleared."
        )
    elif not goal_text:
        result = refuse("Say what the goal is: the end state, written so it is either true or not.")
    elif not purpose_text:
        result = refuse(
            "Say what the end state is for, so a closed route can be told from a lost goal."
        )
    elif not requirement_lines:
        result = refuse(
            "A goal needs minimum conditions: what must hold for it to be met, each one something a reader can go and check."
        )
    else:
        goals = services.features.require(GoalCapability)
        current = goals.goal
        # A mark the agent sets for itself is a claim when a reviewer settles it, and the
        # verdict itself when the working agent is the settlement. A deferral or a clear
        # is administrative either way.
        settlement = getattr(goals, "settlement", GoalReviewConfiguration.REVIEWER)
        reviewer_settles = settlement != GoalReviewConfiguration.AGENT
        pending_review = (
            status_text
            if reviewer_settles and status_text in (Goal.SATISFIED, Goal.BLOCKED)
            else None
        )
        effective_status = Goal.ACTIVE if pending_review is not None else status_text
        goals.write(
            Goal(
                text=goal_text,
                purpose=purpose_text,
                requirements=requirement_lines,
                status=effective_status,
                pending_review=pending_review,
                continuations=current.continuations if current is not None else 0,
            ),
        )
        result = {
            "code": "goal_active" if effective_status == Goal.ACTIVE else "goal_status",
            "goal": goal_text,
            "purpose": purpose_text,
            "requirements": requirement_lines,
            "status": effective_status,
        }
        if pending_review is not None:
            result["pending_review"] = pending_review
        services.record_event("goal_updated", result)
    return compact(result)


update_goal.description = (
    update_goal_description(GoalReviewConfiguration.REVIEWER) or update_goal.description
)
