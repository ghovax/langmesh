import json

import pytest

from langmesh.github.storage import delivery_session_id


@pytest.mark.parametrize(
    ("event_name", "event"),
    [
        (
            "issue_comment",
            {"repository": {"full_name": "acme/project"}, "issue": {"number": 12}},
        ),
        (
            "issue_comment",
            {
                "repository": {"full_name": "acme/project"},
                "issue": {
                    "number": 12,
                    "pull_request": {"url": "https://api.github.com/repos/acme/project/pulls/12"},
                },
            },
        ),
        (
            "pull_request_review_comment",
            {"repository": {"full_name": "acme/project"}, "pull_request": {"number": 12}},
        ),
    ],
)
def test_comment_webhooks_share_the_pull_or_issue_session_key(event_name: str, event: dict) -> None:
    session = delivery_session_id(event_name, json.dumps(event))
    expected_kind = (
        "pull"
        if event_name == "pull_request_review_comment" or "pull_request" in event.get("issue", {})
        else "issue"
    )
    assert session == f"github:acme/project:{expected_kind}:12"
