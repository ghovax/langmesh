import pytest

from langmesh.github.detect import (
    comment_collection,
    comment_number,
    is_mention_turn,
    is_pull_request_comment,
    normalize_comment_event,
)


BOT = "langmesh-agent[bot]"


def issue_comment(*, body: str = "@claude please help", user: str = "octocat") -> dict:
    return {
        "action": "created",
        "issue": {
            "number": 12,
            "title": "Issue",
            "html_url": "https://github.com/acme/project/issues/12",
            "user": {"login": "octocat"},
        },
        "comment": {"id": 101, "body": body, "user": {"login": user}},
    }


def pull_request_conversation_comment(*, body: str = "@claude please help") -> dict:
    event = issue_comment(body=body)
    event["issue"]["pull_request"] = {"url": "https://api.github.com/repos/acme/project/pulls/12"}
    return event


def pull_request_review_comment(*, body: str = "@claude please help") -> dict:
    return {
        "action": "created",
        "pull_request": {
            "number": 12,
            "title": "Pull request",
            "html_url": "https://github.com/acme/project/pull/12",
            "user": {"login": "octocat"},
        },
        "comment": {
            "id": 202,
            "body": body,
            "diff_hunk": "@@ -1 +1 @@",
            "pull_request_review_id": 303,
            "user": {"login": "octocat"},
        },
    }


@pytest.mark.parametrize(
    ("event", "event_name", "collection"),
    [
        (issue_comment(), "issue_comment", "issues"),
        (pull_request_conversation_comment(), "issue_comment", "issues"),
        (pull_request_review_comment(), "pull_request_review_comment", "pulls"),
    ],
)
def test_supported_comment_events_share_trigger_and_thread_lookup(
    event: dict, event_name: str, collection: str
) -> None:
    assert is_mention_turn(
        event,
        event_name=event_name,
        repository="acme/project",
        token="",
        api="https://api.github.com",
        bot_login=BOT,
    )
    assert comment_number(event, event_name=event_name) == 12
    assert comment_collection(event, event_name=event_name) == collection


@pytest.mark.parametrize(
    ("event", "event_name"),
    [
        (issue_comment(body="Please fix this"), "issue_comment"),
        (pull_request_conversation_comment(body="Please fix this"), "issue_comment"),
        (pull_request_review_comment(body="Please fix this"), "pull_request_review_comment"),
    ],
)
def test_comments_without_trigger_need_a_reply_to_the_bot(
    monkeypatch: pytest.MonkeyPatch, event: dict, event_name: str
) -> None:
    assert not is_mention_turn(
        event,
        event_name=event_name,
        repository="acme/project",
        token="",
        api="https://api.github.com",
        bot_login=BOT,
    )

    requested: list[str] = []

    def get(url: str, token: str) -> list[dict]:
        requested.append(url)
        return [
            event["comment"],
            {"id": 999, "user": {"login": BOT}},
        ]

    monkeypatch.setattr("langmesh.github.detect._get", get)
    assert is_mention_turn(
        event,
        event_name=event_name,
        repository="acme/project",
        token="installation-token",
        api="https://api.github.com",
        bot_login=BOT,
    )
    collection = "pulls" if event_name == "pull_request_review_comment" else "issues"
    assert f"/repos/acme/project/{collection}/12/comments" in requested[0]


def test_conversation_and_review_comments_are_normalized_differently_from_issue_comment() -> None:
    issue = normalize_comment_event(issue_comment(), event_name="issue_comment")
    conversation = normalize_comment_event(
        pull_request_conversation_comment(), event_name="issue_comment"
    )
    review = normalize_comment_event(
        pull_request_review_comment(), event_name="pull_request_review_comment"
    )
    assert not is_pull_request_comment(issue, event_name="issue_comment")
    assert is_pull_request_comment(conversation, event_name="issue_comment")
    assert is_pull_request_comment(review, event_name="pull_request_review_comment")
    assert "pull_request" not in issue
    assert conversation["pull_request"] == conversation["issue"]
    assert review["pull_request"]["number"] == 12


def test_non_creation_and_bot_comments_are_ignored() -> None:
    edited = issue_comment()
    edited["action"] = "edited"
    bot = issue_comment(user=BOT)
    for event in (edited, bot):
        assert not is_mention_turn(
            event,
            event_name="issue_comment",
            repository="acme/project",
            token="",
            api="https://api.github.com",
            bot_login=BOT,
        )
