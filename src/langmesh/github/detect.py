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


COMMENT_EVENTS = frozenset({"issue_comment", "pull_request_review_comment"})


def _review_comment(comment: Mapping[str, Any]) -> bool:
    return comment.get("pull_request_review_id") is not None or comment.get("diff_hunk") is not None


def normalize_comment_event(event: Mapping[str, Any], *, event_name: str = "") -> Mapping[str, Any]:
    """Return one canonical pull-request shape for supported comment webhooks.

    GitHub puts a conversation comment's thread in ``issue`` and an inline review
    comment's thread in ``pull_request``. An issue-comment payload with
    ``issue.pull_request`` is therefore a pull-request event even though it does not
    have a top-level ``pull_request`` object.
    """
    if not isinstance(event, Mapping):
        return {}
    pull_request = event.get("pull_request")
    if isinstance(pull_request, Mapping) and pull_request:
        return event
    issue = event.get("issue")
    if not isinstance(issue, Mapping) or not issue.get("pull_request"):
        return event
    normalized = dict(event)
    normalized["pull_request"] = dict(issue)
    return normalized


def is_pull_request_comment(event: Mapping[str, Any], *, event_name: str = "") -> bool:
    """Whether a comment belongs to a pull request, regardless of webhook shape."""
    return isinstance(
        normalize_comment_event(event, event_name=event_name).get("pull_request"), Mapping
    )


def comment_number(event: Mapping[str, Any], *, event_name: str = "") -> int | None:
    """Return the thread number from either supported comment payload shape."""
    normalized = normalize_comment_event(event, event_name=event_name)
    issue = normalized.get("issue")
    pull_request = normalized.get("pull_request")
    if not isinstance(issue, Mapping):
        issue = {}
    if not isinstance(pull_request, Mapping):
        pull_request = {}
    number = issue.get("number") or pull_request.get("number")
    try:
        return int(number) if number else None
    except (TypeError, ValueError):
        return None


def comment_collection(event: Mapping[str, Any], *, event_name: str = "") -> str:
    """Return GitHub's API collection for this comment's thread.

    Pull-request conversation comments remain issue comments in GitHub's API; only
    inline review comments use the pull-request comments collection.
    """
    comment = event.get("comment")
    if not isinstance(comment, Mapping):
        comment = {}
    if event_name == "pull_request_review_comment" or _review_comment(comment):
        return "pulls"
    return "issues"


def _is_human_actor(actor: Any) -> bool:
    """Whether a GitHub actor has enough identity data and is not a bot."""
    if not isinstance(actor, Mapping):
        return False
    login = str(actor.get("login") or "").strip()
    if not login:
        return False
    actor_type = str(actor.get("type") or "").strip().lower()
    return actor_type != "bot" and not login.lower().endswith("[bot]")


def mention_bot_login(login: str, *, bot_login: str) -> bool:
    """Whether this login is the mention job's bot, not some other App."""
    name = (login or "").strip()
    if not name.endswith("[bot]"):
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


def thread_has_prior_bot_comment(
    event: Mapping[str, Any],
    *,
    repository: str,
    token: str,
    api: str,
    bot_login: str,
    ignore_ids: tuple[int, ...] = (),
    event_name: str = "",
) -> bool:
    """Whether the mention bot already wrote on this collection before this comment.

    Looks at the ten most recent comments on the same collection (issue comments or
    review comments). It does not load the thread body. ``ignore_ids`` skips comments
    this job just posted, so the acknowledgement is not treated as an earlier turn.
    """
    if not token:
        return False
    comment = event.get("comment")
    if not isinstance(comment, Mapping):
        comment = {}
    this_id = comment.get("id")
    number = comment_number(event, event_name=event_name)
    if not number:
        return False
    ignored: set[int] = set()
    for item in (this_id, *ignore_ids):
        try:
            if item:
                ignored.add(int(item))
        except (TypeError, ValueError):
            continue
    collection = comment_collection(event, event_name=event_name)
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
        try:
            row_id = int(row.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if row_id in ignored:
            continue
        user = row.get("user")
        login = str(user.get("login") or "") if isinstance(user, Mapping) else ""
        if mention_bot_login(login, bot_login=bot_login):
            return True
    return False


def is_mention_turn(
    event: Mapping[str, Any],
    *,
    event_name: str = "",
) -> bool:
    """Whether a supported, newly created human GitHub event starts a turn."""
    if not isinstance(event, Mapping):
        return False
    event = normalize_comment_event(event, event_name=event_name)
    action = event.get("action")
    if event_name in {"issues", "pull_request"}:
        if action != "opened":
            return False
        source = event.get("issue") if event_name == "issues" else event.get("pull_request")
        return isinstance(source, Mapping) and _is_human_actor(source.get("user"))
    if event_name and event_name not in COMMENT_EVENTS:
        return False
    if action != "created":
        return False
    comment = event.get("comment")
    if not isinstance(comment, Mapping) or not _is_human_actor(comment.get("user")):
        return False
    return True
