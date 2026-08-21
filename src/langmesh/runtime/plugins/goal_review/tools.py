"""The goal plugin's own tools: the review submission and the goal update, defined where the plugin lives."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain.tools import tool
from langchain_core.tools import StructuredTool

from langmesh.base.content.prompts import PackagePromptLoader
from langmesh.base.primitives.serialization import compact
from langmesh.runtime.features import GoalCapability
from langmesh.runtime.plugins.goal_review.models import GoalReview
from langmesh.runtime.plugins.goal_review.goal import Goal
from langmesh.runtime.tools.execution import current_tool_services
from langmesh.runtime.values import ToolStatus

#: The tools' model-facing descriptions, read from this plugin's own prompts directory.
_DESCRIPTIONS = PackagePromptLoader(Path(__file__).parent / "prompts")


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
        # A mark the agent sets for itself earns a secondary review only when it is a
        # completion or blockage claim worth auditing; a deferral or a clear is administrative.
        pending_review = status_text if status_text in (Goal.SATISFIED, Goal.BLOCKED) else None
        goals.write(
            Goal(
                text=goal_text,
                purpose=purpose_text,
                requirements=requirement_lines,
                status=status_text,
                pending_review=pending_review,
                continuations=current.continuations if current is not None else 0,
            ),
        )
        result = {
            "code": "goal_active" if status_text == Goal.ACTIVE else "goal_status",
            "goal": goal_text,
            "purpose": purpose_text,
            "requirements": requirement_lines,
            "status": status_text,
        }
        if pending_review is not None:
            result["pending_review"] = pending_review
        services.record_event("goal_updated", result)
    return compact(result)


update_goal.description = _DESCRIPTIONS.load("update_goal", {}).strip() or update_goal.description
