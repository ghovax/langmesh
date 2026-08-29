"""The installation-level GitHub App service.

This is the universal deployment path for the mention agent. GitHub sends webhook
events here; the service owns the App credentials, and each installation owns its
provider/model settings. Nothing in a customer repository is used as configuration.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import logging
import os
import tempfile
import time
import urllib.parse
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import jwt
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from models_provider import (
    InMemoryCredentialStore,
    OAuthTokens,
    ProviderAuthentication,
)
from pydantic import BaseModel, Field
from langmesh import SQLAlchemyCheckpoints
from langmesh.base.contracts.ports import Checkpoints
from langmesh.github.detect import is_mention_turn, thread_has_prior_bot_comment
from langmesh.runtime.plugins.compaction.configuration import CompactionConfiguration
from langmesh.github.mention import (
    Mention,
    _git_header,
    _run,
    acknowledgement,
    commits_to_push,
    create_comment,
    current_branch,
    mention_from_event,
    posted_reply,
    prepare_tree,
    run_turn,
    tree_is_dirty,
    update_comment,
)
from langmesh.github.storage import InstallationConfiguration, Store

logger = logging.getLogger("langmesh.github.hosted")
DEFAULT_CONFIGURATION_PATH = Path.home() / ".config" / "langmesh" / "github.yaml"


class ConfigurationUpdate(BaseModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_key: str | None = None


class AuthenticationStart(BaseModel):
    model: str = Field(min_length=1)


@dataclass(frozen=True)
class Settings:
    app_id: str
    private_key_path: Path
    webhook_secret: str
    oauth_client_id: str
    oauth_client_secret: str
    encryption_key_path: Path
    database_url: str
    queue_poll_seconds: float
    maximum_delivery_attempts: int
    public_url: str
    compaction: CompactionConfiguration
    provider_application_ids: Mapping[str, str] = field(default_factory=dict)
    github_api_url: str = "https://api.github.com"

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CONFIGURATION_PATH) -> "Settings":
        configuration_path = Path(path).expanduser().resolve()
        try:
            values = yaml.safe_load(configuration_path.read_text(encoding="utf-8")) or {}
        except OSError as error:
            raise RuntimeError(
                f"GitHub App configuration is missing: {configuration_path}"
            ) from error
        if not isinstance(values, dict):
            raise RuntimeError(
                f"GitHub App configuration must be a YAML mapping: {configuration_path}"
            )

        def section(name: str) -> Mapping[str, Any]:
            value = values.get(name)
            if not isinstance(value, Mapping):
                raise RuntimeError(
                    f"GitHub App configuration needs a {name!r} section: {configuration_path}"
                )
            return value

        def section_from(source: Mapping[str, Any], name: str, path: Path) -> Mapping[str, Any]:
            value = source.get(name)
            if not isinstance(value, Mapping):
                raise RuntimeError(f"GitHub App configuration needs a {name!r} section: {path}")
            return value

        def required(source: Mapping[str, Any], name: str, section_name: str) -> str:
            value = str(source.get(name) or "").strip()
            if not value:
                raise RuntimeError(
                    f"GitHub App configuration needs {section_name}.{name!s}: {configuration_path}"
                )
            return value

        github = section("github")
        app = section_from(github, "app", configuration_path)
        webhook = section_from(github, "webhook", configuration_path)
        oauth = section_from(github, "oauth", configuration_path)
        server = section("server")
        storage = section("storage")
        database = section_from(storage, "database", configuration_path)
        encryption = section_from(storage, "encryption", configuration_path)
        queue = section_from(storage, "queue", configuration_path)
        raw_compaction = values.get("compaction", {})
        if not isinstance(raw_compaction, Mapping):
            raise RuntimeError(
                f"GitHub App configuration needs a compaction mapping: {configuration_path}"
            )
        compaction_values: dict[str, Any] = {
            "automatic": True,
            "reclaim_at_fraction": 0.9,
            "output_reserve_fraction": 0.1,
            "recent_working_set_fraction": 0.15,
            "maximum_context_tokens": 98_304,
        }
        compaction_values.update(raw_compaction)
        try:
            compaction = CompactionConfiguration.model_validate(compaction_values)
        except ValueError as error:
            raise RuntimeError(
                f"GitHub App configuration has invalid compaction settings: {configuration_path}"
            ) from error
        try:
            maximum_delivery_attempts = int(queue.get("maximum_delivery_attempts", 5))
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "GitHub App configuration needs storage.queue.maximum_delivery_attempts "
                f"to be a positive integer: {configuration_path}"
            ) from error
        if maximum_delivery_attempts < 1:
            raise RuntimeError(
                "GitHub App configuration needs storage.queue.maximum_delivery_attempts "
                f"to be a positive integer: {configuration_path}"
            )
        return cls(
            app_id=required(app, "id", "github.app"),
            private_key_path=Path(required(app, "private_key_path", "github.app")).expanduser(),
            webhook_secret=required(webhook, "secret", "github.webhook"),
            oauth_client_id=required(oauth, "client_id", "github.oauth"),
            oauth_client_secret=required(oauth, "client_secret", "github.oauth"),
            encryption_key_path=Path(
                required(encryption, "key_path", "storage.encryption")
            ).expanduser(),
            database_url=required(database, "url", "storage.database"),
            queue_poll_seconds=max(0.5, float(queue.get("poll_seconds") or 5)),
            maximum_delivery_attempts=maximum_delivery_attempts,
            public_url=required(server, "public_url", "server").rstrip("/"),
            compaction=compaction,
            github_api_url=str(github.get("api_url") or "https://api.github.com").rstrip("/"),
            provider_application_ids={
                str(provider).strip().lower(): str(application_id).strip()
                for provider, application_id in (
                    oauth.get("provider_application_ids", {})
                    if isinstance(oauth.get("provider_application_ids", {}), Mapping)
                    else {}
                ).items()
                if str(provider).strip() and str(application_id).strip()
            },
        )


class GitHub:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.private_key = settings.private_key_path.read_text(encoding="utf-8")
        self._slug = ""

    def _jwt(self) -> str:
        now = int(time.time())
        return str(
            jwt.encode(
                {"iat": now - 60, "exp": now + 540, "iss": self.settings.app_id},
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
                "User-Agent": "langmesh-github-agent",
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


class Processor:
    def __init__(self, settings: Settings, store: Store, github: GitHub) -> None:
        self.settings, self.store, self.github = settings, store, github
        self.authentication = ProviderAuthentication()
        self._checkpoints: Checkpoints = SQLAlchemyCheckpoints(
            store.engine, table="github_session_checkpoints"
        )
        self._locks: dict[str, asyncio.Lock] = {}

    async def initialize(self) -> None:
        initialize = getattr(self._checkpoints, "initialize", None)
        if initialize is not None:
            await initialize()

    async def run_forever(self) -> None:
        while True:
            delivery = await self.store.claim()
            if delivery is None:
                await asyncio.sleep(self.settings.queue_poll_seconds)
                continue
            delivery_id = str(delivery["delivery_id"])
            attempts = int(delivery.get("attempts") or 1)
            recovered = bool(delivery.get("recovered"))
            event_name = str(delivery["event_name"])
            installation_id = int(delivery["installation_id"])
            logger.info(
                "claimed GitHub delivery id=%s event=%s installation=%s attempt=%s recovered=%s",
                delivery_id,
                event_name,
                installation_id,
                attempts,
                recovered,
            )
            if attempts > self.settings.maximum_delivery_attempts:
                await self.store.mark_failed(
                    delivery_id,
                    "delivery exceeded the configured attempt limit",
                )
                logger.error(
                    "discarded GitHub delivery id=%s beyond the %s-attempt limit",
                    delivery_id,
                    self.settings.maximum_delivery_attempts,
                )
                continue
            try:
                await self.process(
                    event_name,
                    json.loads(str(delivery["payload"])),
                    installation_id,
                    delivery_id=delivery_id,
                    attempt=attempts,
                    recovered=recovered,
                )
            except Exception as error:
                logger.exception("GitHub delivery %s failed", delivery_id)
                if attempts >= self.settings.maximum_delivery_attempts:
                    await self.store.mark_failed(delivery_id, str(error))
                    logger.error(
                        "stopped GitHub delivery id=%s after %s attempts",
                        delivery_id,
                        attempts,
                    )
                else:
                    await self.store.schedule_retry(
                        delivery_id, str(error), min(300, 2 ** min(attempts, 8))
                    )
            else:
                await self.store.complete(delivery_id)
                logger.info("completed GitHub delivery id=%s", delivery_id)

    def _runner(self, token: str):
        def run(
            arguments: list[str],
            *,
            cwd: str,
            env: Mapping[str, str] | None = None,
            extraheader: str = "",
        ) -> str:
            merged = dict(env or os.environ)
            merged["GH_TOKEN"] = token
            merged["GITHUB_TOKEN"] = token
            return _run(arguments, cwd=cwd, env=merged, extraheader=extraheader)

        return run

    def _checkout(self, repository: str, workspace: Path, token: str) -> None:
        workspace.parent.mkdir(parents=True, exist_ok=True)
        if (workspace / ".git").is_dir():
            return
        header = _git_header(token)
        command = [
            "git",
            "clone",
            f"https://github.com/{repository}.git",
            str(workspace),
        ]
        _run(command, cwd=str(workspace.parent), extraheader=header)

    def _publish(self, mention: Mention, workspace: Path, token: str, run: Any) -> str:
        branch = current_branch(workspace, run=run)
        if branch in {"main", "master", mention.default_branch}:
            raise RuntimeError(f"refusing to push protected branch {branch!r}")
        run(["git", "add", "-A"], cwd=str(workspace))
        if not commits_to_push(workspace, run=run):
            return ""
        run(
            ["git", "push", "-u", "origin", f"HEAD:{branch}"],
            cwd=str(workspace),
            extraheader=_git_header(token),
        )
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
            data={
                "title": mention.title or branch,
                "head": branch,
                "base": mention.default_branch,
                "body": f"Opened from {mention.html_url}",
                "draft": True,
            },
        )
        return str(record.get("html_url") or "")

    def _credential_store(
        self, configuration: InstallationConfiguration
    ) -> InMemoryCredentialStore | None:
        if not configuration.oauth_tokens:
            return None
        credentials = InMemoryCredentialStore()
        credentials.save(
            configuration.provider,
            self.authentication.deserialize_token(
                configuration.provider, configuration.oauth_tokens
            ),
        )
        return credentials

    async def process(
        self,
        event_name: str,
        event: dict[str, Any],
        installation_id: int,
        *,
        delivery_id: str = "",
        attempt: int = 0,
        recovered: bool = False,
    ) -> None:
        configuration = await self.store.configuration(installation_id)
        if configuration is None or not configuration.ready:
            logger.info(
                "ignoring delivery id=%s installation=%s: no provider configuration",
                delivery_id,
                installation_id,
            )
            return
        provider, model, api_key = (
            configuration.provider,
            configuration.model,
            configuration.api_key,
        )
        credential_store = self._credential_store(configuration)
        repository = str((event.get("repository") or {}).get("full_name") or "")
        if not repository or event_name not in {
            "issues",
            "pull_request",
            "issue_comment",
            "pull_request_review_comment",
        }:
            return
        token = await asyncio.to_thread(self.github.installation_token, installation_id)
        slug = await asyncio.to_thread(self.github.app_slug)
        bot_login = f"{slug}[bot]"
        if not await asyncio.to_thread(
            is_mention_turn,
            event,
            event_name=event_name,
            repository=repository,
            token=token,
            api=self.settings.github_api_url,
            bot_login=bot_login,
        ):
            return
        mention = mention_from_event(
            event,
            event_name=event_name,
            repository=repository,
            token=token,
            api=self.settings.github_api_url,
            known_turn=True,
            bot_login=bot_login,
        )
        if mention is None or not mention.allowed:
            return
        logger.info(
            "processing delivery id=%s attempt=%s installation=%s repository=%s session=%s",
            delivery_id,
            attempt,
            installation_id,
            repository,
            mention.session_id,
        )
        lock_key = f"{installation_id}:{repository}"
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            with tempfile.TemporaryDirectory(prefix="langmesh-github-") as temporary_directory:
                workspace = Path(temporary_directory) / repository.replace("/", "__")
                await self._process_locked(
                    mention,
                    event,
                    workspace,
                    token,
                    slug,
                    provider,
                    model,
                    api_key,
                    credential_store,
                    installation_id,
                    delivery_id=delivery_id,
                    attempt=attempt,
                    recovered=recovered,
                )

    async def _process_locked(
        self,
        mention: Mention,
        event: dict[str, Any],
        workspace: Path,
        token: str,
        slug: str,
        provider: str,
        model: str,
        api_key: str,
        credential_store: InMemoryCredentialStore | None,
        installation_id: int,
        *,
        delivery_id: str,
        attempt: int,
        recovered: bool,
    ) -> None:
        ack = await self.store.comment_id_for_delivery(delivery_id)
        if ack is None:
            ack = await asyncio.to_thread(
                create_comment,
                mention.repository,
                mention.number,
                acknowledgement(),
                token,
                self.settings.github_api_url,
            )
            await self.store.remember_comment_id(delivery_id, ack)

        async def update_existing_comment(message: str) -> None:
            try:
                await asyncio.to_thread(
                    update_comment,
                    mention.repository,
                    ack,
                    message.strip(),
                    token,
                    self.settings.github_api_url,
                )
            except Exception:
                logger.exception("could not update GitHub comment %s", ack)

        if recovered:
            await update_existing_comment(
                "The worker was interrupted before it could finish. It recovered the saved "
                "session and is retrying now."
            )
        elif attempt > 1:
            await update_existing_comment("The worker is retrying after an earlier failure.")

        logger.info(
            "started GitHub mention delivery id=%s attempt=%s session=%s acknowledgement=%s",
            delivery_id,
            attempt,
            mention.session_id,
            ack,
        )

        async def process_mention() -> None:
            active_mention = mention
            await asyncio.to_thread(self._checkout, mention.repository, workspace, token)
            runner = self._runner(token)
            if active_mention.kind == "pull" and not active_mention.head_ref:
                pull = await asyncio.to_thread(
                    self.github.request,
                    f"/repos/{active_mention.repository}/pulls/{active_mention.number}",
                    token,
                )
                active_mention = (
                    mention_from_event(
                        event,
                        event_name="pull_request",
                        repository=active_mention.repository,
                        pull=pull,
                        known_turn=True,
                        bot_login=f"{slug}[bot]",
                    )
                    or active_mention
                )
            checkout = await asyncio.to_thread(
                prepare_tree,
                active_mention,
                workspace,
                token=token,
                app_slug=slug,
                app_id=self.settings.app_id,
                run=runner,
            )
            followup = await asyncio.to_thread(
                thread_has_prior_bot_comment,
                event,
                repository=active_mention.repository,
                token=token,
                api=self.settings.github_api_url,
                bot_login=f"{slug}[bot]",
                ignore_ids=(ack,),
            )

            try:
                answer = await run_turn(
                    active_mention,
                    workspace,
                    checkout=checkout,
                    update_comment=update_existing_comment,
                    token=token,
                    thread_followup=followup,
                    provider=provider,
                    model=model,
                    api_key=api_key,
                    checkpoints=self._checkpoints,
                    credential_store=credential_store,
                    compaction_configuration=self.settings.compaction,
                )
            finally:
                if credential_store is not None:
                    refreshed = credential_store.load(provider)
                    if isinstance(refreshed, OAuthTokens):
                        await self.store.save_oauth_tokens(
                            installation_id,
                            provider,
                            "",
                            refreshed,
                            self.authentication,
                        )

            def publish_if_needed() -> str:
                if not tree_is_dirty(workspace, run=runner) and not commits_to_push(
                    workspace, run=runner
                ):
                    return ""
                return self._publish(active_mention, workspace, token, runner)

            pull_url = await asyncio.to_thread(publish_if_needed)
            await update_existing_comment(posted_reply(answer, pull_url))
            logger.info(
                "finished GitHub mention delivery id=%s attempt=%s session=%s pull_request=%s",
                delivery_id,
                attempt,
                active_mention.session_id,
                bool(pull_url),
            )

        try:
            await process_mention()
        except Exception:
            logger.exception(
                "hosted mention delivery failed id=%s attempt=%s session=%s for %s#%s",
                delivery_id,
                attempt,
                mention.session_id,
                mention.repository,
                mention.number,
            )
            if attempt >= self.settings.maximum_delivery_attempts:
                await update_existing_comment(
                    "The worker stopped after reaching the delivery attempt limit."
                )
            else:
                await update_existing_comment(
                    "The worker encountered a failure. It will retry shortly and keep this "
                    "comment updated."
                )
            raise


def create_app(configuration_path: str | Path = DEFAULT_CONFIGURATION_PATH) -> FastAPI:
    settings = Settings.load(configuration_path)
    store = Store(settings.database_url, settings.encryption_key_path)
    github = GitHub(settings)
    processor = Processor(settings, store, github)
    worker: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal worker
        await store.initialize()
        await processor.initialize()
        worker = asyncio.create_task(processor.run_forever())
        try:
            yield
        finally:
            if worker is not None:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            await store.close()

    app = FastAPI(title="LangMesh GitHub App", lifespan=lifespan)

    @app.api_route("/", methods=["GET", "HEAD"])
    async def root() -> dict[str, str]:
        return {"service": "langmesh-agent", "status": "ok"}

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/github/setup")
    async def setup(installation_id: int) -> Response:
        if installation_id <= 0:
            raise HTTPException(400, "invalid installation_id")
        token = await store.begin_setup(installation_id)
        query = urllib.parse.urlencode(
            {
                "client_id": settings.oauth_client_id,
                "redirect_uri": f"{settings.public_url}/github/setup/callback",
                "state": token,
            }
        )
        return RedirectResponse(f"https://github.com/login/oauth/authorize?{query}")

    @app.get("/github/setup/callback")
    async def setup_callback(code: str, state: str) -> dict[str, Any]:
        try:
            oauth = await asyncio.to_thread(github.oauth_token, code)
            user = await asyncio.to_thread(github.user, oauth)
            installation_id = await store.authenticate_setup(state, str(user.get("login") or ""))
            if installation_id is None:
                raise RuntimeError("setup session expired")
            record = await asyncio.to_thread(github.verify_installation, installation_id[0], oauth)
            if not record.get("repositories") and record.get("total_count") == 0:
                raise RuntimeError("your GitHub account cannot access this installation")
        except Exception as error:
            raise HTTPException(400, detail=str(error)) from error
        return {
            "installation_id": installation_id[0],
            "setup_token": state,
            "expires_in": 600,
            "configuration_url": f"{settings.public_url}/github/configuration",
        }

    def setup_token(request: Request) -> str:
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise HTTPException(401, detail="use Authorization: Bearer <setup-token>")
        return token.strip()

    authentication = ProviderAuthentication()

    def provider_redirect_uri(provider: str) -> str:
        provider_identifier = provider.strip().lower()
        return authentication.redirect_uri(provider_identifier)

    async def require_oauth_provider(provider: str) -> str:
        provider_identifier = provider.strip().lower()
        profile = authentication.profile(provider_identifier)
        if profile.method != "oauth":
            raise HTTPException(
                422, detail=f"{provider_identifier} does not use OAuth authentication"
            )
        return provider_identifier

    def configuration_response(
        setup: tuple[int, str], current: InstallationConfiguration | None
    ) -> dict[str, Any]:
        configuration = None
        if current is not None and current.model:
            configuration = {
                "provider": current.provider,
                "model": current.model,
                "authentication": authentication.profile(current.provider).method,
            }
        return {
            "installation_id": setup[0],
            "account": setup[1],
            "configuration": configuration,
            "ready": bool(current and current.ready),
        }

    @app.post("/github/auth/{provider}/start")
    async def start_provider_authentication(
        provider: str, payload: AuthenticationStart, request: Request
    ) -> dict[str, Any]:
        setup = await store.setup(setup_token(request))
        if setup is None:
            raise HTTPException(401, detail="setup token is missing or expired")
        provider_identifier = await require_oauth_provider(provider)
        model = payload.model.strip()
        if not model:
            raise HTTPException(422, detail="model is required")
        try:
            authorization = authentication.authorization_request(
                provider_identifier,
                provider_redirect_uri(provider_identifier),
                client_id=settings.provider_application_ids.get(provider_identifier, ""),
            )
        except Exception as error:  # noqa: BLE001 — translate provider capability errors
            raise HTTPException(422, detail=str(error)) from error
        await store.begin_oauth_authorization(
            setup[0],
            setup[1],
            provider_identifier,
            model,
            authorization.state,
            authorization.code_verifier,
            provider_redirect_uri(provider_identifier),
        )
        return {
            "provider": provider_identifier,
            "model": model,
            "authorize_url": authorization.authorize_url,
            "callback_url": provider_redirect_uri(provider_identifier),
            "completion_url": f"{settings.public_url}/github/auth/{urllib.parse.quote(provider_identifier, safe='')}/complete",
            "expires_in": 600,
        }

    async def complete_provider_authentication(
        provider: str, *, code: str, state: str, authorization_error: str
    ) -> dict[str, Any]:
        provider_identifier = await require_oauth_provider(provider)
        authorization_record = await store.oauth_authorization(provider_identifier, state)
        if authorization_record is None:
            raise HTTPException(400, detail="OAuth authorization state is missing or expired")
        if authorization_error:
            await store.consume_oauth_authorization(state)
            raise HTTPException(
                400,
                detail=f"OAuth authorization failed: {authorization_error}",
            )
        installation_id, _user_login, model, code_verifier, redirect_uri = authorization_record
        if redirect_uri and not code.strip():
            raise HTTPException(400, detail="OAuth authorization code is required")
        try:
            authorization = authentication.authorization_request(
                provider_identifier,
                redirect_uri,
                client_id=settings.provider_application_ids.get(provider_identifier, ""),
                state=state,
                code_verifier=code_verifier,
            )
            tokens = await authorization.exchange(code)
            await store.save_oauth_tokens(
                installation_id, provider_identifier, model, tokens, authentication
            )
            if not await store.consume_oauth_authorization(state):
                raise RuntimeError("OAuth authorization was already completed")
        except Exception as error:  # noqa: BLE001 — the callback must return an HTTP error
            raise HTTPException(400, detail=str(error)) from error
        return {
            "authenticated": True,
            "provider": provider_identifier,
            "model": model,
            "configuration_url": f"{settings.public_url}/github/configuration",
        }

    @app.get("/github/auth/{provider}/callback")
    async def provider_authentication_callback(
        provider: str, code: str = "", state: str = "", error: str = ""
    ) -> dict[str, Any]:
        if not code.strip() and not error:
            raise HTTPException(400, detail="OAuth authorization returned no code")
        return await complete_provider_authentication(
            provider,
            code=code,
            state=state,
            authorization_error=error,
        )

    @app.get("/github/auth/{provider}/complete")
    async def complete_provider_browser_authentication(
        provider: str, code: str = "", state: str = "", error: str = ""
    ) -> dict[str, Any]:
        return await complete_provider_authentication(
            provider,
            code=code,
            state=state,
            authorization_error=error,
        )

    @app.get("/github/configuration")
    async def configuration_page(request: Request) -> dict[str, Any]:
        setup = await store.setup(setup_token(request))
        if setup is None:
            raise HTTPException(401, detail="setup token is missing or expired")
        current = await store.configuration(setup[0])
        return configuration_response(setup, current)

    @app.put("/github/configuration")
    async def save_configuration(payload: ConfigurationUpdate, request: Request) -> dict[str, Any]:
        token = setup_token(request)
        setup = await store.setup(token)
        if setup is None:
            raise HTTPException(401, detail="setup token is missing or expired")
        provider, model = payload.provider.strip().lower(), payload.model.strip()
        api_key = (payload.api_key or "").strip()
        current = await store.configuration(setup[0])
        if not provider or not model:
            raise HTTPException(422, detail="provider and model are required")
        profile = authentication.profile(provider)
        if profile.method == "oauth":
            if api_key:
                raise HTTPException(422, detail=f"{provider} uses OAuth; do not provide an api_key")
            if current is None or not current.oauth_tokens:
                raise HTTPException(
                    422, detail=f"connect {provider} OAuth before selecting this provider"
                )
            await store.save_installation(
                setup[0], setup[1], "unknown", provider, model, "", clear_oauth=False
            )
        else:
            if not api_key and current:
                api_key = current.api_key
            if not api_key:
                raise HTTPException(422, detail="provider, model, and api_key are required")
            await store.save_installation(
                setup[0], setup[1], "unknown", provider, model, api_key, clear_oauth=True
            )
        return configuration_response(setup, await store.configuration(setup[0]))

    @app.post("/github/webhook")
    async def webhook(request: Request) -> Response:
        raw = await request.body()
        signature = request.headers.get("x-hub-signature-256", "")
        expected = (
            "sha256=" + hmac.new(settings.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
        )
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(401, "invalid webhook signature")
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as error:
            raise HTTPException(400, "invalid JSON") from error
        installation_id = int(((event.get("installation") or {}).get("id") or 0))
        if not installation_id:
            return Response(status_code=202)
        await store.enqueue(
            request.headers.get("x-github-delivery", ""),
            request.headers.get("x-github-event", ""),
            installation_id,
            raw.decode("utf-8"),
        )
        return Response(status_code=202)

    return app


__all__ = ["DEFAULT_CONFIGURATION_PATH", "Settings", "create_app"]
