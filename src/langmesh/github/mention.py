"""A GitHub Action embedder: one @langmesh[bot] mention drives one library session.

The daemon is not involved. Follow-up mentions on the same issue or pull request restore
the saved session. The process may use GitHub's token; tool children cannot, because
confinement strips it from their environment and credentials are never written into the
checkout. On an issue the agent reuses a branch that already is this work when one exists,
otherwise creates ``langmesh/<slug>-<code>`` itself; the agent
commits, and the wrapper pushes and opens a draft. On a pull request, file edits are
pushed to that branch. Never to main. Thread replies stay user-facing; failures go to
the logger.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

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

MENTION = "@langmesh[bot]"
MENTION_ALIASES = (MENTION, "@langmesh")
# `LangMesh` cannot be a GitHub App (reserved for @langmesh). Accept @langmesh-…[bot].
_HYPHENATED_BOT = re.compile(r"@langmesh-[\w-]+\[bot\](?![\w-])", re.IGNORECASE)
ALLOWED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
STATE_DIRECTORY = ".github/langmesh"
BRANCH_RECORD = "branch"
PROTECTED_BRANCHES = frozenset({"main", "master"})
ACK_COMMENT_ENV = "LANGMESH_ACK_COMMENT_ID"
TURN_FAILED = (
    "Something went wrong while I was working on this. "
    "The details are in the Action log."
)
INVALID_MODEL = (
    "I couldn't start this turn because the model setting isn't in the form I need. "
    "It has to be `provider/model`. The Action log has the exact value."
)
_PROMPTS = PackagePromptLoader(Path(__file__).resolve().parent / "prompts")
logger = logging.getLogger("langmesh.github")

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
    """One @langmesh[bot] comment the Action will answer."""

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


def configure_logging() -> None:
    """The same stderr logger the daemon uses: timestamp, level, logger name, message."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
        force=True,
    )
    logging.getLogger("langmesh").setLevel(logging.INFO)


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


