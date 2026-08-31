"""Whether a GitHub event is a turn for the installed agent.

An issue or pull request opening starts a turn automatically. Every later human
comment starts a turn as well. Bot-authored comments and non-creation events are
ignored.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping


def mention_bot_login(login: str, *, bot_login: str) -> bool:
    """Whether this login belongs to this GitHub App, not some other App."""
    name = (login or "").strip()
    if not name.lower().endswith("[bot]"):
        return False
    return name.lower() == bot_login.lower()


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
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"GitHub API {error.code}: {error.read().decode()[:500]}") from error
    return json.loads(body) if body else {}


def _review_comment(comment: Mapping[str, Any]) -> bool:
    return comment.get("pull_request_review_id") is not None or comment.get("diff_hunk") is not None


def thread_has_prior_bot_comment(
    event: Mapping[str, Any],
    *,
    repository: str,
    token: str,
    api: str,
    bot_login: str,
    ignore_ids: tuple[int, ...] = (),
) -> bool:
    """Whether the mention bot already wrote on this collection before this comment.

    Looks at the ten most recent comments on the same collection (issue comments or
    review comments). It does not load the thread body. ``ignore_ids`` skips comments
    this job just posted, so the acknowledgement is not treated as an earlier turn.
    """
    if not token:
        return False
    comment = event.get("comment") or {}
    this_id = comment.get("id")
    number = (event.get("issue") or {}).get("number") or (event.get("pull_request") or {}).get(
        "number"
    )
    if not number:
        return False
    ignored = {int(this_id)} if this_id else set()
    ignored.update(int(item) for item in ignore_ids if item)
    collection = "pulls" if _review_comment(comment) else "issues"
    try:
        raw = _get(
            f"{api}/repos/{repository}/{collection}/{int(number)}/comments"
            "?per_page=10&sort=created&direction=desc",
            token,
        )
    except (RuntimeError, TypeError, ValueError):
        return False
    rows = [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []
    for row in rows:
        if int(row.get("id") or 0) in ignored:
            continue
        login = str((row.get("user") or {}).get("login") or "")
        if mention_bot_login(login, bot_login=bot_login):
            return True
    return False


def is_mention_turn(
    event: Mapping[str, Any],
    *,
    event_name: str = "",
    repository: str = "",
    token: str = "",
    api: str = "",
    bot_login: str = "",
) -> bool:
    """Whether this event starts a turn the App should answer."""
    action = event.get("action")
    if event_name in {"issues", "pull_request"}:
        if action != "opened":
            return False
        source = event.get("issue") or event.get("pull_request") or {}
        author = str((source.get("user") or {}).get("login") or "")
        return not author.lower().endswith("[bot]")
    if event_name and event_name not in {"issue_comment", "pull_request_review_comment"}:
        return False
    if action is not None and action != "created":
        return False
    comment = event.get("comment") or {}
    if str((comment.get("user") or {}).get("login") or "").lower().endswith("[bot]"):
        return False
    return True
