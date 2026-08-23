"""Whether a GitHub comment is a mention turn. Stdlib only, so the ack step can use it.

The Action posts the acknowledgement before the venv exists. A comment starts a turn
when it addresses the bot, or when it is a reply to one of the bot's comments — a
quote-reply, a review reply, or the comment immediately after the bot.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Mapping

MENTION = "@langmesh[bot]"
MENTION_ALIASES = (MENTION, "@langmesh")
# `LangMesh` cannot be a GitHub App (reserved for @langmesh). Accept @langmesh-…[bot].
_HYPHENATED_BOT = re.compile(r"@langmesh-[\w-]+\[bot\](?![\w-])", re.IGNORECASE)


def mention_handles() -> tuple[str, ...]:
    """`@langmesh[bot]` first, then `@langmesh`, then an optional custom handle."""
    extra = (os.environ.get("LANGMESH_MENTION") or "").strip()
    handles: list[str] = list(MENTION_ALIASES)
    if extra and extra.lower() not in {name.lower() for name in handles}:
        handles.insert(0, extra)
    return tuple(handles)


def mentioned(body: str) -> bool:
    """Whether the comment addressed the bot, not some longer `@langmesh…` login.

    `@langmesh-agent[bot]` and other `@langmesh-…[bot]` slugs count even when
    ``LANGMESH_MENTION`` is unset, because ``LangMesh`` itself cannot be an App.
    """
    text = body.lower()
    for handle in mention_handles():
        if re.search(re.escape(handle.lower()) + r"(?![\w-])", text):
            return True
    return _HYPHENATED_BOT.search(text) is not None


def mention_bot_login(login: str) -> bool:
    """Whether this login is the mention job's bot, not some other App."""
    name = (login or "").strip()
    if not name.endswith("[bot]"):
        return False
    handle = f"@{name}"
    if any(handle.lower() == item.lower() for item in mention_handles()):
        return True
    if name.lower() == "github-actions[bot]":
        return True
    return bool(re.fullmatch(r"langmesh(?:-[\w-]+)?\[bot\]", name, flags=re.IGNORECASE))


def _get(url: str, token: str) -> Any:
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "langmesh",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"GitHub API {error.code}: {error.read().decode()[:500]}") from error
    return json.loads(body) if body else {}


def _quoted_lines(body: str) -> list[str]:
    return [line[1:].strip() for line in body.splitlines() if line.startswith(">")]


def _thread_comments(
    repository: str, number: int, token: str, api: str
) -> list[dict[str, Any]]:
    raw = _get(
        f"{api}/repos/{repository}/issues/{number}/comments?per_page=100"
        "&sort=created&direction=desc",
        token,
    )
    rows = [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []
    rows.reverse()
    return rows


def reply_to_mention_bot(
    event: Mapping[str, Any],
    *,
    repository: str,
    token: str,
    api: str,
) -> bool:
    """A quote-reply, review reply, or the comment immediately after the mention bot."""
    if not token:
        return False
    comment = event.get("comment") or {}
    body = str(comment.get("body") or "")
    parent_id = comment.get("in_reply_to_id")
    if parent_id:
        for path in (
            f"issues/comments/{int(parent_id)}",
            f"pulls/comments/{int(parent_id)}",
        ):
            try:
                record = _get(f"{api}/repos/{repository}/{path}", token)
            except (RuntimeError, TypeError, ValueError):
                continue
            login = str((record.get("user") or {}).get("login") or "")
            if mention_bot_login(login):
                return True
    number = (event.get("issue") or {}).get("number") or (
        event.get("pull_request") or {}
    ).get("number")
    this_id = comment.get("id")
    if not number or not this_id:
        return False
    try:
        rows = _thread_comments(repository, int(number), token, api)
    except (RuntimeError, TypeError, ValueError):
        return False
    quotes = [line for line in _quoted_lines(body) if len(line) >= 8]
    previous = None
    for row in rows:
        if int(row.get("id") or 0) == int(this_id):
            break
        previous = row
        if quotes and mention_bot_login(str((row.get("user") or {}).get("login") or "")):
            text = str(row.get("body") or "")
            if any(line in text for line in quotes):
                return True
    if previous is None:
        return False
    return mention_bot_login(str((previous.get("user") or {}).get("login") or ""))


def is_mention_turn(
    event: Mapping[str, Any],
    *,
    repository: str,
    token: str,
    api: str,
) -> bool:
    """Whether this comment is a mention or a reply the Action should answer."""
    comment = event.get("comment") or {}
    if str((comment.get("user") or {}).get("login") or "").endswith("[bot]"):
        return False
    body = str(comment.get("body") or "")
    return mentioned(body) or reply_to_mention_bot(
        event, repository=repository, token=token, api=api
    )
