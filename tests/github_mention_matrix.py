"""GitHub mention state matrix: one row per situation, fail the lot at the end."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Mapping

from langmesh.base.configuration import BashToolConfiguration
from langmesh.base.identity.providers import PROVIDERS, provider_env_vars, resolve_api_key
from langmesh.github.mention import (
    _BASH_DENY,
    api_key_for,
    branch_slug,
    draft_pull_arguments,
    mention_features,
    mention_from_event,
    model_identifier_from_env,
    posted_reply,
    prompt_for,
    publication_note,
    publish_changes,
    render,
    topic_branch_from_agent,
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
    check(
        "issue publication asks the agent to branch",
        "git checkout -b" in publication_note(mention, code="ab12"),
        True,
    )
    check(
        "issue publication gives the suffix",
        "ab12" in publication_note(mention, code="ab12"),
        True,
    )
    check(
        "resumed issue stays on its branch",
        "already on `langmesh/flaky-test-ab12`"
        in publication_note(mention, branch="langmesh/flaky-test-ab12", resumed=True),
        True,
    )
    pull = mention_from_event(issue(pull=True), repository=REPO, pull=same)
    assert pull is not None
    check(
        "pull publication does not open another PR",
        "does not open another pull request" in publication_note(pull),
        True,
    )
    created = draft_pull_arguments(mention, "langmesh/flaky-test-ab12")
    check("create is a draft", "--draft" in created, True)
    check("create uses the issue branch", "langmesh/flaky-test-ab12" in created, True)
    check("create bases on default branch", "main" in created, True)
    check("create is not marked ready", "ready" not in created, True)
    check("slug sanitizes an agent name", branch_slug("Fix: API / JSON"), "fix-api-json")
    check("slug empty name", branch_slug(""), "work")
    check(
        "keeps the agent's topic branch",
        topic_branch_from_agent("langmesh/fix-auth-ab12", code="ab12"),
        "langmesh/fix-auth-ab12",
    )
    check(
        "prefixes and suffixes the agent's name",
        topic_branch_from_agent("fix-auth", code="ab12"),
        "langmesh/fix-auth-ab12",
    )
    check(
        "main falls back to work",
        topic_branch_from_agent("main", code="ab12"),
        "langmesh/work-ab12",
    )


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
    check("alibaba catalogue env", api_key_for("alibaba", {"DASHSCOPE_API_KEY": "d"}), "d")
    check("google gemini env", api_key_for("google", {"GEMINI_API_KEY": "g"}), "g")
    check("google api env", api_key_for("google", {"GOOGLE_API_KEY": "g"}), "g")
    check("meta llama env", api_key_for("meta_llama", {"LLAMA_API_KEY": "l"}), "l")
    check("conventional fallback", api_key_for("alibaba", {"ALIBABA_API_KEY": "a"}), "a")
    check("hyphenated conventional", api_key_for("open-router", {"OPEN_ROUTER_API_KEY": "x"}), "x")
    check("custom conventional", api_key_for("custom", {"CUSTOM_API_KEY": "c"}), "c")
    check("native has no key env", api_key_for("chatgpt", {"CHATGPT_API_KEY": "x"}), "")
    check("opencode anonymous", api_key_for("opencode", {}), "public")
    check("wrong provider env ignored", api_key_for("anthropic", {"OPENAI_API_KEY": "ignored"}), "")
    check(
        "LANGMESH_API_KEY wins",
        api_key_for("anthropic", {"LANGMESH_API_KEY": "a", "ANTHROPIC_API_KEY": "b"}),
        "a",
    )
    check(
        "alibaba names include catalogue and conventional",
        provider_env_vars("alibaba"),
        ("DASHSCOPE_API_KEY", "ALIBABA_API_KEY"),
    )
    check("native providers have no env", provider_env_vars("chatgpt"), ())
    check("hyphen conventional names", provider_env_vars("open-router"), ("OPEN_ROUTER_API_KEY",))
    for identifier, definition in PROVIDERS.items():
        names = provider_env_vars(identifier)
        if definition.native:
            check(f"{identifier} native empty", names, ())
            continue
        if not names:
            failures.append(f"{identifier}: expected env names")
        conventional = f"{identifier.upper().replace('-', '_')}_API_KEY"
        if conventional not in names:
            failures.append(f"{identifier}: missing {conventional}")
    previous = os.environ.get("DASHSCOPE_API_KEY")
    os.environ["DASHSCOPE_API_KEY"] = "from-env"
    try:
        check("resolve_api_key reads catalogue env", resolve_api_key("alibaba", {}), "from-env")
    finally:
        if previous is None:
            del os.environ["DASHSCOPE_API_KEY"]
        else:
            os.environ["DASHSCOPE_API_KEY"] = previous
    bash = BashToolConfiguration(permissions=dict(_BASH_DENY))
    check("deny git push", bash.evaluate_permission("git push origin feature"), "deny")
    check("deny force-push", bash.evaluate_permission("git push --force origin feature"), "deny")
    check("deny -f push", bash.evaluate_permission("git push -f origin feature"), "deny")
    check("allow git commit", bash.evaluate_permission("git commit -am x"), "allow")
    check("empty reply", posted_reply(""), "Done.")
    check("stripped reply", posted_reply("  All green.  "), "All green.")
    check(
        "reply with pull",
        posted_reply("All green.", "https://example.test/pr"),
        "All green.\n\nhttps://example.test/pr",
    )
    issue_mention = mention_from_event(issue(), repository=REPO)
    assert issue_mention is not None
    prompt = render(
        "system",
        {"publication": publication_note(issue_mention, code="ab12")},
    )
    check("system names the comment tool", "submit_github_comment" in prompt, True)
    check("system forbids push", "Do not git push" in prompt, True)
    check("system names the draft", "draft pull request" in prompt, True)
    check("system asks the agent to branch", "git checkout -b" in prompt, True)
    recorded: list[list[str]] = []

    def fake_create(
        arguments: list[str],
        *,
        cwd: str,
        env: Mapping[str, str] | None = None,
        extraheader: str = "",
    ) -> str:
        recorded.append(list(arguments))
        if arguments[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return "langmesh/flaky-test-ab12"
        if arguments[:3] == ["gh", "pr", "list"]:
            return ""
        if arguments[:3] == ["gh", "pr", "create"]:
            return "https://github.com/ghovax/langmesh/pull/99\n"
        return ""

    opened = publish_changes(issue_mention, Path("/tmp"), token="t", run=fake_create)
    check("issue publish returns the draft url", opened, "https://github.com/ghovax/langmesh/pull/99")
    created = next(call for call in recorded if call[:3] == ["gh", "pr", "create"])
    check("issue publish creates a draft", "--draft" in created, True)
    recorded.clear()

    def fake_existing(
        arguments: list[str],
        *,
        cwd: str,
        env: Mapping[str, str] | None = None,
        extraheader: str = "",
    ) -> str:
        recorded.append(list(arguments))
        if arguments[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return "langmesh/flaky-test-ab12"
        if arguments[:3] == ["gh", "pr", "list"]:
            return "https://github.com/ghovax/langmesh/pull/3"
        return ""

    reused = publish_changes(issue_mention, Path("/tmp"), token="t", run=fake_existing)
    check("follow-up reuses the existing PR", reused, "https://github.com/ghovax/langmesh/pull/3")
    check(
        "follow-up does not create",
        all(call[:3] != ["gh", "pr", "create"] for call in recorded),
        True,
    )
    check("follow-up does not mark ready", all("ready" not in call for call in recorded), True)
    recorded.clear()

    def fake_agent_name(
        arguments: list[str],
        *,
        cwd: str,
        env: Mapping[str, str] | None = None,
        extraheader: str = "",
    ) -> str:
        recorded.append(list(arguments))
        if arguments[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return "fix-auth"
        if arguments[:3] == ["gh", "pr", "list"]:
            return ""
        if arguments[:3] == ["gh", "pr", "create"]:
            return "https://github.com/ghovax/langmesh/pull/8\n"
        return ""

    scratch = Path(tempfile.mkdtemp())
    (scratch / ".github/langmesh").mkdir(parents=True)
    (scratch / ".github/langmesh/code").write_text("ab12\n")
    opened = publish_changes(issue_mention, scratch, token="t", run=fake_agent_name)
    check("agent-named branch opens a draft", opened, "https://github.com/ghovax/langmesh/pull/8")
    check(
        "wrapper prefixes the agent's name",
        ["git", "checkout", "-B", "langmesh/fix-auth-ab12"] in recorded,
        True,
    )
    created = next(call for call in recorded if call[:3] == ["gh", "pr", "create"])
    check("create uses the agent's slug", "langmesh/fix-auth-ab12" in created, True)
    recorded.clear()

    def fake_on_pull(
        arguments: list[str],
        *,
        cwd: str,
        env: Mapping[str, str] | None = None,
        extraheader: str = "",
    ) -> str:
        recorded.append(list(arguments))
        if arguments[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return "feature"
        return ""

    pull_mention = mention_from_event(issue(pull=True), repository=REPO)
    assert pull_mention is not None
    check(
        "pull mention does not open a PR",
        publish_changes(pull_mention, Path("/tmp"), token="t", run=fake_on_pull),
        "",
    )
    check(
        "pull mention does not call gh pr",
        all(call[:2] != ["gh", "pr"] for call in recorded),
        True,
    )
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
