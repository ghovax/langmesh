"""GitHub mention state matrix: one row per situation, fail the lot at the end."""

from __future__ import annotations

from typing import Any

from langmesh.github.mention import (
    api_key_for,
    mention_features,
    mention_from_event,
    model_identifier_from_env,
    posted_reply,
    prompt_for,
    render,
)
from langmesh.github.reply import GitHubReply
from langmesh.runtime.features.seam import Feature, Features
from langmesh.runtime.plugins.compaction import Compaction, KeepRecentTurns

REPO = "ghovax/langmesh"
failures: list[str] = []


def check(situation: str, got: object, expected: object) -> None:
    if got != expected:
        failures.append(f"{situation}: got {got!r}, expected {expected!r}")


def issue(
    *,
    body: str = "please @langmesh fix the flaky test",
    login: str = "owner",
    association: str = "OWNER",
    number: int | None = 12,
    pull: bool = False,
    title: str = "Flaky test",
    default_branch: str = "main",
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "title": title,
        "html_url": f"https://github.com/{REPO}/issues/{number}",
    }
    if number is not None:
        record["number"] = number
    if pull:
        record["pull_request"] = {"url": f"https://api.github.com/repos/{REPO}/pulls/{number}"}
        record["html_url"] = f"https://github.com/{REPO}/pull/{number}"
    return {
        "comment": {
            "body": body,
            "user": {"login": login},
            "author_association": association,
        },
        "issue": record,
        "repository": {"default_branch": default_branch},
    }


def seen(
    *,
    kind: str,
    allowed: bool,
    number: int = 12,
    is_fork: bool = False,
    head_ref: str = "",
    title: str = "Flaky test",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "allowed": allowed,
        "session_id": f"github:{REPO}:{kind}:{number}",
        "topic_branch": f"langmesh/{kind}-{number}",
        "is_fork": is_fork,
        "head_ref": head_ref,
        "title": title,
    }


def observe(event: dict[str, Any], pull: dict[str, Any] | None = None) -> dict[str, Any] | None:
    mention = mention_from_event(event, repository=REPO, pull=pull)
    if mention is None:
        return None
    return {
        "kind": mention.kind,
        "allowed": mention.allowed,
        "session_id": mention.session_id,
        "topic_branch": mention.topic_branch,
        "is_fork": mention.is_fork,
        "head_ref": mention.head_ref,
        "title": mention.title,
    }


def run_mention_matrix() -> None:
    same = {"head": {"ref": "feature", "repo": {"full_name": REPO}}}
    fork = {"head": {"ref": "feature", "repo": {"full_name": "stranger/langmesh"}}}
    review = {
        "comment": {
            "body": "@langmesh please",
            "user": {"login": "owner"},
            "author_association": "MEMBER",
        },
        "pull_request": {
            "number": 7,
            "title": "API",
            "html_url": f"https://github.com/{REPO}/pull/7",
            "head": {"ref": "api", "repo": {"full_name": REPO}},
        },
        "repository": {"default_branch": "main"},
    }
    rows: list[tuple[str, dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]] = [
        ("no @langmesh", issue(body="hello"), None, None),
        ("empty body", issue(body=""), None, None),
        ("bot comment", issue(login="github-actions[bot]"), None, None),
        ("dependabot", issue(login="dependabot[bot]"), None, None),
        ("missing number", issue(number=None), None, None),
        ("number zero", issue(number=0), None, None),
        ("owner on an issue", issue(), None, seen(kind="issue", allowed=True)),
        (
            "case-insensitive mention",
            issue(body="Hey @LangMesh, look"),
            None,
            seen(kind="issue", allowed=True),
        ),
        ("member", issue(association="MEMBER"), None, seen(kind="issue", allowed=True)),
        ("collaborator", issue(association="COLLABORATOR"), None, seen(kind="issue", allowed=True)),
        (
            "first-time contributor",
            issue(association="FIRST_TIME_CONTRIBUTOR", login="stranger"),
            None,
            seen(kind="issue", allowed=False),
        ),
        ("contributor", issue(association="CONTRIBUTOR"), None, seen(kind="issue", allowed=False)),
        ("empty association", issue(association=""), None, seen(kind="issue", allowed=False)),
        (
            "same-repository pull request",
            issue(pull=True),
            same,
            seen(kind="pull", allowed=True, head_ref="feature"),
        ),
        (
            "fork pull request",
            issue(pull=True),
            fork,
            seen(kind="pull", allowed=False, is_fork=True, head_ref="feature"),
        ),
        (
            "pull without head yet",
            issue(pull=True),
            None,
            seen(kind="pull", allowed=True),
        ),
        (
            "review comment",
            review,
            None,
            seen(kind="pull", allowed=True, number=7, head_ref="api", title="API"),
        ),
    ]
    for situation, payload, pull, expected in rows:
        check(situation, observe(payload, pull), expected)
    first = mention_from_event(issue(body="@langmesh start"), repository=REPO)
    second = mention_from_event(issue(body="@langmesh continue"), repository=REPO)
    check(
        "follow-up session id",
        first is not None and second is not None and first.session_id == second.session_id,
        True,
    )
    mention = mention_from_event(issue(), repository=REPO)
    assert mention is not None
    text = prompt_for(mention)
    check("turn includes title", "Flaky test" in text, True)
    check("turn includes mention", "@langmesh" in text, True)
    check("turn includes url", mention.html_url in text, True)


