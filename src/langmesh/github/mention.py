"""The GitHub App mention runtime.

The service supplies the provider, model, API key, installation token, and App identity.
Follow-up mentions on the same issue or pull request restore the saved session. The
agent commits and pushes on topic branches, while the service prevents pushes to the
default branch and opens draft pull requests for issue work.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol

from models_provider import bind_credential_store, fetch_chatgpt_models, reset_credential_store

from langmesh import (
    AgentConfiguration,
    PackagePromptLoader,
    SandboxConfiguration,
    Session,
    SessionComponents,
)
from langmesh.base.confinement import Profile, environment_variables
from langmesh.runtime.plugins.compaction.configuration import CompactionConfiguration
from langmesh.base.content.model_routing import resolve_litellm
from langmesh.base.contracts.ports import Checkpoints
from langmesh.base.persistence.artifacts import DirectoryArtifacts
from langmesh.github.detect import is_mention_turn
from langmesh.runtime.features import Feature
from langmesh.runtime.plugins.background import BackgroundJobs
from langmesh.runtime.plugins.bash import Bash
from langmesh.runtime.plugins.compaction import (
    Compaction,
    DirectCompactionPreparation,
)
from langmesh.runtime.plugins.continuation import Continuation
from langmesh.runtime.plugins.permissions import PermissionReview
from langmesh.runtime.plugins.web import Web
from langmesh.runtime.turn_events import (
    CompactionDone,
    CompactionStarted,
    Done,
    Suspended,
    TextChunk,
    ToolCall,
    Usage,
)

ALLOWED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
PROTECTED_BRANCHES = frozenset({"main", "master"})
EXTERNAL_COMMAND_TIMEOUT_SECONDS = 120.0
_PROMPTS = PackagePromptLoader(Path(__file__).resolve().parent / "prompts")
logger = logging.getLogger("langmesh.github")


def _resident_memory_megabytes() -> float:
    """Current process RSS on Linux, or zero when the host does not expose it."""
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


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
    """One comment the installed GitHub App will answer."""

    body: str
    number: int
    kind: str
    title: str
    html_url: str
    source_url: str
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


def acknowledgement(viewer_url: str = "") -> str:
    return working_comment(render("acknowledgement"), viewer_url=viewer_url)


def working_comment(message: str, *, viewer_url: str = "") -> str:
    """Add a standard GitHub note to an in-progress comment."""
    text = message.strip() or "Working on this."
    if viewer_url:
        return f"""> [!NOTE]
> Work is still in progress. [View the live session]({viewer_url})

{text}"""
    return f"""> [!NOTE]
> Work is still in progress.

