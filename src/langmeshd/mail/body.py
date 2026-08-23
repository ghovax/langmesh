"""The body that becomes a turn: this message's content, with quoted history stripped.

Inbound MIME is parsed with the stdlib `email` package. HTML is reduced with markdownify.
Quoted reply tails are removed by email-reply-parser rather than ad-hoc regex.
"""

from __future__ import annotations

import hashlib
from email.message import EmailMessage, Message
from email.utils import parseaddr

from email_reply_parser import EmailReplyParser
from markdownify import markdownify


def sender_address(message: Message) -> str:
    """The mailbox the message claims to be from, lowercased, or empty."""
    _, address = parseaddr(str(message.get("From") or ""))
    return address.strip().lower()


def header_addresses(message: Message, name: str) -> list[str]:
    """Every address in a header, lowercased, ignoring display names."""
    values: list[str] = []
    for raw in message.get_all(name, []):
        _, address = parseaddr(str(raw))
        if address.strip():
            values.append(address.strip().lower())
    return values


def display_from(message: Message) -> str:
    """The From header as written, falling back to the bare address."""
    value = str(message.get("From") or "").strip()
    return value or sender_address(message)


def subject_of(message: Message) -> str:
    return " ".join(str(message.get("Subject") or "").split())


def message_id_of(message: Message) -> str:
    return str(message.get("Message-ID") or message.get("Message-Id") or "").strip()


def durable_identity(message: Message) -> str:
    """A stable id for this inbound mail, even when the server omitted Message-ID."""
    header = message_id_of(message)
    if header:
        return header
    digest = hashlib.sha256(
        "\n".join(
            (
                sender_address(message),
                str(message.get("Date") or ""),
                subject_of(message),
                turn_body(message),
            )
        ).encode()
    ).hexdigest()
    return f"sha256:{digest}"


def _split_message_ids(value: str) -> list[str]:
    """Message-IDs as they appear in References / In-Reply-To, in order."""
    found: list[str] = []
    current: list[str] = []
    for character in value:
        if character == "<":
            current = ["<"]
        elif current:
            current.append(character)
            if character == ">":
                token = "".join(current).strip()
                if token != "<>":
                    found.append(token)
                current = []
    return found


def referenced_ids(message: Message) -> list[str]:
    """Thread ancestors, oldest first: References, then In-Reply-To if it was missing."""
    ordered: list[str] = []
    seen: set[str] = set()
    for header in ("References", "In-Reply-To"):
        for raw in message.get_all(header, []):
            for identifier in _split_message_ids(str(raw)):
                if identifier not in seen:
                    seen.add(identifier)
                    ordered.append(identifier)
    return ordered


def thread_root_id(message: Message) -> str:
    """The id that names this conversation: the oldest reference, else this message."""
    ancestors = referenced_ids(message)
    if ancestors:
        return ancestors[0]
    return message_id_of(message)


def _plain_from_html(html: str) -> str:
    converted = markdownify(html, heading_style="ATX", strip=["script", "style"])
    return str(converted or "").strip()


def _part_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace").strip()
        except LookupError:
            return payload.decode("utf-8", errors="replace").strip()
    raw = part.get_payload()
    if isinstance(raw, str):
        return raw.strip()
    return str(raw or "").strip()


def extracted_body(message: Message) -> str:
    """The message's own prose: text/plain preferred, HTML reduced if that is all there is."""
    if isinstance(message, EmailMessage):
        plain = message.get_body(preferencelist=("plain",))
        if plain is not None:
            text = _part_text(plain)
            if text:
                return text
        html = message.get_body(preferencelist=("html",))
        if html is not None:
            return _plain_from_html(_part_text(html))
    if message.is_multipart():
        html_fallback = ""
        for part in message.walk():
            if part.get_content_maintype() != "text":
                continue
            subtype = part.get_content_subtype()
            text = _part_text(part)
            if subtype == "plain" and text:
                return text
            if subtype == "html" and text and not html_fallback:
                html_fallback = _plain_from_html(text)
        return html_fallback
    if message.get_content_subtype() == "html":
        return _plain_from_html(_part_text(message))
    return _part_text(message)


def turn_body(message: Message) -> str:
    """This specific email's content, with quoted reply history removed."""
    raw = extracted_body(message)
    if not raw.strip():
        return ""
    return str(EmailReplyParser.parse_reply(raw) or "").strip()


def is_automatic(message: Message) -> bool:
    """Whether this is a bounce, vacation, or list traffic that must not start a turn."""
    auto = str(message.get("Auto-Submitted") or "").strip().lower()
    if auto and auto != "no":
        return True
    precedence = str(message.get("Precedence") or "").strip().lower()
    if precedence in {"bulk", "junk", "list"}:
        return True
    return str(message.get("X-Auto-Response-Suppress") or "").strip().lower() in {
        "all",
        "autoreply",
    }


def sender_allowed(address: str, allow_from: list[str]) -> bool:
    """Whether `address` is on the allow-list: an exact mailbox, or `@domain` for a whole domain."""
    mailbox = address.strip().lower()
    if not mailbox or "@" not in mailbox:
        return False
    _, domain = mailbox.rsplit("@", 1)
    for entry in allow_from:
        token = entry.strip().lower()
        if not token:
            continue
        if token == mailbox:
            return True
        if token.startswith("@") and domain == token[1:]:
            return True
        if token.startswith("*@") and domain == token[2:]:
            return True
    return False
