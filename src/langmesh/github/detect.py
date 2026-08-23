"""Whether a GitHub comment is a mention turn. Stdlib only, so the ack step can use it.

The Action posts the acknowledgement before the venv exists. A comment starts a turn
when it addresses the bot, or when it is a reply to one of the bot's comments — a
review reply (`in_reply_to_id`) or the comment immediately after the bot.

Reply detection follows those pointers only: one parent comment, or the two most
recent comments on the same collection. It does not load the thread into memory.
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


def _review_comment(comment: Mapping[str, Any]) -> bool:
    return comment.get("pull_request_review_id") is not None or comment.get("diff_hunk") is not None


def _previous_comment(
    event: Mapping[str, Any],
    *,
    repository: str,
    token: str,
    api: str,
) -> dict[str, Any] | None:
    """The comment immediately before this one on the same collection, or ``None``."""
    comment = event.get("comment") or {}
    this_id = comment.get("id")
    number = (event.get("issue") or {}).get("number") or (
        event.get("pull_request") or {}
    ).get("number")
    if not number or not this_id:
        return None
    collection = "pulls" if _review_comment(comment) else "issues"
    raw = _get(
        f"{api}/repos/{repository}/{collection}/{int(number)}/comments"
        "?per_page=2&sort=created&direction=desc",
        token,
    )
    rows = [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []
    if not rows:
        return None
    if int(rows[0].get("id") or 0) == int(this_id):
        return rows[1] if len(rows) > 1 else None
    return rows[0]


def reply_to_mention_bot(
    event: Mapping[str, Any],
    *,
    repository: str,
    token: str,
    api: str,
) -> bool:
    """A review reply or the comment immediately after the mention bot."""
    if not token:
        return False
    comment = event.get("comment") or {}
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
    try:
        previous = _previous_comment(event, repository=repository, token=token, api=api)
    except (RuntimeError, TypeError, ValueError):
        return False
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
