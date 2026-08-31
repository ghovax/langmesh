from langmesh.github.detect import is_mention_turn


def comment_event(*, body: str = "", user: str = "octocat", action: str = "created") -> dict:
    return {
        "action": action,
        "comment": {"body": body, "user": {"login": user}},
    }


def test_human_issue_comment_starts_a_turn_without_a_trigger() -> None:
    assert is_mention_turn(comment_event(body="Please fix this"), event_name="issue_comment")


def test_human_review_comment_starts_a_turn_without_a_trigger() -> None:
    assert is_mention_turn(
        comment_event(body="This line needs changing"),
        event_name="pull_request_review_comment",
    )


def test_non_creation_and_bot_comments_are_ignored() -> None:
    assert not is_mention_turn(
        comment_event(body="Please fix this", action="edited"),
        event_name="issue_comment",
    )
    assert not is_mention_turn(
        comment_event(body="Please fix this", user="langmesh-agent[bot]"),
        event_name="issue_comment",
    )


def test_human_issue_and_pull_request_openings_still_start_turns() -> None:
    issue = {"action": "opened", "issue": {"user": {"login": "octocat"}}}
    pull_request = {"action": "opened", "pull_request": {"user": {"login": "octocat"}}}
    assert is_mention_turn(issue, event_name="issues")
    assert is_mention_turn(pull_request, event_name="pull_request")
