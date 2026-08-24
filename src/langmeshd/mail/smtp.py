"""Outbound replies: one SMTP message per finished turn, retried with a stable Message-ID."""

from __future__ import annotations

import contextlib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any

import aiosmtplib
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from langmeshd.commons.configuration import EmailConfiguration
from langmeshd.mail.body import reference_chain
from langmeshd.mail.html import html_from_markdown

# Do not mint Message-IDs at gmail.com / outlook / etc: those providers rewrite ids that
# impersonate their domain, and a later In-Reply-To would miss the thread.
MESSAGE_ID_DOMAIN = "langmesh.mail"


def outbound_message_id() -> str:
    """A Message-ID the mailbox provider is unlikely to replace."""
    return make_msgid(domain=MESSAGE_ID_DOMAIN)


def _smtp_kwargs(configuration: EmailConfiguration) -> dict[str, Any]:
    """The aiosmtplib connect options for this mailbox. No password means no AUTH."""
    password = configuration.effective_smtp_password or None
    username = configuration.effective_smtp_username or None if password else None
    return {
        "hostname": configuration.effective_smtp_host,
        "port": configuration.effective_smtp_port,
        "username": username,
        "password": password,
        "start_tls": (
            configuration.effective_smtp_start_tls
            if not configuration.effective_smtp_use_tls
            else False
        ),
        "use_tls": configuration.effective_smtp_use_tls,
    }


def reply_message(
    *,
    configuration: EmailConfiguration,
    to_address: str,
    subject: str,
    body: str,
    in_reply_to: str,
    references: str,
    message_id: str = "",
) -> EmailMessage:
    """A reply whose preferred body is HTML rendered from the agent's markdown."""
    message = EmailMessage()
    mailbox = configuration.effective_address
    message["From"] = mailbox
    message["To"] = to_address
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = message_id or outbound_message_id()
    reply_subject = (
        subject
        if subject.lower().startswith("re:")
        else f"Re: {subject}"
        if subject
        else "Re: LangMesh"
    )
    message["Subject"] = reply_subject
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    chain = reference_chain(references, in_reply_to)
    if chain:
        message["References"] = chain
    message.set_content(body)
    message.add_alternative(html_from_markdown(body), subtype="html")
    return message


@retry(
    retry=retry_if_exception_type((OSError, aiosmtplib.SMTPException)),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(8),
    reraise=True,
)
async def send_reply(configuration: EmailConfiguration, message: EmailMessage) -> None:
    """Hand the message to aiosmtplib. The same Message-ID is reused on retry so a duplicate is the same mail."""
    await aiosmtplib.send(
        message,
        **_smtp_kwargs(configuration),
        timeout=60,
    )


async def probe_smtp(configuration: EmailConfiguration) -> None:
    """Prove SMTP will accept this mailbox. Fail now, not after the first turn has already run."""
    client = aiosmtplib.SMTP(**_smtp_kwargs(configuration), timeout=30)
    await client.connect()
    try:
        await client.noop()
    finally:
        with contextlib.suppress(Exception):
            await client.quit()