def run_model_and_reply_matrix() -> None:
    check("default model", model_identifier_from_env({}), ("anthropic", "claude-sonnet-4-5"))
    check(
        "first-slash split",
        model_identifier_from_env({"LANGMESH_MODEL": "openrouter/anthropic/claude-sonnet-4-5"}),
        ("openrouter", "anthropic/claude-sonnet-4-5"),
    )
    for situation, environ in (
        ("missing slash", {"LANGMESH_MODEL": "gpt-4"}),
        ("empty provider", {"LANGMESH_MODEL": "/claude"}),
        ("empty model", {"LANGMESH_MODEL": "anthropic/"}),
        ("whitespace model", {"LANGMESH_MODEL": "  /  "}),
    ):
        try:
            model_identifier_from_env(environ)
        except ValueError:
            continue
        failures.append(f"{situation}: expected ValueError")
    check(
        "blank LANGMESH_MODEL uses default",
        model_identifier_from_env({"LANGMESH_MODEL": "  "}),
        ("anthropic", "claude-sonnet-4-5"),
    )
    check("LANGMESH_API_KEY", api_key_for("anthropic", {"LANGMESH_API_KEY": "sk-any"}), "sk-any")
    check("provider env", api_key_for("groq", {"GROQ_API_KEY": "g"}), "g")
    check("hyphenated provider env", api_key_for("open-router", {"OPEN_ROUTER_API_KEY": "x"}), "x")
    check("wrong provider env ignored", api_key_for("anthropic", {"OPENAI_API_KEY": "ignored"}), "")
    check(
        "LANGMESH_API_KEY wins",
        api_key_for("anthropic", {"LANGMESH_API_KEY": "a", "ANTHROPIC_API_KEY": "b"}),
        "a",
    )
    check("empty reply", posted_reply(""), "Done.")
    check("stripped reply", posted_reply("  All green.  "), "All green.")
    check(
        "reply with pull",
        posted_reply("All green.", "https://example.test/pr"),
        "All green.\n\nhttps://example.test/pr",
    )
    prompt = render("system")
    check("system names the comment tool", "submit_github_comment" in prompt, True)
    check("system forbids push", "Do not git push" in prompt, True)
    check(
        "invalid model prompt",
        "provider/model" in render("invalid_model", {"value": "gpt-4"}),
        True,
    )

    idle = GitHubReply()
    check("idle comment", idle.comment, None)
    check("idle complete", idle.should_complete_turn(), False)
    reminder = idle.incomplete_reminder()
    check(
        "idle reminder names the tool",
        reminder is not None and "submit_github_comment" in reminder,
        True,
    )
    check("idle reminder is stable", idle.incomplete_reminder(), reminder)
    check("idle snapshot", idle.snapshot(), None)
    idle.submit("Fixed the flake.")
    check("submitted comment", idle.comment, "Fixed the flake.")
    check("submitted complete", idle.should_complete_turn(), True)
    check("submitted reminder", idle.incomplete_reminder(), None)
    check("submitted snapshot", idle.snapshot(), None)
    empty = GitHubReply()
    empty.submit("")
    check("empty submit still completes", empty.should_complete_turn(), True)
    check("empty submit is stored", empty.comment, "")

    class First(Feature):
        def incomplete_reminder(self) -> str | None:
            return "first"

    class Second(Feature):
        def incomplete_reminder(self) -> str | None:
            return "second"

        def should_complete_turn(self) -> bool:
            return True

    features = Features([First(), Second()])
    check("reminder first-wins", features.incomplete_reminder(), "first")
    check("complete any-wins", features.should_complete_turn(), True)
    check("empty features reminder", Features().incomplete_reminder(), None)
    check("empty features complete", Features().should_complete_turn(), False)

    reply = GitHubReply()
    composed = mention_features(reply)
    compaction = next(feature for feature in composed if isinstance(feature, Compaction))
    check("keep recent turns", isinstance(compaction._strategy, KeepRecentTurns), True)
    check("reply is composed", reply in composed, True)
    check("comment tool contributed", reply.contribute_tools()[0].name, "submit_github_comment")


def main() -> int:
    run_mention_matrix()
    run_model_and_reply_matrix()
    if failures:
        print("\n".join(failures))
        return 1
    print("github mention matrix: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
