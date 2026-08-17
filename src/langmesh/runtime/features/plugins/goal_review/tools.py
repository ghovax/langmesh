"""The goal plugin's own tools: the review submission and the goal update, defined where the plugin lives."""

from __future__ import annotations

from typing import Any

from langchain.tools import tool
from langchain_core.tools import StructuredTool

from langmesh.base.primitives.serialization import compact
from langmesh.runtime.features.plugins.goal_review import GoalReview
from langmesh.runtime.goal import Goal
from langmesh.runtime.tools.execution import current_tool_services
from langmesh.runtime.values import ToolStatus

async def _submit_goal_review(**arguments: Any) -> str:
    services = current_tool_services()
    services.features.invoke("submit_goal_review", GoalReview.model_validate(arguments))
    services.abort_event.set()
    return compact({"code": "goal_review_submitted", "status": ToolStatus.OK.value})

submit_goal_review = StructuredTool.from_function(
    coroutine=_submit_goal_review,
    name="submit_goal_review",
    description="Submit the goal review's verdict.",
    args_schema=GoalReview,
)

@tool
async def update_goal(
    *,
    goal: str,
    purpose: str,
    requirements: list[str],
) -> str:
    """Set the goal; described in descriptions/update_goal.md."""
    services = current_tool_services()
    goal_text = goal.strip()
    purpose_text = purpose.strip()
    requirement_lines = [line for line in (str(entry).strip() for entry in requirements) if line]

    def refuse(message: str) -> dict[str, Any]:
        return {"code": "goal_update_error", "status": "error", "message": message}

    if not goal_text:
        result = refuse("Say what the goal is: the end state, written so it is either true or not.")
    elif not purpose_text:
        result = refuse("Say what the end state is for, so a closed route can be told from a lost goal.")
    elif not requirement_lines:
        result = refuse("A goal needs minimum conditions: what must hold for it to be met, each one something a reader can go and check.")
    else:
        current = services.features.invoke("goal_current")
        services.features.invoke(
            "goal_write",
            Goal(text=goal_text, purpose=purpose_text, requirements=requirement_lines,
                 continuations=current.continuations if current is not None else 0),
        )
        result = {"code": "goal_active", "goal": goal_text, "purpose": purpose_text, "requirements": requirement_lines}
        services.record_event("goal_updated", result)
    return compact(result)