{text}"""


def comment_pointer(comment: Mapping[str, Any], thread_url: str) -> str:
    """The HTML URL of this comment, or a fragment on the thread when the event omitted one."""
    url = str(comment.get("html_url") or "").strip()
    if url:
        return url
    comment_id = comment.get("id")
    if not thread_url or not comment_id:
        return thread_url
    if comment.get("pull_request_review_id") is not None or comment.get("diff_hunk") is not None:
        return f"{thread_url}#discussion_r{int(comment_id)}"
    return f"{thread_url}#issuecomment-{int(comment_id)}"


def mention_from_event(
    event: Mapping[str, Any],
    *,
    event_name: str = "",
    repository: str,
    pull: Mapping[str, Any] | None = None,
    token: str = "",
    api: str = "",
    known_turn: bool = False,
    bot_login: str = "",
) -> Mention | None:
    """The mention this payload is, or ``None`` when it is not a mention to answer.

    ``known_turn`` is set when the event filter already decided this event starts a
    turn, so this call does not walk GitHub again.
    """
    comment = event.get("comment") or {}
    issue = event.get("issue") or {}
    pull_event = event.get("pull_request") or {}
    event_source = comment or pull_event or issue
    body = str(event_source.get("body") or "")
    if not known_turn and not is_mention_turn(
        event,
        event_name=event_name,
        repository=repository,
        token=token,
        api=api,
        bot_login=bot_login,
    ):
        return None
    user = str((event_source.get("user") or {}).get("login") or "")
    if user.lower().endswith("[bot]"):
        return None
    number = issue.get("number") or pull_event.get("number")
    if not number:
        return None
    kind = "pull" if pull_event or issue.get("pull_request") else "issue"
    pull_source = pull or pull_event or {}
    head = pull_source.get("head") or {}
    head_repository = str((head.get("repo") or {}).get("full_name") or "")
    default_branch = str((event.get("repository") or {}).get("default_branch") or "main")
    html_url = str(issue.get("html_url") or pull_event.get("html_url") or "")
    return Mention(
        body=body,
        number=int(number),
        kind=kind,
        title=str(issue.get("title") or pull_event.get("title") or ""),
        html_url=html_url,
        source_url=comment_pointer(comment, html_url) if comment else html_url,
        user=user,
        association=str(event_source.get("author_association") or ""),
        default_branch=default_branch,
        repository=repository,
        head_ref=str(head.get("ref") or ""),
        head_repository=head_repository,
        is_fork=bool(head_repository and head_repository != repository),
    )


def render(name: str, variables: Mapping[str, object] | None = None) -> str:
    """A GitHub-mention prompt from ``prompts/*.md``."""
    return _PROMPTS.load(name, dict(variables or {})).strip()


def posted_reply(answer: str, pull_url: str = "", viewer_url: str = "") -> str:
    """The issue comment: the submitted reply, or ``Done.``, plus a pull-request URL when there is one."""
    note = answer.strip() or "Done."
    if pull_url:
        note = f"""{note}

{pull_url}"""
    if viewer_url:
        note = f"""{note}

[View the session]({viewer_url})"""
    return note


def turn_payload(
    mention: Mention,
    *,
    checkout: Checkout | None = None,
    followup: bool = False,
) -> dict[str, str]:
    """The labeled fields one mention turn sends. Follow-ups omit the stable thread keys."""
    payload: dict[str, str] = {}
    if not followup:
        if mention.title.strip():
            payload["thread"] = mention.title.strip()
        if mention.html_url.strip():
            payload["thread_url"] = mention.html_url.strip()
        payload["kind"] = mention.kind
        head = checkout.branch if checkout is not None else ""
        if head.strip():
            payload["head"] = head.strip()
    if mention.source_url.strip():
        payload["source_url"] = mention.source_url.strip()
    if mention.user.strip():
        payload["source_author"] = mention.user.strip()
    payload["body"] = mention.body
    return payload


def prompt_for(
    mention: Mention,
    *,
    checkout: Checkout | None = None,
    followup: bool = False,
) -> str:
    """The turn's user message: one JSON object with a key in front of each field.

    The opening turn names the thread, its URL, the kind, HEAD, the comment URL, and
    the comment. A later mention on the same thread — restored or not — sends only
    the new comment and its URL so those stable keys are not repeated. The thread
    body is not pasted; the agent reads earlier comments through ``gh``.
    """
    return render(
        "turn",
        {
            "payload": json.dumps(
                turn_payload(mention, checkout=checkout, followup=followup),
                ensure_ascii=False,
                indent=2,
            )
        },
    )


def _run(
    arguments: list[str],
    *,
    cwd: str,
    env: Mapping[str, str] | None = None,
    extraheader: str = "",
) -> str:
    """Run a bounded process in ``cwd`` and return stdout.

    ``extraheader`` is GitHub authentication that never reaches disk. Non-interactive
    Git settings are deliberate: an unattended webhook worker must fail and retry when
    authentication or the network is unavailable, never wait for terminal input.
    """
    command = list(arguments)
    if extraheader and command and command[0] == "git":
        command = ["git", "-c", f"{_git_header_key()}={extraheader}", *command[1:]]
    merged = dict(os.environ if env is None else env)
    if command and command[0] == "git":
        merged["GIT_TERMINAL_PROMPT"] = "0"
        merged["GCM_INTERACTIVE"] = "Never"
    shown = " ".join(arguments)
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=merged,
            capture_output=True,
            text=True,
            timeout=EXTERNAL_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        logger.error(
            "%s timed out after %.1f seconds",
            shown,
            EXTERNAL_COMMAND_TIMEOUT_SECONDS,
        )
        raise RuntimeError(
            f"{shown} timed out after {EXTERNAL_COMMAND_TIMEOUT_SECONDS:.1f} seconds"
        ) from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
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


def mention_sandbox(token: str) -> Profile:
    """Open network, and give tool children the job token so they can git push and use gh."""
    profile = SandboxConfiguration(enforce="required", network=True).to_profile()
    environment = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}
    if token:
        environment["GITHUB_TOKEN"] = token
        environment["GH_TOKEN"] = token
        environment["GIT_CONFIG_COUNT"] = "1"
        environment["GIT_CONFIG_KEY_0"] = _git_header_key()
        environment["GIT_CONFIG_VALUE_0"] = _git_header(token)
    render_api_key = os.environ.get(environment_variables.RENDER_API_KEY, "").strip()
    if render_api_key:
        environment[environment_variables.RENDER_API_KEY] = render_api_key
    return replace(profile, environment=environment)


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


def prepare_tree(
    mention: Mention,
    workspace: Path,
    *,
    token: str,
    app_slug: str,
    app_id: str,
    run: Run = _run,
) -> Checkout:
    """Check out an existing thread branch, or the default branch so the agent can choose."""
    cwd = str(workspace)
    # The worker uses a temporary HOME, but later git commands still need this checkout
    # marked safe explicitly.
    run(["git", "config", "--global", "--add", "safe.directory", cwd], cwd=cwd)
    set_git_author(workspace, app_slug=app_slug, app_id=app_id, run=run)
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
    name = open_issue_head(mention, run=run, cwd=cwd)
    if name:
        _checkout_named(
            name,
            default_branch=mention.default_branch,
            cwd=cwd,
            header=header,
            run=run,
            remote_exists=_remote_has(name, cwd=cwd, header=header, run=run),
        )
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
    """Whether the temporary checkout has uncommitted work."""
    cwd = str(workspace)
    run(["git", "add", "-A"], cwd=cwd)
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


def set_git_author(
    workspace: Path,
    *,
    app_slug: str,
    app_id: str,
    run: Run = _run,
) -> None:
    """So commits the agent makes are authored as the job's bot."""
    cwd = str(workspace)
    slug = app_slug.strip()
    resolved_app_id = app_id.strip()
    if not slug or not resolved_app_id:
        raise ValueError("the GitHub App slug and ID are required")
    actor = f"{slug}[bot]"
    email = f"{resolved_app_id}+{slug}[bot]@users.noreply.github.com"
    run(["git", "config", "user.name", actor], cwd=cwd)
    run(["git", "config", "user.email", email], cwd=cwd)


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
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"GitHub API {error.code}: {error.read().decode()[:500]}") from error
    return json.loads(body) if body else {}


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


class UncommittedChanges(Feature):
    """Hold the turn open until the agent commits file edits."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    def incomplete_reminder(self) -> str | None:
        if not tree_is_dirty(self._workspace):
            return None
        return render("uncommitted_changes")


def mention_features(
    workspace: Path,
    *,
    compaction_configuration: CompactionConfiguration | None = None,
) -> list[Feature]:
    """The plugins one mention session runs.

    The session is ``automatic``: a call that stays inside the box runs, and a call
    that leaves it or matches a destructive bash rule is decided by the reviewer.
    Ordinary ``git`` and ``gh`` on the topic branch do not raise a gate because the
    service has already supplied network access and the installation token.
    """
    return [
        Compaction(
            configuration=compaction_configuration,
            preparation=DirectCompactionPreparation(),
        ),
        PermissionReview(),
        Continuation(),
        BackgroundJobs(),
        Bash(),
        Web(),
        UncommittedChanges(workspace),
    ]


def _session(
    mention: Mention,
    workspace: Path,
    token: str,
    checkpoints: Checkpoints,
    provider: str,
    model: str,
    api_key: str,
    credential_store: Any = None,
    compaction_configuration: CompactionConfiguration | None = None,
) -> Session:
    provider, model, key = provider.strip().lower(), model.strip(), api_key.strip()
    if not provider or not model:
        raise ValueError("provider and model are required")
    if provider != "chatgpt" and not key:
        raise ValueError("provider, model, and API key are required")
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
        ],
    )
    return Session(
        agent,
        directory=str(workspace),
        session_id=mention.session_id,
        permission_mode="automatic",
        sandbox=mention_sandbox(token),
        providers={provider: key} if key else None,
        components=SessionComponents(
            artifacts=DirectoryArtifacts(workspace.parent / "artifacts"),
            checkpoints=checkpoints,
            features=mention_features(
                workspace,
                compaction_configuration=compaction_configuration or CompactionConfiguration(),
            ),
            credential_store=credential_store,
        ),
    )


async def run_turn(
    mention: Mention,
    workspace: Path,
    *,
    checkout: Checkout,
    update_comment: Callable[[str], Awaitable[None]] | None = None,
    token: str = "",
    thread_followup: bool = False,
    provider: str,
    model: str,
    api_key: str,
    checkpoints: Checkpoints,
    credential_store: Any = None,
    compaction_configuration: CompactionConfiguration | None = None,
    session_callback: Callable[[Session | None], None] | None = None,
) -> str:
    if provider.strip().lower() == "chatgpt" and credential_store is not None:
        credential_binding = bind_credential_store(credential_store)
        try:
            # OAuth supplies the subscription credential; the live catalogue supplies the
            # model's current context window before the runtime chooses its compaction budget.
            await fetch_chatgpt_models()
        finally:
            reset_credential_store(credential_binding)
    async with _session(
        mention,
        workspace,
        token,
        checkpoints,
        provider=provider,
        model=model,
        api_key=api_key,
        credential_store=credential_store,
        compaction_configuration=compaction_configuration,
    ) as session:
        if session_callback is not None:
            session_callback(session)
        try:
            restored = await session.restore()
            followup = restored or thread_followup
            resolved_provider = provider.strip()
            resolved_model = model.strip()
            key = api_key.strip()
            resolved = resolve_litellm(
                f"{resolved_provider}/{resolved_model}",
                {resolved_provider: key} if key else {},
                {},
                credential_store=credential_store,
            )
            logger.info(
                "mention model %s/%s wire=%s base=%s restored=%s messages=%s "
                "thread_followup=%s followup=%s session=%s rss_mib=%s",
                resolved_provider,
                resolved_model,
                resolved["model"],
                resolved["api_base"],
                restored,
                len(session.conversation),
                thread_followup,
                followup,
                mention.session_id,
                _resident_memory_megabytes(),
            )
            await session.set_permission_mode("automatic")
            answer = ""
            response_text = ""
            model_call = 0
            compaction_started = False
            async for event in session.stream(
                prompt_for(mention, checkout=checkout, followup=followup)
            ):
                if isinstance(event, Usage):
                    model_call += 1
                    logger.info(
                        "mention usage session=%s call=%s input=%s output=%s "
                        "cache_read=%s cache_write=%s prefix_reusable=%s "
                        "reusable_prefix=%s shared=%s/%s divergence=%s "
                        "request_reusable=%s cache_fraction=%s rss_mib=%s",
                        mention.session_id,
                        model_call,
                        event.input_tokens,
                        event.output_tokens,
                        event.cache_read_tokens,
                        event.cache_write_tokens,
                        event.cache_prefix_reusable,
                        event.reusable_prefix_tokens,
                        event.shared_segments,
                        event.segments,
                        event.divergence,
                        event.cache_request_reusable,
                        event.cache_read_fraction,
                        _resident_memory_megabytes(),
                    )
                if isinstance(event, CompactionStarted):
                    if not compaction_started and update_comment is not None:
                        await update_comment("Compacting the conversation before continuing.")
                    compaction_started = True
                    logger.info(
                        "mention compaction started session=%s reason=%s "
                        "messages_before=%s tokens_before=%s",
                        mention.session_id,
                        event.reason,
                        event.messages_before,
                        event.tokens_before,
                    )
                if isinstance(event, CompactionDone):
                    if update_comment is not None:
                        status = (
                            "Compaction complete. Continuing."
                            if event.ok
                            else "Compaction did not complete; the turn cannot continue automatically."
                        )
                        await update_comment(status)
                    compaction_started = False
                    logger.info(
                        "mention compaction done session=%s reason=%s ok=%s "
                        "messages_before=%s messages_after=%s "
                        "tokens_before=%s tokens_after=%s error=%s rss_mib=%s",
                        mention.session_id,
                        event.reason,
                        event.ok,
                        event.messages_before,
                        event.messages_after,
                        event.tokens_before,
                        event.tokens_after,
                        event.error_code,
                        _resident_memory_megabytes(),
                    )
                if isinstance(event, Suspended):
                    raise PermissionError(
                        "This turn is suspended. Inspect `session.state.pending`, "
                        "call `session.respond(...)` for each interaction, then drive "
                        "`session.resume()`; or supply an approver through SessionComponents."
                    )
                if isinstance(event, TextChunk):
                    response_text += event.text
                if isinstance(event, ToolCall):
                    status = response_text.strip()
                    if status and update_comment is not None:
                        await update_comment(status)
                        response_text = ""
                if isinstance(event, Done):
                    answer = (
                        "Stopped."
                        if event.stop_reason == "cancelled"
                        else event.text or response_text or answer
                    )
            return answer.strip()
        finally:
            if session_callback is not None:
                session_callback(None)
