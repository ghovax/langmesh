"""Which machine an inbound message is for: plus-tag on a new thread, thread map on a reply.

One mailbox can serve several hosts (``agent+vps@`` vs ``agent+laptop@``). A new thread
is a new session on the host named in the plus-tag. A reply is steering into the
conversation this host already mapped. Foreign and untagged new mail is left UNSEEN
so the host that owns it can take it.
"""

from __future__ import annotations

from email.message import Message

from langmeshd.mail.body import canonical_mailbox, header_addresses, plus_tag

# Headers that can carry the envelope recipient after a forward or alias rewrite.
RECIPIENT_HEADERS = (
    "To",
    "Cc",
    "Delivered-To",
    "X-Original-To",
    "X-Forwarded-To",
    "Envelope-To",
)

SKIP_OTHER_MACHINE = "other-machine"
SKIP_UNADDRESSED = "unaddressed"
# Persist the skip so this host does not keep re-reading the mail, but do not set IMAP
# ``\Seen``: the host that was addressed still needs UNSEEN to find it.
UNSEEN_SKIP_REASONS = frozenset({SKIP_OTHER_MACHINE, SKIP_UNADDRESSED})


def addressed_tags(message: Message, mailbox_address: str) -> set[str]:
    """Plus-tags on recipient headers that belong to this mailbox, not other people."""
    ours = canonical_mailbox(mailbox_address)
    if not ours:
        return set()
    tags: set[str] = set()
    for header in RECIPIENT_HEADERS:
        for address in header_addresses(message, header):
            if canonical_mailbox(address) != ours:
                continue
            tag = plus_tag(address)
            if tag:
                tags.add(tag)
    return tags


def route(
    message: Message,
    *,
    mailbox_address: str,
    machine: str,
    owned_thread: bool,
) -> str | None:
    """Why this host should skip the message, or ``None`` to take it.

    A reply already mapped on this host is steering, even when the plus-tag is missing.
    A plus-tag for a different machine still yields to that host. A new thread is
    accepted only when the plus-tag names this machine; missing tags are not guessed.
    """
    slug = machine.strip().lower()
    tags = addressed_tags(message, mailbox_address)
    ours = bool(slug and slug in tags)
    foreign = {tag for tag in tags if tag != slug}

    if owned_thread:
        if foreign and not ours:
            return SKIP_OTHER_MACHINE
        return None
    if ours:
        return None
    if foreign:
        return SKIP_OTHER_MACHINE
    return SKIP_UNADDRESSED


__all__ = [
    "RECIPIENT_HEADERS",
    "SKIP_OTHER_MACHINE",
    "SKIP_UNADDRESSED",
    "UNSEEN_SKIP_REASONS",
    "addressed_tags",
    "route",
]
