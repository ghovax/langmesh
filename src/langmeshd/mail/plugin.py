"""The mail plugin: the email that lands in the thread is a tool call.

The daemon composes this feature on every hosted session; it is silent unless the session is a
mailbox thread. Progress and reply both send a new SMTP message. The mail client marks IMAP
Seen after a reply, and does not harvest assistant prose.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from langmesh import PackagePromptLoader
from langmesh.base.primitives.serialization import compact
from langmesh.runtime.features import Feature, PluginContext, PluginHost
from langmesh.runtime.tools.execution import current_tool_services
from langmesh.runtime.values import ToolStatus

_PROMPTS = PackagePromptLoader(Path(__file__).resolve().parent / "prompts")
logger = logging.getLogger("langmeshd.mail")
PROGRESS_TURNS_INTERVAL = 24

MailKind = Literal["progress", "reply"]


class MailUpdate(BaseModel):
    """The markdown `submit_email` sends as a new message in this thread."""

    body: str = Field(description="The entire email to send. Markdown is rendered as HTML.")
    kind: MailKind = Field(
        default="progress",
        description=_PROMPTS.load("email_kind", {}).strip(),
    )


@runtime_checkable
class EmailReplyCapability(Protocol):
    """SMTP for a mailbox session. The method is not `submit`: GoalReviewFeature also has that name."""

    async def submit_mailbox(self, body: str, *, kind: MailKind = "progress") -> None: ...


async def _submit_email(**arguments: Any) -> str:
    payload = MailUpdate.model_validate(arguments)
    await (
        current_tool_services()
        .features.require(EmailReplyCapability)
        .submit_mailbox(payload.body, kind=payload.kind)
    )
    return compact(
        {
            "code": "email_submitted",
            "status": ToolStatus.OK.value,
            "kind": payload.kind,
        }
    )


submit_email = StructuredTool.from_function(
    coroutine=_submit_email,
    name="submit_email",
    description=_PROMPTS.load("submit_email", {}).strip(),
    args_schema=MailUpdate,
)


class EmailReply(Feature):
    """Send `submit_email` over SMTP and remind until a call is a reply."""

    def __init__(self) -> None:
        self._replied = False
        self._openings = 0
        self._mail: bool | None = None
        self._host: PluginHost | None = None

    def attach(self, context: PluginContext, host: PluginHost | None = None) -> None:
        self._context = context
        self._host = host

    def _is_mail(self) -> bool:
        if self._mail is not None:
            return self._mail
        session_id = getattr(getattr(self, "_context", None), "session_id", "")
        if not session_id:
            return False
        from langmeshd.mail.threads import session_is_mailbox

        self._mail = session_is_mailbox(session_id)
        return self._mail

    def compose_prompt(self, variables: dict[str, str]) -> None:
        if not self._is_mail():
            return
        variables["channel_guidance"] = _PROMPTS.load("channel_guidance", {}).strip()

    def contribute_tools(self) -> list:
        """The mail tool, for a profile that declared it, or before attachment for discovery.

        The daemon names `submit_email` in `tools_enabled` only for mailbox sessions, the same
        way the GitHub embedder names `submit_github_comment`.
        """
        context = getattr(self, "_context", None)
        if context is None:
            return [submit_email]
        declared = getattr(context.agent_configuration, "tools_enabled", None) or []
        return [submit_email] if "submit_email" in declared else []

    async def submit_mailbox(self, body: str, *, kind: MailKind = "progress") -> None:
        from langmeshd.commons.configuration_file import load as load_document
        from langmeshd.commons.configuration import EmailConfiguration
        from langmeshd.mail.body import reference_chain
        from langmeshd.mail.smtp import outbound_message_id, reply_message, send_reply
        from langmeshd.mail.threads import POSTED, SEEN, ThreadStore

        session_id = self._context.session_id
        store = ThreadStore()
        try:
            binding = store.binding_for_session(session_id)
            item = store.latest_item_for_session(session_id)
            if binding is None or item is None:
                raise RuntimeError("This session is not a mailbox thread.")
            if kind == "reply" and (self._replied or item.state in {POSTED, SEEN}):
                return
            configuration = EmailConfiguration.model_validate(
                (load_document() or {}).get("email") or {}
            )
            outbound_id = outbound_message_id()
            in_reply_to = binding["last_message_id"] or item.message_id or item.in_reply_to
            references = reference_chain(
                binding["last_references"],
                in_reply_to,
                item.message_id,
            )
            outbound = reply_message(
                configuration=configuration,
                to_address=item.sender or binding["reply_address"],
                subject=item.subject or binding["subject"] or "LangMesh",
                body=body,
                in_reply_to=in_reply_to,
                references=references,
                message_id=outbound_id,
            )
            try:
                await send_reply(configuration, outbound)
            except Exception:
                logger.exception("could not send submit_email")
                raise
            store.note_outbound(
                session_id,
                message_id=outbound_id,
                references=references,
            )
            if kind == "reply":
                store.mark_replied(
                    session_id,
                    reply_text=body,
                    outbound_message_id=outbound_id,
                )
                self._replied = True
        finally:
            store.close()

    def prepare_request(self) -> None:
        host = self._host
        if host is None or host.turn.maintenance_active() or not self._is_mail():
            return
        self._openings += 1
        if self._openings % PROGRESS_TURNS_INTERVAL != 1:
            return
        name = "email_progress_start" if self._openings == 1 else "email_progress_interval"
        host.conversation.messages.append(
            host.turn.reminder_message(_PROMPTS.load(name, {}).strip())
        )
        host.bookkeeping.note_state_changed()

    def should_complete_turn(self) -> bool:
        if not self._is_mail():
            return False
        from langmeshd.mail.threads import POSTED, SEEN, ThreadStore

        store = ThreadStore()
        try:
            item = store.latest_item_for_session(self._context.session_id)
            posted = item is not None and item.state in {POSTED, SEEN}
            self._replied = posted
            return posted
        finally:
            store.close()

    def incomplete_reminder(self) -> str | None:
        if not self._is_mail() or self.should_complete_turn():
            return None
        return _PROMPTS.load("email_missing", {}).strip()


__all__ = [
    "EmailReply",
    "EmailReplyCapability",
    "MailKind",
    "MailUpdate",
    "PROGRESS_TURNS_INTERVAL",
    "submit_email",
]
