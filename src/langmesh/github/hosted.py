"""The installation-level GitHub App service.

This is the universal deployment path for the mention agent. GitHub sends webhook
events here; the service owns the App credentials, and each installation owns its
provider/model settings. Nothing in a customer repository is used as configuration.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import jwt
import yaml
from cryptography.fernet import Fernet
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from langmesh.github.detect import is_mention_turn, thread_has_prior_bot_comment
from langmesh.github.mention import (
    Mention,
    _git_header,
    _git_header_key,
    _run,
    commits_to_push,
    create_comment,
    current_branch,
    mention_from_event,
    posted_reply,
    prepare_tree,
    publish_thread_comment,
    run_turn,
    tree_is_dirty,
    user_failure,
    working_comment,
)

logger = logging.getLogger("langmesh.github.hosted")
DEFAULT_CONFIGURATION_PATH = Path.home() / ".config" / "langmesh" / "github-app.yaml"


@dataclass(frozen=True)
class Settings:
    app_id: str
    private_key_path: Path
    webhook_secret: str
    oauth_client_id: str
    oauth_client_secret: str
    encryption_key_path: Path
    database_path: Path
    workspaces_path: Path
    public_url: str
    github_api_url: str = "https://api.github.com"

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CONFIGURATION_PATH) -> "Settings":
        configuration_path = Path(path).expanduser().resolve()
        try:
            values = yaml.safe_load(configuration_path.read_text(encoding="utf-8")) or {}
        except OSError as error:
            raise RuntimeError(f"GitHub App configuration is missing: {configuration_path}") from error
        if not isinstance(values, dict):
            raise RuntimeError(f"GitHub App configuration must be a YAML mapping: {configuration_path}")

        def required(name: str) -> str:
            value = str(values.get(name) or "").strip()
            if not value:
                raise RuntimeError(f"GitHub App configuration needs {name!r}: {configuration_path}")
            return value

        return cls(
            app_id=required("app_id"),
            private_key_path=Path(required("private_key_path")).expanduser(),
            webhook_secret=required("webhook_secret"),
            oauth_client_id=required("oauth_client_id"),
            oauth_client_secret=required("oauth_client_secret"),
            encryption_key_path=Path(required("encryption_key_path")).expanduser(),
            database_path=Path(required("database_path")).expanduser(),
            workspaces_path=Path(required("workspaces_path")).expanduser(),
            public_url=required("public_url").rstrip("/"),
            github_api_url=str(values.get("github_api_url") or "https://api.github.com").rstrip("/"),
        )


class Store:
    """Small durable store; provider API keys are encrypted before SQLite sees them."""

    def __init__(self, settings: Settings) -> None:
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        key = settings.encryption_key_path.read_bytes().strip()
        self._cipher = Fernet(key)
        self._db = sqlite3.connect(settings.database_path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS installations ("
            " installation_id INTEGER PRIMARY KEY, account_login TEXT NOT NULL,"
            " account_type TEXT NOT NULL, provider TEXT NOT NULL DEFAULT '',"
            " model TEXT NOT NULL DEFAULT '', api_key BLOB NOT NULL DEFAULT '', updated_at INTEGER NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS deliveries (delivery_id TEXT PRIMARY KEY, received_at INTEGER NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS setup_sessions (token TEXT PRIMARY KEY, installation_id INTEGER NOT NULL,"
            " user_login TEXT NOT NULL DEFAULT '', expires_at INTEGER NOT NULL)"
        )
        self._db.commit()

    def seen_delivery(self, delivery_id: str) -> bool:
        if not delivery_id:
            return False
        try:
            self._db.execute(
                "INSERT INTO deliveries(delivery_id, received_at) VALUES (?, ?)",
                (delivery_id, int(time.time())),
            )
            self._db.commit()
            return False
        except sqlite3.IntegrityError:
            return True

    def configuration(self, installation_id: int) -> tuple[str, str, str] | None:
        row = self._db.execute(
            "SELECT provider, model, api_key FROM installations WHERE installation_id = ?",
            (installation_id,),
        ).fetchone()
        if not row or not row[0] or not row[1]:
            return None
        key = self._cipher.decrypt(bytes(row[2])).decode() if row[2] else ""
        return str(row[0]), str(row[1]), key

    def save_installation(
        self, installation_id: int, account_login: str, account_type: str, provider: str, model: str, api_key: str
    ) -> None:
        old = self._db.execute(
            "SELECT api_key FROM installations WHERE installation_id = ?", (installation_id,)
        ).fetchone()
        encrypted = self._cipher.encrypt(api_key.encode()) if api_key else (old[0] if old else b"")
        self._db.execute(
            "INSERT INTO installations(installation_id, account_login, account_type, provider, model, api_key, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(installation_id) DO UPDATE SET"
            " account_login=excluded.account_login, account_type=excluded.account_type, provider=excluded.provider,"
            " model=excluded.model, api_key=excluded.api_key, updated_at=excluded.updated_at",
            (installation_id, account_login, account_type, provider, model, encrypted, int(time.time())),
        )
        self._db.commit()

    def begin_setup(self, installation_id: int) -> str:
        token = secrets.token_urlsafe(32)
        self._db.execute(
            "INSERT INTO setup_sessions(token, installation_id, expires_at) VALUES (?, ?, ?)",
            (token, installation_id, int(time.time()) + 600),
        )
        self._db.commit()
        return token

    def authenticate_setup(self, token: str, user_login: str) -> tuple[int, str] | None:
        row = self._db.execute(
            "SELECT installation_id FROM setup_sessions WHERE token = ? AND expires_at >= ?",
            (token, int(time.time())),
        ).fetchone()
        if not row:
            return None
        self._db.execute(
            "UPDATE setup_sessions SET user_login = ? WHERE token = ?", (user_login, token)
        )
        self._db.commit()
        return int(row[0]), user_login

    def setup(self, token: str) -> tuple[int, str] | None:
        row = self._db.execute(
            "SELECT installation_id, user_login FROM setup_sessions WHERE token = ? AND expires_at >= ? AND user_login != ''",
            (token, int(time.time())),
        ).fetchone()
        return (int(row[0]), str(row[1])) if row else None


class GitHub:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.private_key = settings.private_key_path.read_text(encoding="utf-8")
        self._slug = ""

    def _jwt(self) -> str:
        now = int(time.time())
        return str(
            jwt.encode(
                {"iat": now - 60, "exp": now + 540, "iss": int(self.settings.app_id)},
                self.private_key,
                algorithm="RS256",
            )
        )

    def request(self, path: str, token: str, *, data: Any = None, method: str = "") -> Any:
        body = None if data is None else json.dumps(data).encode()
        request = urllib.request.Request(
            f"{self.settings.github_api_url}{path}",
            data=body,
            method=method or ("POST" if body is not None else "GET"),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "langmesh-github-app",
            },
        )
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"GitHub API {error.code}: {error.read().decode()[:500]}") from error
        return json.loads(raw) if raw else {}

    def app_slug(self) -> str:
        if not self._slug:
            self._slug = str(self.request("/app", self._jwt()).get("slug") or "").strip()
        if not self._slug:
            raise RuntimeError("GitHub App returned no slug")
        return self._slug

    def installation_token(self, installation_id: int) -> str:
        record = self.request(
            f"/app/installations/{installation_id}/access_tokens", self._jwt(), data={}
        )
        token = str(record.get("token") or "").strip()
        if not token:
            raise RuntimeError("GitHub App installation token was empty")
        return token

    def oauth_token(self, code: str) -> str:
        body = urllib.parse.urlencode(
            {
                "client_id": self.settings.oauth_client_id,
                "client_secret": self.settings.oauth_client_secret,
                "code": code,
            }
        ).encode()
        request = urllib.request.Request(
            "https://github.com/login/oauth/access_token",
            data=body,
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            record = json.loads(response.read())
        token = str(record.get("access_token") or "").strip()
        if not token:
            raise RuntimeError("GitHub OAuth did not return an access token")
        return token

    def user(self, token: str) -> dict[str, Any]:
        return dict(self.request("/user", token))

    def verify_installation(self, installation_id: int, token: str) -> dict[str, Any]:
        return dict(
            self.request(f"/user/installations/{installation_id}/repositories?per_page=1", token)
        )


def _html_page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta name='viewport' content='width=device-width'>"
        f"<title>{html.escape(title)}</title><main style='max-width:38rem;margin:3rem auto;font:16px system-ui'>"
        f"{body}</main>"
    )


class Processor:
    def __init__(self, settings: Settings, store: Store, github: GitHub) -> None:
        self.settings, self.store, self.github = settings, store, github
        self._locks: dict[str, threading.Lock] = {}

    def _runner(self, token: str):
        def run(arguments: list[str], *, cwd: str, env: Mapping[str, str] | None = None, extraheader: str = "") -> str:
            merged = dict(env or os.environ)
            merged["GH_TOKEN"] = token
            merged["GITHUB_TOKEN"] = token
            return _run(arguments, cwd=cwd, env=merged, extraheader=extraheader)

        return run

    def _checkout(self, repository: str, workspace: Path, token: str) -> None:
        workspace.parent.mkdir(parents=True, exist_ok=True)
        if (workspace / ".git").is_dir():
            return
        workspace.parent.mkdir(parents=True, exist_ok=True)
        header = _git_header(token)
        command = ["git", "-c", f"{_git_header_key()}={header}", "clone", f"https://github.com/{repository}.git", str(workspace)]
        subprocess.run(command, check=True, capture_output=True, text=True)

    def _publish(self, mention: Mention, workspace: Path, token: str, run: Any) -> str:
        branch = current_branch(workspace, run=run)
        if branch in {"main", "master", mention.default_branch}:
            raise RuntimeError(f"refusing to push protected branch {branch!r}")
        run(["git", "add", "-A"], cwd=str(workspace))
        run(["git", "reset", "-q", "--", ".github/langmesh"], cwd=str(workspace))
        if not commits_to_push(workspace, run=run):
            return ""
        run(["git", "push", "-u", "origin", f"HEAD:{branch}"], cwd=str(workspace), extraheader=_git_header(token))
        owner = mention.repository.split("/", 1)[0]
        pulls = self.github.request(
            f"/repos/{mention.repository}/pulls?state=open&head={urllib.parse.quote(owner + ':' + branch)}",
            token,
        )
        if pulls:
            return str(pulls[0].get("html_url") or "")
        record = self.github.request(
            f"/repos/{mention.repository}/pulls",
            token,
            data={"title": mention.title or branch, "head": branch, "base": mention.default_branch, "body": f"Opened from {mention.html_url}", "draft": True},
        )
        return str(record.get("html_url") or "")

    def process(self, event_name: str, event: dict[str, Any], installation_id: int) -> None:
        configuration = self.store.configuration(installation_id)
        if configuration is None:
            logger.info("installation %s has no provider configuration", installation_id)
            return
        provider, model, api_key = configuration
        repository = str((event.get("repository") or {}).get("full_name") or "")
        if not repository or event_name not in {"issue_comment", "pull_request_review_comment"}:
            return
        token = self.github.installation_token(installation_id)
        slug = self.github.app_slug()
        bot_login = f"{slug}[bot]"
        if not is_mention_turn(event, repository=repository, token=token, api=self.settings.github_api_url, bot_login=bot_login):
            return
        mention = mention_from_event(
            event,
            repository=repository,
            token=token,
            api=self.settings.github_api_url,
            known_turn=True,
            bot_login=bot_login,
        )
        if mention is None or not mention.allowed:
            return
        workspace = self.settings.workspaces_path / str(installation_id) / repository.replace("/", "__")
        lock_key = str(workspace)
        lock = self._locks.setdefault(lock_key, threading.Lock())
        with lock:
            asyncio.run(self._process_locked(mention, event, workspace, token, slug, provider, model, api_key))

    async def _process_locked(self, mention: Mention, event: dict[str, Any], workspace: Path, token: str, slug: str, provider: str, model: str, api_key: str) -> None:
        self._checkout(mention.repository, workspace, token)
        runner = self._runner(token)
        if mention.kind == "pull" and not mention.head_ref:
            pull = self.github.request(f"/repos/{mention.repository}/pulls/{mention.number}", token)
            mention = mention_from_event(event, repository=mention.repository, pull=pull, known_turn=True, bot_login=f"{slug}[bot]") or mention
        ack = create_comment(mention.repository, mention.number, working_comment(""), token, self.settings.github_api_url)
        try:
            checkout = prepare_tree(mention, workspace, token=token, app_slug=slug, app_id=self.settings.app_id, run=runner)
            followup = thread_has_prior_bot_comment(event, repository=mention.repository, token=token, api=self.settings.github_api_url, bot_login=f"{slug}[bot]", ignore_ids=(ack,))

            def publish(text: str) -> None:
                publish_thread_comment(mention.repository, mention.number, text, token, self.settings.github_api_url, comment_id=ack)

            answer = await run_turn(
                mention, workspace, checkout=checkout, publish=publish, token=token,
                thread_followup=followup, provider=provider, model=model, api_key=api_key,
            )
            pull_url = self._publish(mention, workspace, token, runner) if tree_is_dirty(workspace, run=runner) or commits_to_push(workspace, run=runner) else ""
            publish_thread_comment(mention.repository, mention.number, posted_reply(answer, pull_url), token, self.settings.github_api_url, comment_id=ack)
        except Exception:
            logger.exception("hosted mention turn failed for %s#%s", mention.repository, mention.number)
            publish_thread_comment(mention.repository, mention.number, user_failure("Something went wrong while I was working on this."), token, self.settings.github_api_url, comment_id=ack)


def create_app(configuration_path: str | Path = DEFAULT_CONFIGURATION_PATH) -> FastAPI:
    settings = Settings.load(configuration_path)
    store = Store(settings)
    github = GitHub(settings)
    processor = Processor(settings, store, github)
    app = FastAPI(title="LangMesh GitHub App")

    @app.get("/github/setup", response_class=HTMLResponse)
    def setup(installation_id: int) -> Response:
        if installation_id <= 0:
            raise HTTPException(400, "invalid installation_id")
        token = store.begin_setup(installation_id)
        query = urllib.parse.urlencode({"client_id": settings.oauth_client_id, "redirect_uri": f"{settings.public_url}/github/setup/callback", "state": token})
        return RedirectResponse(f"https://github.com/login/oauth/authorize?{query}")

    @app.get("/github/setup/callback", response_class=HTMLResponse)
    def setup_callback(code: str, state: str) -> Response:
        try:
            oauth = github.oauth_token(code)
            user = github.user(oauth)
            installation_id = store.authenticate_setup(state, str(user.get("login") or ""))
            if installation_id is None:
                raise RuntimeError("setup session expired")
            record = github.verify_installation(installation_id[0], oauth)
            if not record.get("repositories") and record.get("total_count") == 0:
                raise RuntimeError("your GitHub account cannot access this installation")
        except Exception as error:
            return _html_page("LangMesh setup failed", f"<h1>Setup failed</h1><p>{html.escape(str(error))}</p>")
        response = RedirectResponse("/github/configure", status_code=303)
        response.set_cookie("langmesh_setup", state, httponly=True, secure=True, samesite="lax", max_age=600)
        return response

    @app.get("/github/configure", response_class=HTMLResponse)
    def configure_page(request: Request) -> HTMLResponse:
        setup_token = request.cookies.get("langmesh_setup", "")
        setup = store.setup(setup_token)
        if setup is None:
            return _html_page("LangMesh setup", "<h1>Setup expired</h1><p>Open the GitHub App installation link again.</p>")
        current = store.configuration(setup[0])
        provider, model = (current[0], current[1]) if current else ("", "")
        body = (
            "<h1>Configure LangMesh</h1><p>These settings apply only to this GitHub App installation.</p>"
            "<form method='post' action='/github/configure'>"
            f"<label>Provider<br><input name='provider' required value='{html.escape(provider)}'></label><br><br>"
            f"<label>Model<br><input name='model' required value='{html.escape(model)}'></label><br><br>"
            "<label>API key<br><input name='api_key' type='password' placeholder='leave blank to keep the current key'></label><br><br>"
            "<button>Save configuration</button></form>"
        )
        return _html_page("Configure LangMesh", body)

    @app.post("/github/configure", response_class=HTMLResponse)
    async def configure(request: Request) -> HTMLResponse:
        setup_token = request.cookies.get("langmesh_setup", "")
        setup = store.setup(setup_token)
        if setup is None:
            return _html_page("LangMesh setup", "<h1>Setup expired</h1>")
        form = await request.form()
        provider, model = str(form.get("provider") or "").strip(), str(form.get("model") or "").strip()
        if not provider or not model:
            return _html_page("LangMesh setup", "<h1>Provider and model are required</h1>")
        current = store.configuration(setup[0])
        api_key = str(form.get("api_key") or "")
        if not api_key and current:
            api_key = current[2]
        store.save_installation(setup[0], setup[1], "unknown", provider, model, api_key)
        return _html_page("LangMesh configured", "<h1>LangMesh is configured</h1><p>You can now mention the installed bot in a repository.</p>")

    @app.post("/github/webhook")
    async def webhook(request: Request, background_tasks: BackgroundTasks) -> Response:
        raw = await request.body()
        signature = request.headers.get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(settings.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(401, "invalid webhook signature")
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as error:
            raise HTTPException(400, "invalid JSON") from error
        if store.seen_delivery(request.headers.get("x-github-delivery", "")):
            return Response(status_code=202)
        installation_id = int(((event.get("installation") or {}).get("id") or 0))
        if not installation_id:
            return Response(status_code=202)
        background_tasks.add_task(processor.process, request.headers.get("x-github-event", ""), event, installation_id)
        return Response(status_code=202)

    return app


__all__ = ["DEFAULT_CONFIGURATION_PATH", "Settings", "create_app"]
