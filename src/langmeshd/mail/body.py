"""The body that becomes a turn: this message's HTML, as markdown, without the quoted thread.

Mail is HTML. The HTML part is the body; text/plain is only used when there is no HTML.
Quoted replies are the containers mail clients wrap around the previous message. Those
nodes are removed, then the rest is converted with markdownify for the agent.
"""

from __future__ import annotations

import hashlib
from email.message import EmailMessage, Message
from email.utils import parseaddr

from bs4 import BeautifulSoup
from markdownify import markdownify


_GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


def canonical_mailbox(address: str) -> str:
    """Gmail plus-addresses and googlemail.com are the same account as user@gmail.com."""
    mailbox = address.strip().lower()
    if "@" not in mailbox:
        return mailbox
    local, domain = mailbox.rsplit("@", 1)
    if domain in _GMAIL_DOMAINS:
        local = local.split("+", 1)[0]
        domain = "gmail.com"
    return f"{local}@{domain}"


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


def reply_address(message: Message) -> str:
    """Where a reply should be sent: Reply-To, otherwise From."""
    for address in header_addresses(message, "Reply-To"):
        if address:
            return address
    return sender_address(message)


def display_from(message: Message) -> str:
    """The From header as written, falling back to the bare address."""
    value = str(message.get("From") or "").strip()
    return value or sender_address(message)


def subject_of(message: Message) -> str:
    return " ".join(str(message.get("Subject") or "").split())


def canonical_message_id(value: str) -> str:
    """Message-IDs as `<id@host>`, so In-Reply-To matches what we stored."""
    token = " ".join(str(value or "").split())
    if not token or token.startswith("sha256:"):
        return token
    if not token.startswith("<"):
        token = f"<{token}"
    if not token.endswith(">"):
        token = f"{token}>"
    return token


def message_id_of(message: Message) -> str:
    return canonical_message_id(str(message.get("Message-ID") or message.get("Message-Id") or ""))


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
                token = canonical_message_id("".join(current))
                if token and token != "<>":
                    found.append(token)
                current = []
    if not found:
        token = canonical_message_id(value)
        if token and "@" in token and " " not in token.strip("<>"):
            found.append(token)
    return found


def reference_chain(*parts: str) -> str:
    """Unique Message-IDs in order, for a References header."""
    seen: set[str] = set()
    ordered: list[str] = []
    for part in parts:
        for identifier in _split_message_ids(part) if part else []:
            if identifier not in seen:
                seen.add(identifier)
                ordered.append(identifier)
    return " ".join(ordered)


def in_reply_to_ids(message: Message) -> list[str]:
    """Message-IDs named by In-Reply-To, in order."""
    found: list[str] = []
    seen: set[str] = set()
    for raw in message.get_all("In-Reply-To", []):
        for identifier in _split_message_ids(str(raw)):
            if identifier not in seen:
                seen.add(identifier)
                found.append(identifier)
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


def _part_of(message: Message, subtype: str) -> str:
    """The first text part of this subtype, or empty."""
    if isinstance(message, EmailMessage):
        part = message.get_body(preferencelist=(subtype,))
        if part is not None:
            text = _part_text(part)
            if text:
                return text
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() != "text":
                continue
            if part.get_content_subtype() == subtype:
                text = _part_text(part)
                if text:
                    return text
        return ""
    if message.get_content_subtype() == subtype:
        return _part_text(message)
    return ""


def _drop_quoted_html(html: str) -> str:
    """Remove the previous message as mail clients wrap it, leaving this message's HTML."""
    soup = BeautifulSoup(html, "html.parser")
    # These markers sit *above* the quoted thread rather than wrapping it.
    for node in soup.select(
        '[id$="divRplyFwdMsg"], #appendonsend, .moz-cite-prefix, .OutlookMessageHeader'
    ):
        for sibling in list(node.next_siblings):
            sibling.extract()
        node.decompose()
    for node in soup.select(
        ".gmail_quote, .gmail_quote_container, .gmail_extra, "
        "blockquote[type=cite], .yahoo_quoted, .protonmail_quote"
    ):
        node.decompose()
    root = soup.body if soup.body is not None else soup
    return root.decode_contents()


def _markdown_from_html(html: str) -> str:
    return str(
        markdownify(
            html,
            heading_style="ATX",
            strip=["script", "style", "head", "meta", "title", "img"],
        )
        or ""
    ).strip()


def _unquoted_plain(text: str) -> str:
    """text/plain with RFC 3676 quote lines dropped, used only when the message has no HTML."""
    kept: list[str] = []
    for line in text.splitlines():
        if not line.startswith(">"):
            kept.append(line)
    return "\n".join(kept).strip()


def turn_body(message: Message) -> str:
    """This email's body as markdown: HTML when present, otherwise unquoted text/plain.

    HTML is the body even when a plaintext part exists. If that HTML is only the quoted
    thread, the unquoted plaintext part is used so the new message is not dropped.
    """
    html = _part_of(message, "html")
    if html:
        prose = _markdown_from_html(_drop_quoted_html(html))
        if prose:
            return prose
    plain = _part_of(message, "plain")
    if plain:
        return _unquoted_plain(plain)
    return ""


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
    """Whether `address` is on the allow-list: an exact mailbox, a Gmail alias of one, or `@domain`.

    Gmail plus-addresses and googlemail.com are the same account, matching how IMAP login
    already authenticates `user+tag@gmail.com` as `user@gmail.com`.
    """
    mailbox = address.strip().lower()
    if not mailbox or "@" not in mailbox:
        return False
    sender = canonical_mailbox(mailbox)
    _, sender_domain = sender.rsplit("@", 1)
    raw_domain = mailbox.rsplit("@", 1)[1]
    for entry in allow_from:
        token = entry.strip().lower()
        if not token:
            continue
        listed_domain = ""
        if token.startswith("*@"):
            listed_domain = token[2:]
        elif token.startswith("@"):
            listed_domain = token[1:]
        if listed_domain:
            listed = canonical_mailbox(f"placeholder@{listed_domain}").rsplit("@", 1)[1]
            if sender_domain == listed or raw_domain == listed_domain:
                return True
            continue
        if token == mailbox or canonical_mailbox(token) == sender:
            return True
    return False
