"""The GitHub mention plugin: the comment that lands on the thread is a tool call.

The Action posts whatever this feature collected, not the model's prose. The working session
sees `submit_github_comment` because the embedder both composed this feature and named the
tool in `tools_enabled`. Each call writes the acknowledgement in place. A later mention must
submit its own reply; earlier submissions are not the next turn's comment.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Literal, Protocol, runtime_checkable

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from langmesh.base.content.prompts import PackagePromptLoader
from langmesh.base.primitives.serialization import compact
from langmesh.runtime.features import Feature, PluginContext, PluginHost
from langmesh.runtime.tools.execution import current_tool_services
from langmesh.runtime.values import ToolStatus

_PROMPTS = PackagePromptLoader(Path(__file__).resolve().parent / "prompts")
logger = logging.getLogger("langmesh.github")
# Model openings of this mention job between progress reminders. Opening 1 of a
# new thread reminds so a direction update lands before other work; then 33, 65, …
PROGRESS_TURNS = 32

CommentKind = Literal["progress", "reply"]


class GitHubComment(BaseModel):
    """The comment `submit_github_comment` writes onto the acknowledgement."""

    comment: str = Field(
        description="The entire GitHub comment to write onto this issue or pull request."
    )
    kind: CommentKind = Field(
        default="progress",
        description=_PROMPTS.load("github_comment_kind", {}).strip(),
    )


@runtime_checkable
class GitHubReplyCapability(Protocol):
    @property
    def comment(self) -> str | None: ...

    def submit(self, comment: str, *, kind: CommentKind = "progress") -> None: ...


async def _submit_github_comment(**arguments: Any) -> str:
    payload = GitHubComment.model_validate(arguments)
    current_tool_services().features.require(GitHubReplyCapability).submit(
        payload.comment, kind=payload.kind
    )
    return compact(
        {
            "code": "github_comment_submitted",
            "status": ToolStatus.OK.value,
            "kind": payload.kind,
        }
    )


submit_github_comment = StructuredTool.from_function(
    coroutine=_submit_github_comment,
    name="submit_github_comment",
    description=_PROMPTS.load("submit_github_comment", {}).strip(),
    args_schema=GitHubComment,
)


class GitHubReply(Feature):
    """Write `submit_github_comment` in place and remind until a call is a reply."""

    def __init__(
        self,
        publish: Callable[[str], None] | None = None,
        *,
        followup: bool = False,
    ) -> None:
        self._comment: str | None = None
        self._replied = False
        self._publish = publish
        self._followup = followup
        self._openings = 0
        self._host: PluginHost | None = None

    def attach(self, context: PluginContext, host: PluginHost | None = None) -> None:
        self._context = context
        self._host = host

    @property
    def comment(self) -> str | None:
        return self._comment

    def submit(self, comment: str, *, kind: CommentKind = "progress") -> None:
        self._comment = comment
        if kind == "reply":
            self._replied = True
        if self._publish is None:
            return
        try:
            self._publish(comment)
        except Exception:
            logger.exception("could not write submit_github_comment onto the thread")

    def prepare_request(self) -> None:
        """Append a progress reminder at the first opening of a new thread, then every ``PROGRESS_TURNS``.

        A follow-up mention skips that first note so a continued thread does not get
        the same "call progress first" instruction again. The note is a harness
        reminder on the conversation tail. The system prompt and tool schema are not
        rewritten, so the provider-cache prefix stays intact.
        """
        host = self._host
        if host is None or host.turn.maintenance_active():
            return
        self._openings += 1
        if self._followup and self._openings == 1:
            return
        if self._openings % PROGRESS_TURNS != 1:
            return
        name = (
            "github_comment_progress_start"
            if self._openings == 1
            else "github_comment_progress_interval"
        )
        host.conversation.messages.append(
            host.turn.reminder_message(_PROMPTS.load(name, {}).strip())
        )
        host.bookkeeping.note_state_changed()

    def contribute_tools(self) -> list:
        """The comment tool, for a profile that declared it, or before attachment for discovery."""
        context = getattr(self, "_context", None)
        if context is None:
            return [submit_github_comment]
        declared = getattr(context.agent_configuration, "tools_enabled", None) or []
        return [submit_github_comment] if "submit_github_comment" in declared else []

    def should_complete_turn(self) -> bool:
        return self._replied

    def incomplete_reminder(self) -> str | None:
        if self._replied:
            return None
        return _PROMPTS.load("github_comment_missing", {}).strip()


__all__ = [
    "CommentKind",
    "GitHubComment",
    "GitHubReply",
    "GitHubReplyCapability",
    "submit_github_comment",
]
