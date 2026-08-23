"""The GitHub mention Action answers only the mentions it should, and never pushes main."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from langmesh.github.mention import (
    Mention,
    api_key_for,
    current_branch,
    mention_from_event,
    model_identifier_from_env,
    prompt_for,
    publish_changes,
    render,
    tree_is_dirty,
)


def _comment_event(
    *,
    body: str = "please @langmesh fix the flaky test",
    association: str = "OWNER",
    login: str = "owner",
    number: int = 12,
    pull: bool = False,
    title: str = "Flaky test",
    default_branch: str = "main",
    repository: str = "ghovax/langmesh",
) -> dict[str, Any]:
    issue: dict[str, Any] = {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/{repository}/issues/{number}",
    }
    if pull:
        issue["pull_request"] = {"url": f"https://api.github.com/repos/{repository}/pulls/{number}"}
        issue["html_url"] = f"https://github.com/{repository}/pull/{number}"
    return {
        "comment": {
            "body": body,
            "user": {"login": login},
            "author_association": association,
        },
        "issue": issue,
        "repository": {"default_branch": default_branch},
    }


def test_ignores_comments_without_a_mention() -> None:
    assert mention_from_event(_comment_event(body="hello"), repository="ghovax/langmesh") is None


def test_ignores_bot_comments() -> None:
    event = _comment_event(login="github-actions[bot]")
    assert mention_from_event(event, repository="ghovax/langmesh") is None


def test_owners_on_an_issue_are_answered() -> None:
    mention = mention_from_event(_comment_event(), repository="ghovax/langmesh")
    assert mention is not None
    assert mention.kind == "issue"
    assert mention.session_id == "github:ghovax/langmesh:issue:12"
    assert mention.topic_branch == "langmesh/issue-12"
    assert mention.allowed


def test_mention_match_is_case_insensitive() -> None:
    event = _comment_event(body="Hey @LangMesh, look at this")
    mention = mention_from_event(event, repository="ghovax/langmesh")
    assert mention is not None


def test_outsiders_are_not_allowed() -> None:
    event = _comment_event(association="FIRST_TIME_CONTRIBUTOR", login="stranger")
    mention = mention_from_event(event, repository="ghovax/langmesh")
    assert mention is not None
    assert not mention.allowed


def test_same_repository_pull_request_is_allowed() -> None:
    event = _comment_event(pull=True)
    mention = mention_from_event(
        event,
        repository="ghovax/langmesh",
        pull={
            "head": {"ref": "feature", "repo": {"full_name": "ghovax/langmesh"}},
            "title": "Flaky test",
        },
    )
    assert mention is not None
    assert mention.kind == "pull"
    assert mention.head_ref == "feature"
    assert mention.allowed
    assert mention.session_id == "github:ghovax/langmesh:pull:12"


def test_fork_pull_requests_are_not_allowed() -> None:
    event = _comment_event(pull=True)
    mention = mention_from_event(
        event,
        repository="ghovax/langmesh",
        pull={
            "head": {"ref": "feature", "repo": {"full_name": "stranger/langmesh"}},
        },
    )
    assert mention is not None
    assert mention.is_fork
    assert not mention.allowed


def test_follow_up_on_the_same_thread_reuses_the_session_id() -> None:
    first = mention_from_event(_comment_event(body="@langmesh start"), repository="ghovax/langmesh")
    second = mention_from_event(
        _comment_event(body="@langmesh continue with tests"),
        repository="ghovax/langmesh",
    )
    assert first is not None and second is not None
    assert first.session_id == second.session_id


def test_prompt_includes_the_thread_and_the_comment() -> None:
    mention = mention_from_event(_comment_event(), repository="ghovax/langmesh")
    assert mention is not None
    text = prompt_for(mention)
    assert "Flaky test" in text
    assert "@langmesh" in text
    assert mention.html_url in text


def test_system_prompt_and_replies_come_from_markdown() -> None:
    assert "Do not git push" in render("system")
    assert render("empty_reply") == "Done."
    assert "provider/model" in render("invalid_model", {"value": "gpt-4"})
    assert "boom" in render("turn_failed", {"error": "boom"})


def test_model_identifier_splits_on_the_first_slash() -> None:
    assert model_identifier_from_env({}) == ("anthropic", "claude-sonnet-4-5")
    assert model_identifier_from_env(
        {"LANGMESH_MODEL": "openrouter/anthropic/claude-sonnet-4-5"}
    ) == (
        "openrouter",
        "anthropic/claude-sonnet-4-5",
    )


def test_model_identifier_rejects_a_value_without_a_slash() -> None:
    with pytest.raises(ValueError, match="gpt-4"):
        model_identifier_from_env({"LANGMESH_MODEL": "gpt-4"})


def test_api_key_is_not_tied_to_a_provider_list() -> None:
    assert api_key_for("anthropic", {"LANGMESH_API_KEY": "sk-any"}) == "sk-any"
    assert api_key_for("groq", {"GROQ_API_KEY": "g"}) == "g"
    assert api_key_for("anthropic", {"OPENAI_API_KEY": "ignored"}) == ""


def test_publish_refuses_the_default_branch() -> None:
    mention = Mention(
        body="@langmesh",
        number=1,
        kind="issue",
        title="x",
        html_url="https://example.test/1",
        user="owner",
        association="OWNER",
        default_branch="main",
        repository="ghovax/langmesh",
    )

    def fake_run(arguments: list[str], **_kwargs: object) -> str:
        if arguments[:2] == ["git", "rev-parse"]:
            return "main\n"
        return ""

    with pytest.raises(RuntimeError, match="protected branch"):
        publish_changes(mention, workspace=Path("/tmp"), token="t", run=fake_run)


def test_tree_is_dirty_when_cached_diff_is_nonempty(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(arguments: list[str], **_kwargs: object) -> str:
        calls.append(arguments)
        if arguments[:3] == ["git", "diff", "--cached"]:
            raise subprocess.CalledProcessError(1, arguments)
        return ""

    assert tree_is_dirty(tmp_path, run=fake_run)
    assert ["git", "reset", "-q", "--", ".langmesh-github"] in calls


def test_review_comment_uses_the_pull_request_payload() -> None:
    event = {
        "comment": {
            "body": "@langmesh please",
            "user": {"login": "owner"},
            "author_association": "MEMBER",
        },
        "pull_request": {
            "number": 7,
            "title": "API",
            "html_url": "https://github.com/ghovax/langmesh/pull/7",
            "head": {"ref": "api", "repo": {"full_name": "ghovax/langmesh"}},
        },
        "repository": {"default_branch": "main"},
    }
    mention = mention_from_event(event, repository="ghovax/langmesh")
    assert mention is not None
    assert mention.kind == "pull"
    assert mention.head_ref == "api"
    assert mention.session_id == "github:ghovax/langmesh:pull:7"
    assert mention.allowed


def test_current_branch_strips_whitespace(tmp_path: Path) -> None:
    def fake_run(arguments: list[str], **_kwargs: object) -> str:
        return "langmesh/issue-12\n"

    assert current_branch(tmp_path, run=fake_run) == "langmesh/issue-12"


def test_prepare_tree_checks_out_an_existing_topic_branch(tmp_path: Path) -> None:
    from langmesh.github.mention import prepare_tree

    mention = mention_from_event(_comment_event(), repository="ghovax/langmesh")
    assert mention is not None
    calls: list[list[str]] = []
    headers: list[str] = []

    def fake_run(arguments: list[str], extraheader: str = "", **_kwargs: object) -> str:
        calls.append(arguments)
        headers.append(extraheader)
        if arguments[:3] == ["git", "ls-remote"]:
            return "abc123\trefs/heads/langmesh/issue-12\n"
        return ""

    prepare_tree(mention, tmp_path, token="t", run=fake_run)
    assert ["git", "checkout", "-B", "langmesh/issue-12", "FETCH_HEAD"] in calls
    assert any(header.startswith("AUTHORIZATION: bearer") for header in headers)
    assert all(
        argument != "http.extraheader=AUTHORIZATION: bearer t"
        for call in calls
        for argument in call
    )