def run_log_url() -> str:
    server = (os.environ.get("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
    repository = os.environ.get("GITHUB_REPOSITORY") or ""
    run_id = os.environ.get("GITHUB_RUN_ID") or ""
    if repository and run_id:
        return f"{server}/{repository}/actions/runs/{run_id}"
    return ""


def acknowledgement() -> str:
    return render("acknowledgement")


def working_comment(text: str) -> str:
    """The acknowledgement or a progress note, plus the live Action log."""
    body = (text or acknowledgement()).strip()
    url = run_log_url()
    if not url:
        return body
    return render("working_comment", {"body": body, "url": url})


def user_failure(message: str) -> str:
    """A thread reply that stays helpful and leaves the cause in the Action log."""
    log = run_log_url()
    if log:
        return f"{message} See the [Action log]({log})."
    return message


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


def reply_to_mention_bot(
    event: Mapping[str, Any],
    *,
    repository: str,
    token: str,
    api: str,
) -> bool:
    """A quote-reply, review reply, or the comment immediately after the mention bot."""
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
                record = _api_request(f"{api}/repos/{repository}/{path}", token)
            except (RuntimeError, TypeError, ValueError):
                continue
            login = str((record.get("user") or {}).get("login") or "")
            if mention_bot_login(login):
                return True
    number = (event.get("issue") or {}).get("number") or (
        event.get("pull_request") or {}
    ).get("number")
    this_id = comment.get("id")
    if not number or not this_id:
        return False
    try:
        rows = _api_request(
            f"{api}/repos/{repository}/issues/{int(number)}/comments?per_page=100",
            token,
        )
    except (RuntimeError, TypeError, ValueError):
        return False
    if not isinstance(rows, list):
        return False
    previous = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if int(row.get("id") or 0) == int(this_id):
            break
        previous = row
    if previous is None:
        return False
    return mention_bot_login(str((previous.get("user") or {}).get("login") or ""))


def mention_from_event(
    event: Mapping[str, Any],
    *,
    repository: str,
    pull: Mapping[str, Any] | None = None,
    token: str = "",
    api: str = "",
) -> Mention | None:
    """The mention this payload is, or ``None`` when it is not a mention to answer."""
    comment = event.get("comment") or {}
    body = str(comment.get("body") or "")
    if not mentioned(body) and not reply_to_mention_bot(
        event, repository=repository, token=token, api=api
    ):
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
        command = ["git", "-c", f"{_git_header_key()}={extraheader}", *command[1:]]
    merged = dict(os.environ if env is None else env)
    if command and command[0] == "git":
        merged.setdefault("GIT_TERMINAL_PROMPT", "0")
    completed = subprocess.run(command, cwd=cwd, env=merged, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        shown = " ".join(arguments)
        logger.error("%s failed: %s", shown, detail or f"exit {completed.returncode}")
        raise RuntimeError(f"{shown} failed: {detail or f'exit {completed.returncode}'}")
    return completed.stdout


def _git_header_key() -> str:
    """The ``http.<url>.extraheader`` key ``actions/checkout`` uses for this host."""
    host = (os.environ.get("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
    return f"http.{host}/.extraheader"


def _git_header(token: str) -> str:
    """Git HTTPS wants Basic ``x-access-token``, not the REST API's Bearer scheme."""
    if not token:
        return ""
    credential = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return f"AUTHORIZATION: basic {credential}"


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
    except (OSError, RuntimeError, subprocess.CalledProcessError):
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
    # Checkout writes safe.directory under a temporary HOME; later git uses the runner's HOME.
    run(["git", "config", "--global", "--add", "safe.directory", cwd], cwd=cwd)
    configure_git_author(workspace, run=run)
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
    staged = run(["git", "diff", "--cached", "--name-only"], cwd=cwd).strip()
    run(["git", "reset", "-q"], cwd=cwd)
    return bool(staged)


def commits_to_push(workspace: Path, *, run: Run = _run) -> bool:
    """Whether HEAD has commits that are not on ``origin`` for this branch."""
    cwd = str(workspace)
    branch = current_branch(workspace, run=run)
    try:
        count = run(
            ["git", "rev-list", "--count", f"origin/{branch}..HEAD"],
            cwd=cwd,
        ).strip()
    except (OSError, RuntimeError, subprocess.CalledProcessError):
        return True
    return int(count or 0) > 0


def configure_git_author(workspace: Path, *, run: Run = _run) -> None:
    """So commits the agent makes are authored as the job's bot."""
    cwd = str(workspace)
    actor = (os.environ.get("LANGMESH_GIT_NAME") or "github-actions[bot]").strip()
    email = (
        os.environ.get("LANGMESH_GIT_EMAIL") or "41898282+github-actions[bot]@users.noreply.github.com"
    ).strip()
    run(["git", "config", "user.name", actor], cwd=cwd)
    run(["git", "config", "user.email", email], cwd=cwd)


def publish_changes(
    mention: Mention,
    workspace: Path,
    *,
    token: str,
    run: Run = _run,
) -> str:
    """Push the agent's commits. On an issue, open or reuse a draft PR. On a pull request, only push."""
    cwd = str(workspace)
    branch = current_branch(workspace, run=run)
    protected = PROTECTED_BRANCHES | {mention.default_branch}
    if branch in protected:
        raise RuntimeError(f"refusing to push protected branch {branch!r}")
    if mention.kind == "issue":
        remember_branch(workspace, branch)
    run(["git", "add", "-A"], cwd=cwd)
    run(["git", "reset", "-q", "--", STATE_DIRECTORY], cwd=cwd)
    if tree_is_dirty(workspace, run=run):
        raise RuntimeError("uncommitted file changes remain; the agent must commit before finishing")
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


def _api_request(url: str, token: str, *, data: bytes | None = None, method: str = "") -> Any:
    verb = method or ("POST" if data is not None else "GET")
    request = urllib.request.Request(
        url,
        data=data,
        method=verb,
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


def acknowledgement_id(environ: Mapping[str, str] | None = None) -> int | None:
    """Comment id posted by the workflow before this process started, if any."""
    raw = ((environ or os.environ).get(ACK_COMMENT_ENV) or "").strip()
    return int(raw) if raw.isdigit() else None


def delete_comment(repository: str, comment_id: int, token: str, api: str) -> None:
    _api_request(
        f"{api}/repos/{repository}/issues/comments/{comment_id}",
        token,
        method="DELETE",
    )


def create_comment(repository: str, number: int, text: str, token: str, api: str) -> int:
    record = _api_request(
        f"{api}/repos/{repository}/issues/{number}/comments",
        token,
        data=json.dumps({"body": text[:65536]}).encode(),
    )
    return int(record["id"])


def update_comment(repository: str, comment_id: int, text: str, token: str, api: str) -> None:
    _api_request(
        f"{api}/repos/{repository}/issues/comments/{comment_id}",
        token,
        data=json.dumps({"body": text[:65536]}).encode(),
        method="PATCH",
    )


def publish_thread_comment(
    repository: str,
    number: int,
    text: str,
    token: str,
    api: str,
    *,
    comment_id: int | None = None,
) -> int:
    """Update the acknowledgement in place, or post a new comment if that is not possible."""
    if comment_id:
        try:
            update_comment(repository, comment_id, text, token, api)
            return comment_id
        except Exception:
            logger.exception("could not update acknowledgement comment %s", comment_id)
    return create_comment(repository, number, text, token, api)


def post_comment(repository: str, number: int, text: str, token: str, api: str) -> None:
    create_comment(repository, number, text, token, api)


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
        description="Does the work asked in a GitHub mention, in the repository that comment is on.",
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


async def run_turn(
    mention: Mention,
    workspace: Path,
    *,
    checkout: Checkout,
    publish: Callable[[str], None] | None = None,
) -> str:
    def publish_working(text: str) -> None:
        if publish is None:
            return
        publish(working_comment(text))

    reply = GitHubReply(publish=publish_working if publish is not None else None)
    async with _session(mention, workspace, reply) as session:
        await session.ask(prompt_for(mention, checkout=checkout))
        return (reply.comment or "").strip()


def drop_acknowledgement(
    repository: str, comment_id: int | None, token: str, api: str
) -> None:
    """Remove a workflow-posted ack when this process will not answer the mention."""
    if not comment_id:
        return
    try:
        delete_comment(repository, comment_id, token, api)
    except Exception:
        logger.exception("could not delete acknowledgement comment %s", comment_id)


def main() -> None:
    configure_logging()
    event_path = os.environ["GITHUB_EVENT_PATH"]
    repository = os.environ["GITHUB_REPOSITORY"]
    workspace = Path(os.environ.get("GITHUB_WORKSPACE") or os.getcwd()).resolve()
    token = os.environ.get("GITHUB_TOKEN", "")
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    event = json.loads(Path(event_path).read_text())
    comment_id = acknowledgement_id()
    mention = mention_from_event(
        event, repository=repository, token=token, api=api
    )
    if mention is None:
        drop_acknowledgement(repository, comment_id, token, api)
        return
    if mention.kind == "pull" and not mention.head_ref:
        mention = mention_from_event(
            event,
            repository=repository,
            pull=fetch_pull(repository, mention.number, token, api),
            token=token,
            api=api,
        )
        if mention is None:
            drop_acknowledgement(repository, comment_id, token, api)
            return
    if not mention.allowed:
        logger.info("ignoring mention from %s (%s)", mention.user, mention.association)
        drop_acknowledgement(repository, comment_id, token, api)
        return
    logger.info(
        "mention %s %s#%s by %s",
        mention.kind,
        repository,
        mention.number,
        mention.user,
    )
    if comment_id is None:
        try:
            comment_id = create_comment(
                repository,
                mention.number,
                working_comment(acknowledgement()),
                token,
                api,
            )
        except Exception:
            logger.exception("could not post acknowledgement")
    try:
        model_identifier_from_env()
    except ValueError as error:
        logger.error("%s", render("invalid_model", {"value": str(error)}))
        publish_thread_comment(
            repository,
            mention.number,
            user_failure(INVALID_MODEL),
            token,
            api,
            comment_id=comment_id,
        )
        return
    try:
        checkout = prepare_tree(mention, workspace, token=token)

        def publish_comment(text: str) -> None:
            if comment_id:
                update_comment(repository, comment_id, text[:65536], token, api)

        answer = asyncio.run(
            run_turn(
                mention,
                workspace,
                checkout=checkout,
                publish=publish_comment if comment_id else None,
            )
        )
        pull_url = ""
        if tree_is_dirty(workspace) or commits_to_push(workspace):
            pull_url = publish_changes(mention, workspace, token=token)
        publish_thread_comment(
            repository,
            mention.number,
            posted_reply(answer, pull_url),
            token,
            api,
            comment_id=comment_id,
        )
    except Exception:
        logger.exception("mention turn failed")
        try:
            publish_thread_comment(
                repository,
                mention.number,
                user_failure(TURN_FAILED),
                token,
                api,
                comment_id=comment_id,
            )
        except Exception:
            logger.exception("could not publish the user-facing failure comment")
        raise


if __name__ == "__main__":
    main()
