"""The interaction plugin: asking the person a question, mid-turn.

The library core never names ask_user: the tool and its description are this plugin's,
contributed through the feature seam when the host composes it. A bare library embedding has
no ask-the-user surface at all.
"""

from __future__ import annotations

from pathlib import Path

from langchain.tools import tool

from langmesh.base.configuration import PromptLoader
from langmesh.base.primitives.serialization import compact
from langmesh.runtime.features import Feature, PluginContext
from langmesh.runtime.tools.execution import current_tool_decision, current_tool_services

#: The tool's model-facing description, read from this plugin's own prompts directory.
_DESCRIPTIONS = PromptLoader(Path(__file__).parent / "prompts")


@tool
async def ask_user(
    *,
    questions: list[dict],
) -> str:
    """Ask the user; described in descriptions/ask_user.md."""
    services = current_tool_services()
    answers = current_tool_decision().answers if current_tool_decision() is not None else None
    if isinstance(answers, dict) and answers.get("__declined__"):
        result = {
            "code": "user_declined",
            "status": "error",
            "decision": {
                "actor": str(answers.get("__actor__") or "person"),
                "reason": answers.get("__reason__") or None,
            },
        }
        services.abort_event.set()
    else:
        result = {"code": "user_answered", "answers": answers}
    return compact(result)


class Interaction(Feature):
    """Asking the person a question, mid-turn."""

    def attach(self, context: PluginContext, host=None) -> None:
        self._context = context

    def contribute_tools(self) -> list:
        """The ask_user tool, for a profile that declared it."""
        context = getattr(self, "_context", None)
        if context is None:
            return [ask_user]
        declared = getattr(context.agent_configuration, "tools_enabled", None)
        return [ask_user] if declared is not None and "ask_user" in declared else []


# The tool's model-facing description is this plugin's own file, applied once at import.
ask_user.description = _DESCRIPTIONS.load("ask_user", {}).strip() or ask_user.description

__all__ = ["Interaction", "ask_user"]
