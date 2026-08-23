"""The GitHub mention plugin: the comment that lands on the thread is a tool call.

The Action posts whatever this feature collected, not the model's prose. The working session
sees `submit_github_comment` because the embedder both composed this feature and named the
tool in `tools_enabled`. The submitted comment is not checkpointed: a later mention must
submit its own reply.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from langmesh.base.content.prompts import PackagePromptLoader
from langmesh.base.primitives.serialization import compact
from langmesh.runtime.features import Feature, PluginContext
from langmesh.runtime.tools.execution import current_tool_services
from langmesh.runtime.values import ToolStatus

TOOL_NAME = "submit_github_comment"
REMINDER_LIMIT = 3
_PROMPTS = PackagePromptLoader(Path(__file__).resolve().parent / "prompts")


class GitHubComment(BaseModel):
    """The comment `submit_github_comment` posts on the issue or pull request."""

    comment: str = Field(
        description="The entire GitHub comment to post on this issue or pull request."
    )


@runtime_checkable
class GitHubReplyCapability(Protocol):
    @property
    def comment(self) -> str | None: ...

    def submit(self, comment: str) -> None: ...


async def _submit_github_comment(**arguments: Any) -> str:
    payload = GitHubComment.model_validate(arguments)
    current_tool_services().features.require(GitHubReplyCapability).submit(payload.comment)
    return compact({"code": "github_comment_submitted", "status": ToolStatus.OK.value})


submit_github_comment = StructuredTool.from_function(
    coroutine=_submit_github_comment,
    name=TOOL_NAME,
    description=_PROMPTS.load(TOOL_NAME, {}).strip(),
    args_schema=GitHubComment,
)


class GitHubReply(Feature):
    """Collect the GitHub comment through `submit_github_comment` and remind until it lands."""

    def __init__(self) -> None:
        self._comment: str | None = None
        self._reminders = 0

    def attach(self, context: PluginContext, host=None) -> None:
        self._context = context

    @property
    def comment(self) -> str | None:
        return self._comment

    def submit(self, comment: str) -> None:
        self._comment = comment

    def contribute_tools(self) -> list:
        """The comment tool, for a profile that declared it, or before attachment for discovery."""
        context = getattr(self, "_context", None)
        if context is None:
            return [submit_github_comment]
        declared = getattr(context.agent_configuration, "tools_enabled", None) or []
        return [submit_github_comment] if TOOL_NAME in declared else []

    def should_complete_turn(self) -> bool:
        return self._comment is not None

    def incomplete_reminder(self) -> str | None:
        if self._comment is not None or self._reminders >= REMINDER_LIMIT:
            return None
        self._reminders += 1
        return _PROMPTS.load("github_comment_missing", {}).strip()


__all__ = [
    "GitHubComment",
    "GitHubReply",
    "GitHubReplyCapability",
    "REMINDER_LIMIT",
    "TOOL_NAME",
    "submit_github_comment",
]
