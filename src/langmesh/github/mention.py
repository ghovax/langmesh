"""A GitHub Action embedder: one @langmesh mention drives one library session.

The daemon is not involved. Follow-up mentions on the same issue or pull request restore
the saved session. The process may use GitHub's token; tool children cannot, because
confinement strips it from their environment and credentials are never written into the
checkout. On an issue the agent reuses a branch that already is this work when one exists,
otherwise creates ``langmesh/<name>-<code>`` itself; the wrapper
commits, pushes, and opens a draft. On a pull request, file edits are pushed to that
branch. Never to main.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from langmesh import (
    AgentConfiguration,
    BashToolConfiguration,
    SandboxConfiguration,
    Session,
    SessionComponents,
    SQLiteCheckpoints,
    ToolsConfiguration,
)
from langmesh.base.content.models import split_model_identifier
from langmesh.base.content.prompts import PackagePromptLoader
from langmesh.base.identity.providers import get_provider_definition, provider_env_vars
from langmesh.github.reply import GitHubReply
from langmesh.runtime.features import Feature
from langmesh.runtime.plugins.background import BackgroundJobsFeature
from langmesh.runtime.plugins.bash import Bash
from langmesh.runtime.plugins.compaction import (
    Compaction,
    DirectCompactionPreparation,
    KeepRecentTurns,
)
from langmesh.runtime.plugins.continuation import Continuation
from langmesh.runtime.plugins.permission_reviewer import PermissionReviewer
from langmesh.runtime.plugins.permissions import PermissionReview
from langmesh.runtime.plugins.web import Web

MENTION = "@langmesh"
ALLOWED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
STATE_DIRECTORY = ".github/langmesh"
BRANCH_RECORD = "branch"
PROTECTED_BRANCHES = frozenset({"main", "master"})
_PROMPTS = PackagePromptLoader(Path(__file__).resolve().parent / "prompts")

# The job is the only publisher. Longest-match would otherwise keep force-push at "ask".
_BASH_DENY = {
    "git push*": "deny",
    "git push --force*": "deny",
    "git push -f*": "deny",
}


class Run(Protocol):
    def __call__(
        self,
        arguments: list[str],
        *,
        cwd: str,
        env: Mapping[str, str] | None = None,
        extraheader: str = "",
    ) -> str: ...


@dataclass(frozen=True)
class Checkout:
    """Where this mention starts, and whether that branch already exists for the thread."""

    branch: str
    resumed: bool = False


@dataclass(frozen=True)
class Mention:
    """One @langmesh comment the Action will answer."""

    body: str
    number: int
    kind: str
    title: str
    html_url: str
    user: str
    association: str
    default_branch: str
    repository: str
    head_ref: str = ""
    head_repository: str = ""
    is_fork: bool = False

    @property
    def session_id(self) -> str:
        return f"github:{self.repository}:{self.kind}:{self.number}"

    @property
    def allowed(self) -> bool:
        return self.association in ALLOWED_ASSOCIATIONS and not self.is_fork


def mention_from_event(
    event: Mapping[str, Any],
    *,
    repository: str,
    pull: Mapping[str, Any] | None = None,
) -> Mention | None:
    """The mention this payload is, or ``None`` when it is not one we answer."""
    comment = event.get("comment") or {}
    body = str(comment.get("body") or "")
    if MENTION.lower() not in body.lower():
        return None
    user = str((comment.get("user") or {}).get("login") or "")
    if user.endswith("[bot]"):
        return None
    issue = event.get("issue") or {}
    pull_event = event.get("pull_request") or {}
    number = issue.get("number") or pull_event.get("number")
    if not number:
        return None
    kind = "pull" if pull_event or issue.get("pull_request") else "issue"
    source = pull or pull_event or {}
    head = source.get("head") or {}
    head_repository = str((head.get("repo") or {}).get("full_name") or "")
    default_branch = str((event.get("repository") or {}).get("default_branch") or "main")
    return Mention(
        body=body,
        number=int(number),
        kind=kind,
        title=str(issue.get("title") or pull_event.get("title") or ""),
        html_url=str(issue.get("html_url") or pull_event.get("html_url") or ""),
        user=user,
        association=str(comment.get("author_association") or ""),
        default_branch=default_branch,
        repository=repository,
        head_ref=str(head.get("ref") or ""),
        head_repository=head_repository,
        is_fork=bool(head_repository and head_repository != repository),
    )


def render(name: str, variables: Mapping[str, object] | None = None) -> str:
    """A GitHub-mention prompt from ``prompts/*.md``."""
    return _PROMPTS.load(name, dict(variables or {})).strip()


def posted_reply(answer: str, pull_url: str = "") -> str:
    """The issue comment: the submitted reply, or ``Done.``, plus a pull-request URL when there is one."""
    note = answer.strip() or "Done."
    if not pull_url:
        return note
    return f"""{note}

{pull_url}"""


def model_identifier_from_env(environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    """``LANGMESH_MODEL`` as ``(provider, model)``, split on the first slash."""
    raw = (
        (environ or os.environ).get("LANGMESH_MODEL") or ""
    ).strip() or "anthropic/claude-sonnet-4-5"
    split = split_model_identifier(raw)
    if split is None or not split[0].strip() or not split[1].strip():
        raise ValueError(raw)
    return split[0].strip(), split[1].strip()


def api_key_for(provider: str, environ: Mapping[str, str] | None = None) -> str:
    """``LANGMESH_API_KEY``, then this provider's catalogue env vars, else its anonymous sentinel."""
    env = environ or os.environ
    if key := (env.get("LANGMESH_API_KEY") or "").strip():
        return key
    for name in provider_env_vars(provider):
        if value := (env.get(name) or "").strip():
            return value
    definition = get_provider_definition(provider)
    return (definition.anonymous_api_key if definition is not None else "").strip()


def prompt_for(mention: Mention, *, checkout: Checkout | None = None) -> str:
    """The turn's user message: the thread, the comment, then this mention's situation."""
    branch = checkout.branch if checkout is not None else ""
    resumed = checkout.resumed if checkout is not None else False
    return render(
        "turn",
        {
            "title": mention.title,
            "html_url": mention.html_url,
            "body": mention.body,
            "publication": publication_note(mention, branch=branch, resumed=resumed),
        },
    )


def publication_note(mention: Mention, *, branch: str = "", resumed: bool = False) -> str:
    """This mention's situation, appended on the turn so the system prompt stays cache-stable."""
    if mention.kind != "issue":
        where = f" Stay on `{branch}`." if branch else ""
        return f"This mention is on a pull request.{where}"
    if resumed and branch:
        return f"This mention is on an issue. You are already on `{branch}`; keep working there."
    if branch:
        return f"This mention is on an issue. HEAD is `{branch}`."
    return "This mention is on an issue."


def draft_pull_arguments(mention: Mention, branch: str) -> list[str]:
    """Open a draft PR from this issue's topic branch. Never marks an existing PR ready."""
    return [
        "gh",
        "pr",
        "create",
        "--draft",
        "--head",
        branch,
        "--base",
        mention.default_branch,
        "--title",
        mention.title or branch,
        "--body",
        f"Opened from {mention.html_url}",
    ]


def branch_record(workspace: Path) -> Path:
    return workspace / STATE_DIRECTORY / BRANCH_RECORD


def remember_branch(workspace: Path, name: str) -> None:
    path = branch_record(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{name}\n")


def recalled_branch(workspace: Path) -> str:
    path = branch_record(workspace)
    if not path.is_file():
        return ""
    name = path.read_text().strip()
    if not name or name in PROTECTED_BRANCHES:
        return ""
    return name


def state_path(workspace: Path) -> Path:
    return workspace / STATE_DIRECTORY / "session.sqlite"


def _run(
    arguments: list[str],
    *,
    cwd: str,
    env: Mapping[str, str] | None = None,
    extraheader: str = "",
) -> str:
    """Run a process in ``cwd`` and return stdout. ``extraheader`` is GitHub auth that never hits disk."""
    command = list(arguments)
    if extraheader and command and command[0] == "git":
        command = ["git", "-c", f"http.extraheader={extraheader}", *command[1:]]
    merged = dict(os.environ if env is None else env)
    completed = subprocess.run(command, cwd=cwd, env=merged, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise subprocess.CalledProcessError(
            completed.returncode,
            command,
            completed.stdout,
            completed.stderr or detail or f"exit {completed.returncode}",
        )
    return completed.stdout


def _git_header(token: str) -> str:
    return f"AUTHORIZATION: bearer {token}" if token else ""


def _remote_has(name: str, *, cwd: str, header: str, run: Run) -> bool:
    return bool(
        run(
            ["git", "ls-remote", "--heads", "origin", name],
            cwd=cwd,
            extraheader=header,
        ).strip()
    )


def _checkout_named(
    name: str,
    *,
    default_branch: str,
    cwd: str,
    header: str,
    run: Run,
    remote_exists: bool,
) -> None:
    source = name if remote_exists else default_branch
    run(["git", "fetch", "origin", source], cwd=cwd, extraheader=header)
    run(["git", "checkout", "-B", name, "FETCH_HEAD"], cwd=cwd)


def open_issue_head(mention: Mention, *, run: Run, cwd: str) -> str:
    """The head of an open draft already opened from this issue, if ``gh`` can see one."""
    try:
        raw = run(
            [
                "gh",
                "pr",
                "list",
                "--search",
                f"Opened from {mention.html_url}",
                "--state",
                "open",
                "--json",
                "headRefName",
            ],
            cwd=cwd,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    try:
        rows = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError:
        return ""
    if not isinstance(rows, list):
        return ""
    for row in rows:
        if not isinstance(row, dict):
            continue
        head = str(row.get("headRefName") or "").strip()
        if head and head not in PROTECTED_BRANCHES:
            return head
    return ""


def prepare_tree(mention: Mention, workspace: Path, *, token: str, run: Run = _run) -> Checkout:
    """Check out an existing thread branch, or the default branch so the agent can choose."""
    cwd = str(workspace)
    header = _git_header(token)
    run(["git", "fetch", "origin", "--prune"], cwd=cwd, extraheader=header)
    if mention.kind == "pull" and mention.head_ref and not mention.is_fork:
        run(
            ["git", "fetch", "origin", f"refs/heads/{mention.head_ref}"],
            cwd=cwd,
            extraheader=header,
        )
        run(["git", "checkout", "-B", mention.head_ref, "FETCH_HEAD"], cwd=cwd)
        return Checkout(branch=mention.head_ref, resumed=True)
    name = recalled_branch(workspace) or open_issue_head(mention, run=run, cwd=cwd)
    if name:
        _checkout_named(
            name,
            default_branch=mention.default_branch,
            cwd=cwd,
            header=header,
            run=run,
            remote_exists=_remote_has(name, cwd=cwd, header=header, run=run),
        )
        remember_branch(workspace, name)
        return Checkout(branch=name, resumed=True)
    run(
        ["git", "fetch", "origin", mention.default_branch],
        cwd=cwd,
        extraheader=header,
    )
    run(
        ["git", "checkout", "-B", mention.default_branch, "FETCH_HEAD"],
        cwd=cwd,
    )
    return Checkout(branch=mention.default_branch, resumed=False)


def current_branch(workspace: Path, *, run: Run = _run) -> str:
    return run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(workspace)).strip()


def tree_is_dirty(workspace: Path, *, run: Run = _run) -> bool:
    """Whether the checkout has work besides the session's own state directory."""
    cwd = str(workspace)
    run(["git", "add", "-A"], cwd=cwd)
    run(["git", "reset", "-q", "--", STATE_DIRECTORY], cwd=cwd)
    try:
        run(["git", "diff", "--cached", "--quiet"], cwd=cwd)
    except subprocess.CalledProcessError:
        return True
    run(["git", "reset", "-q"], cwd=cwd)
    return False


def publish_changes(mention: Mention, workspace: Path, *, token: str, run: Run = _run) -> str:
    """Commit file edits and push. On an issue, open or reuse a draft PR. On a pull request, only push."""
    cwd = str(workspace)
    branch = current_branch(workspace, run=run)
    protected = PROTECTED_BRANCHES | {mention.default_branch}
    if branch in protected:
        raise RuntimeError(f"refusing to push protected branch {branch!r}")
    if mention.kind == "issue":
        remember_branch(workspace, branch)
    run(["git", "add", "-A"], cwd=cwd)
    run(["git", "reset", "-q", "--", STATE_DIRECTORY], cwd=cwd)
    run(["git", "config", "user.name", "github-actions[bot]"], cwd=cwd)
    run(
        ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
        cwd=cwd,
    )
    run(
        [
            "git",
            "commit",
            "-m",
            f"langmesh: {mention.title or mention.session_id}",
        ],
        cwd=cwd,
    )
    header = _git_header(token)
    run(["git", "push", "-u", "origin", f"HEAD:{branch}"], cwd=cwd, extraheader=header)
    if mention.kind != "issue":
        return ""
    existing = run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--json",
            "url",
            "--jq",
            ".[0].url // empty",
        ],
        cwd=cwd,
    ).strip()
    if existing:
        return existing
    return run(draft_pull_arguments(mention, branch), cwd=cwd).strip()


def _api_request(url: str, token: str, *, data: bytes | None = None) -> Any:
    request = urllib.request.Request(
        url,
        data=data,
        method="POST" if data is not None else "GET",
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


def fetch_pull(repository: str, number: int, token: str, api: str) -> dict[str, Any]:
    return _api_request(f"{api}/repos/{repository}/pulls/{number}", token)


def post_comment(repository: str, number: int, text: str, token: str, api: str) -> None:
    _api_request(
        f"{api}/repos/{repository}/issues/{number}/comments",
        token,
        data=json.dumps({"body": text[:65536]}).encode(),
    )


def mention_features(reply: GitHubReply) -> list[Feature]:
    """The plugins one mention session runs, including the comment tool."""
    reviewer = PermissionReviewer()
    return [
        Compaction(
            strategy=KeepRecentTurns(24),
            preparation=DirectCompactionPreparation(),
            summarizer=None,
        ),
        PermissionReview(reviewer=reviewer),
        reviewer,
        Continuation(),
        BackgroundJobsFeature(),
        Bash(),
        Web(),
        reply,
    ]


def _session(mention: Mention, workspace: Path, reply: GitHubReply) -> Session:
    state_path(workspace).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        state_path(workspace), isolation_level=None, check_same_thread=False
    )
    provider, model = model_identifier_from_env()
    key = api_key_for(provider)
    agent = AgentConfiguration(
        name="langmesh",
        description="Does the work asked in a GitHub mention.",
        system_prompt=render("system"),
        provider=provider,
        model=model,
        permission_mode="automatic",
        tools_enabled=[
            "bash",
            "search_web",
            "fetch_url",
            "download",
            "set_tasks",
            "update_tasks",
            "submit_github_comment",
        ],
        tools=ToolsConfiguration(bash=BashToolConfiguration(permissions=dict(_BASH_DENY))),
    )
    return Session(
        agent,
        directory=str(workspace),
        session_id=mention.session_id,
        permission_mode="automatic",
        sandbox=SandboxConfiguration(enforce="required", network=False),
        providers={provider: key} if key else None,
        components=SessionComponents(
            checkpoints=SQLiteCheckpoints(connection),
            features=mention_features(reply),
        ),
    )


async def run_turn(mention: Mention, workspace: Path, *, checkout: Checkout) -> str:
    reply = GitHubReply()
    async with _session(mention, workspace, reply) as session:
        await session.ask(prompt_for(mention, checkout=checkout))
        return (reply.comment or "").strip()


def main() -> None:
    event_path = os.environ["GITHUB_EVENT_PATH"]
    repository = os.environ["GITHUB_REPOSITORY"]
    workspace = Path(os.environ.get("GITHUB_WORKSPACE") or os.getcwd()).resolve()
    token = os.environ.get("GITHUB_TOKEN", "")
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    event = json.loads(Path(event_path).read_text())
    mention = mention_from_event(event, repository=repository)
    if mention is None:
        return
    if mention.kind == "pull" and not mention.head_ref:
        mention = mention_from_event(
            event,
            repository=repository,
            pull=fetch_pull(repository, mention.number, token, api),
        )
        if mention is None:
            return
    if not mention.allowed:
        return
    try:
        model_identifier_from_env()
    except ValueError as error:
        post_comment(
            repository,
            mention.number,
            render("invalid_model", {"value": str(error)}),
            token,
            api,
        )
        return
    checkout = prepare_tree(mention, workspace, token=token)
    try:
        answer = asyncio.run(run_turn(mention, workspace, checkout=checkout))
    except Exception as error:
        post_comment(
            repository,
            mention.number,
            f"The turn failed: {error}",
            token,
            api,
        )
        raise
    pull_url = ""
    if tree_is_dirty(workspace):
        pull_url = publish_changes(mention, workspace, token=token)
    post_comment(repository, mention.number, posted_reply(answer, pull_url), token, api)


if __name__ == "__main__":
    main()
