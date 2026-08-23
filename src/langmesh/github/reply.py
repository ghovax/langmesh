"""The GitHub mention plugin: the comment that lands on the thread is a tool call.

The Action posts whatever this feature collected, not the model's prose. The working session
sees `submit_github_comment` because the embedder both composed this feature and named the
tool in `tools_enabled`. Each call writes the acknowledgement in place. A later mention must
submit its own reply; earlier submissions are not the next turn's comment.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from langmesh.base.content.prompts import PackagePromptLoader
from langmesh.base.primitives.serialization import compact
from langmesh.runtime.features import Feature, PluginContext
from langmesh.runtime.tools.execution import current_tool_services
from langmesh.runtime.values import ToolStatus

_PROMPTS = PackagePromptLoader(Path(__file__).resolve().parent / "prompts")
logger = logging.getLogger("langmesh.github")


class GitHubComment(BaseModel):
    """The comment `submit_github_comment` writes onto the acknowledgement."""

    comment: str = Field(
        description="The entire GitHub comment to write onto this issue or pull request."
    )
    done: bool = Field(
        default=False,
        description=(
            "True when this is the finished reply and the turn should end. "
            "False for a brief progress note; keep working after that call."
        ),
    )


@runtime_checkable
class GitHubReplyCapability(Protocol):
    @property
    def comment(self) -> str | None: ...

    def submit(self, comment: str, *, done: bool = False) -> None: ...


async def _submit_github_comment(**arguments: Any) -> str:
    payload = GitHubComment.model_validate(arguments)
    current_tool_services().features.require(GitHubReplyCapability).submit(
        payload.comment, done=payload.done
    )
    return compact(
        {
            "code": "github_comment_submitted",
            "status": ToolStatus.OK.value,
            "done": payload.done,
        }
    )


submit_github_comment = StructuredTool.from_function(
    coroutine=_submit_github_comment,
    name="submit_github_comment",
    description=_PROMPTS.load("submit_github_comment", {}).strip(),
    args_schema=GitHubComment,
)


class GitHubReply(Feature):
    """Write `submit_github_comment` in place and remind until a call sets done."""

    def __init__(self, publish: Callable[[str], None] | None = None) -> None:
        self._comment: str | None = None
        self._done = False
        self._publish = publish

    def attach(self, context: PluginContext, host=None) -> None:
        self._context = context

    @property
    def comment(self) -> str | None:
        return self._comment

    def submit(self, comment: str, *, done: bool = False) -> None:
        self._comment = comment
        if done:
            self._done = True
        if self._publish is None:
            return
        try:
            self._publish(comment)
        except Exception:
            logger.exception("could not write submit_github_comment onto the thread")

    def contribute_tools(self) -> list:
        """The comment tool, for a profile that declared it, or before attachment for discovery."""
        context = getattr(self, "_context", None)
        if context is None:
            return [submit_github_comment]
        declared = getattr(context.agent_configuration, "tools_enabled", None) or []
        return [submit_github_comment] if "submit_github_comment" in declared else []

    def should_complete_turn(self) -> bool:
        return self._done

    def incomplete_reminder(self) -> str | None:
        if self._done:
            return None
        return _PROMPTS.load("github_comment_missing", {}).strip()


__all__ = [
    "GitHubComment",
    "GitHubReply",
    "GitHubReplyCapability",
    "submit_github_comment",
]
