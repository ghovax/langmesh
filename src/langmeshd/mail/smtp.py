"""Outbound replies: one SMTP message per finished turn, retried with a stable Message-ID."""

from __future__ import annotations

from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import aiosmtplib
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from langmeshd.commons.configuration import EmailConfiguration


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
    """A reply that carries In-Reply-To / References so the next inbound stays on the same thread."""
    message = EmailMessage()
    mailbox = configuration.effective_address
    domain = mailbox.rsplit("@", 1)[-1] if "@" in mailbox else "langmesh.local"
    message["From"] = mailbox
    message["To"] = to_address
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = message_id or make_msgid(domain=domain)
    reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}" if subject else "Re: LangMesh"
    message["Subject"] = reply_subject
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    chain = " ".join(part for part in (references, in_reply_to) if part).strip()
    if chain:
        message["References"] = chain
    message.set_content(body)
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
        hostname=configuration.effective_smtp_host,
        port=configuration.smtp.port,
        username=configuration.effective_smtp_username or None,
        password=configuration.effective_smtp_password or None,
        start_tls=configuration.smtp.start_tls if not configuration.smtp.use_tls else False,
        use_tls=configuration.smtp.use_tls,
        timeout=60,
    )
