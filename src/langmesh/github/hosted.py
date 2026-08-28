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
import secrets
import subprocess
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
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from models_provider import (
    InMemoryCredentialStore,
    OAuthTokens,
    ProviderAuthentication,
)
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from langmesh import SQLAlchemyCheckpoints
from langmesh.base.contracts.ports import Checkpoints
from langmesh.github.detect import is_mention_turn, thread_has_prior_bot_comment
from langmesh.github.mention import (
    Mention,
    _git_header,
    _git_header_key,
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

logger = logging.getLogger("langmesh.github.hosted")
DEFAULT_CONFIGURATION_PATH = Path.home() / ".config" / "langmesh" / "github.yaml"


class ConfigurationUpdate(BaseModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_key: str | None = None


class AuthenticationStart(BaseModel):
    model: str = Field(min_length=1)


@dataclass(frozen=True)
class InstallationConfiguration:
    """The selected model and its installation-owned authentication."""

    provider: str
    model: str
    api_key: str
    oauth_tokens: Mapping[str, Any] | None

    @property
    def ready(self) -> bool:
        if not self.model:
            return False
        profile = ProviderAuthentication().profile(self.provider)
        return bool(self.oauth_tokens) if profile.method == "oauth" else bool(self.api_key)


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


class Store:
    """Durable GitHub service state in a caller-owned external SQL database."""

    def __init__(self, settings: Settings) -> None:
        self._cipher = Fernet(settings.encryption_key_path.read_bytes().strip())
        self.engine: AsyncEngine = create_async_engine(settings.database_url, pool_pre_ping=True)

    async def initialize(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS langmesh_github_installations (
                installation_id BIGINT PRIMARY KEY,
                account_login TEXT NOT NULL,
                account_type TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                api_key TEXT NOT NULL DEFAULT '',
                oauth_tokens TEXT NOT NULL DEFAULT '',
                updated_at BIGINT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS langmesh_github_deliveries (
                delivery_id TEXT PRIMARY KEY,
                event_name TEXT NOT NULL,
                installation_id BIGINT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                received_at BIGINT NOT NULL,
                claimed_at BIGINT,
                attempts BIGINT NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                comment_id BIGINT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS langmesh_github_setup_sessions (
                token TEXT PRIMARY KEY,
                installation_id BIGINT NOT NULL,
                user_login TEXT NOT NULL DEFAULT '',
                expires_at BIGINT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS langmesh_github_oauth_authorizations (
                state TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                installation_id BIGINT NOT NULL,
                user_login TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL,
                code_verifier TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                expires_at BIGINT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS langmesh_github_deliveries_ready ON langmesh_github_deliveries (status, received_at)",
            "CREATE INDEX IF NOT EXISTS langmesh_github_setup_sessions_expiry ON langmesh_github_setup_sessions (expires_at)",
            "CREATE INDEX IF NOT EXISTS langmesh_github_oauth_authorizations_expiry ON langmesh_github_oauth_authorizations (expires_at)",
            "DROP TABLE IF EXISTS langmesh_github_chatgpt_authorizations",
        )
        async with self.engine.begin() as connection:
            for statement in statements:
                await connection.execute(text(statement))
            if connection.dialect.name == "sqlite":
                try:
                    await connection.execute(
                        text(
                            "ALTER TABLE langmesh_github_deliveries ADD COLUMN "
                            "next_attempt_at BIGINT NOT NULL DEFAULT 0"
                        )
                    )
                except Exception:
                    pass
                try:
                    await connection.execute(
                        text("ALTER TABLE langmesh_github_deliveries ADD COLUMN comment_id BIGINT")
                    )
                except Exception:
                    pass
                try:
                    await connection.execute(
                        text(
                            "ALTER TABLE langmesh_github_installations ADD COLUMN "
                            "oauth_tokens TEXT NOT NULL DEFAULT ''"
                        )
                    )
                except Exception:
                    pass
                try:
                    await connection.execute(
                        text(
                            "ALTER TABLE langmesh_github_oauth_authorizations ADD COLUMN "
                            "model TEXT NOT NULL DEFAULT ''"
                        )
                    )
                except Exception:
                    pass
                try:
                    await connection.execute(
                        text(
                            "ALTER TABLE langmesh_github_oauth_authorizations ADD COLUMN "
                            "redirect_uri TEXT NOT NULL DEFAULT ''"
                        )
                    )
                except Exception:
                    pass
            else:
                await connection.execute(
                    text(
                        "ALTER TABLE langmesh_github_deliveries ADD COLUMN IF NOT EXISTS "
                        "next_attempt_at BIGINT NOT NULL DEFAULT 0"
                    )
                )
                await connection.execute(
                    text(
                        "ALTER TABLE langmesh_github_deliveries ADD COLUMN IF NOT EXISTS "
                        "comment_id BIGINT"
                    )
                )
                await connection.execute(
                    text(
                        "ALTER TABLE langmesh_github_installations ADD COLUMN IF NOT EXISTS "
                        "oauth_tokens TEXT NOT NULL DEFAULT ''"
                    )
                )
                await connection.execute(
                    text(
                        "ALTER TABLE langmesh_github_oauth_authorizations ADD COLUMN IF NOT EXISTS "
                        "model TEXT NOT NULL DEFAULT ''"
                    )
                )
                await connection.execute(
                    text(
                        "ALTER TABLE langmesh_github_oauth_authorizations ADD COLUMN IF NOT EXISTS "
                        "redirect_uri TEXT NOT NULL DEFAULT ''"
                    )
                )

    async def close(self) -> None:
        await self.engine.dispose()

    async def enqueue(
        self, delivery_id: str, event_name: str, installation_id: int, payload: str
    ) -> bool:
        if not delivery_id:
            return False
        async with self.engine.begin() as connection:
            result = await connection.execute(
                text(
                    "INSERT INTO langmesh_github_deliveries "
                    "(delivery_id, event_name, installation_id, payload, status, received_at) "
                    "VALUES (:delivery_id, :event_name, :installation_id, :payload, 'queued', :received_at) "
                    "ON CONFLICT (delivery_id) DO NOTHING RETURNING delivery_id"
                ),
                {
                    "delivery_id": delivery_id,
                    "event_name": event_name,
                    "installation_id": installation_id,
                    "payload": payload,
                    "received_at": int(time.time()),
                },
            )
        return result.scalar_one_or_none() is not None

    async def claim(self, stale_after: int = 900) -> dict[str, Any] | None:
        now = int(time.time())
        async with self.engine.begin() as connection:
            result = await connection.execute(
                text(
                    "SELECT delivery_id, event_name, installation_id, payload, status, attempts "
                    "FROM langmesh_github_deliveries "
                    "WHERE (status = 'queued' AND next_attempt_at <= :now) "
                    "OR (status = 'processing' AND claimed_at < :stale_at) "
                    "ORDER BY received_at LIMIT 1 FOR UPDATE SKIP LOCKED"
                ),
                {"now": now, "stale_at": now - stale_after},
            )
            row = result.mappings().first()
            if row is None:
                return None
            await connection.execute(
                text(
                    "UPDATE langmesh_github_deliveries SET status = 'processing', claimed_at = :claimed_at, "
                    "attempts = attempts + 1 WHERE delivery_id = :delivery_id"
                ),
                {"claimed_at": now, "delivery_id": row["delivery_id"]},
            )
        claimed = dict(row)
        claimed["recovered"] = claimed["status"] == "processing"
        claimed["attempts"] = int(claimed["attempts"]) + 1
        return claimed

    async def complete(self, delivery_id: str) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE langmesh_github_deliveries SET status = 'completed' WHERE delivery_id = :delivery_id"
                ),
                {"delivery_id": delivery_id},
            )

    async def schedule_retry(self, delivery_id: str, error: str, delay: int) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE langmesh_github_deliveries SET status = 'queued', claimed_at = NULL, "
                    "next_attempt_at = :next_attempt_at, last_error = :error "
                    "WHERE delivery_id = :delivery_id"
                ),
                {
                    "delivery_id": delivery_id,
                    "error": error[:4000],
                    "next_attempt_at": int(time.time()) + max(1, delay),
                },
            )

    async def mark_failed(self, delivery_id: str, error: str) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE langmesh_github_deliveries SET status = 'failed', claimed_at = NULL, "
                    "next_attempt_at = 0, last_error = :error WHERE delivery_id = :delivery_id"
                ),
                {"delivery_id": delivery_id, "error": error[:4000]},
            )

    async def comment_id_for_delivery(self, delivery_id: str) -> int | None:
        async with self.engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT comment_id FROM langmesh_github_deliveries "
                    "WHERE delivery_id = :delivery_id"
                ),
                {"delivery_id": delivery_id},
            )
            value = result.scalar_one_or_none()
        return int(value) if value is not None else None

    async def remember_comment_id(self, delivery_id: str, comment_id: int) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE langmesh_github_deliveries SET comment_id = :comment_id "
                    "WHERE delivery_id = :delivery_id"
                ),
                {"delivery_id": delivery_id, "comment_id": comment_id},
            )

    async def configuration(self, installation_id: int) -> InstallationConfiguration | None:
        async with self.engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT provider, model, api_key, oauth_tokens FROM langmesh_github_installations "
                    "WHERE installation_id = :installation_id"
                ),
                {"installation_id": installation_id},
            )
            row = result.first()
        if not row or not row[0]:
            return None
        key = self._cipher.decrypt(str(row[2]).encode()).decode() if row[2] else ""
        oauth_tokens: Mapping[str, Any] | None = None
        if row[3]:
            try:
                decoded = json.loads(self._cipher.decrypt(str(row[3]).encode()).decode())
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                raise RuntimeError("Stored OAuth credentials are invalid") from error
            if not isinstance(decoded, Mapping):
                raise RuntimeError("Stored OAuth credentials are invalid")
            oauth_tokens = dict(decoded)
        return InstallationConfiguration(str(row[0]), str(row[1]), key, oauth_tokens)

    async def save_installation(
        self,
        installation_id: int,
        account_login: str,
        account_type: str,
        provider: str,
        model: str,
        api_key: str | None,
        *,
        clear_oauth: bool = False,
    ) -> None:
        encrypted = self._cipher.encrypt(api_key.encode()).decode() if api_key else ""
        async with self.engine.begin() as connection:
            result = await connection.execute(
                text(
                    "SELECT api_key, oauth_tokens FROM langmesh_github_installations "
                    "WHERE installation_id = :installation_id"
                ),
                {"installation_id": installation_id},
            )
            row = result.first()
            if api_key is None:
                encrypted = str(row[0]) if row and row[0] else ""
            existing_oauth = str(row[1]) if row and row[1] else ""
            if clear_oauth:
                existing_oauth = ""
            await connection.execute(
                text(
                    "INSERT INTO langmesh_github_installations "
                    "(installation_id, account_login, account_type, provider, model, api_key, oauth_tokens, updated_at) "
                    "VALUES (:installation_id, :account_login, :account_type, :provider, :model, :api_key, :oauth_tokens, :updated_at) "
                    "ON CONFLICT (installation_id) DO UPDATE SET account_login = EXCLUDED.account_login, "
                    "account_type = EXCLUDED.account_type, provider = EXCLUDED.provider, model = EXCLUDED.model, "
                    "api_key = EXCLUDED.api_key, "
                    "oauth_tokens = CASE WHEN :clear_oauth THEN '' ELSE EXCLUDED.oauth_tokens END, "
                    "updated_at = EXCLUDED.updated_at"
                ),
                {
                    "installation_id": installation_id,
                    "account_login": account_login,
                    "account_type": account_type,
                    "provider": provider,
                    "model": model,
                    "api_key": encrypted,
                    "oauth_tokens": existing_oauth,
                    "clear_oauth": clear_oauth,
                    "updated_at": int(time.time()),
                },
            )

    async def save_oauth_tokens(
        self,
        installation_id: int,
        provider: str,
        model: str,
        tokens: OAuthTokens,
        authentication: ProviderAuthentication,
    ) -> None:
        encrypted = self._cipher.encrypt(
            json.dumps(
                authentication.serialize_token(provider, tokens), separators=(",", ":")
            ).encode()
        ).decode()
        async with self.engine.begin() as connection:
            result = await connection.execute(
                text(
                    "SELECT provider, model FROM langmesh_github_installations "
                    "WHERE installation_id = :installation_id"
                ),
                {"installation_id": installation_id},
            )
            existing = result.first()
            existing_model = (
                str(existing[1]) if existing is not None and str(existing[0]) == provider else ""
            )
            selected_model = model.strip() or existing_model
            if not selected_model:
                raise ValueError("OAuth authentication requires a model")
            if existing is None:
                await connection.execute(
                    text(
                        "INSERT INTO langmesh_github_installations "
                        "(installation_id, account_login, account_type, provider, model, api_key, oauth_tokens, updated_at) "
                        "VALUES (:installation_id, '', 'unknown', :provider, :model, '', :oauth_tokens, :updated_at)"
                    ),
                    {
                        "installation_id": installation_id,
                        "provider": provider,
                        "model": selected_model,
                        "oauth_tokens": encrypted,
                        "updated_at": int(time.time()),
                    },
                )
            else:
                await connection.execute(
                    text(
                        "UPDATE langmesh_github_installations SET provider = :provider, "
                        "model = :model, "
                        "api_key = '', oauth_tokens = :oauth_tokens, updated_at = :updated_at "
                        "WHERE installation_id = :installation_id"
                    ),
                    {
                        "installation_id": installation_id,
                        "provider": provider,
                        "model": selected_model,
                        "oauth_tokens": encrypted,
                        "updated_at": int(time.time()),
                    },
                )

    async def begin_oauth_authorization(
        self,
        installation_id: int,
        user_login: str,
        provider: str,
        model: str,
        state: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO langmesh_github_oauth_authorizations "
                    "(state, provider, installation_id, user_login, model, code_verifier, redirect_uri, expires_at) "
                    "VALUES (:state, :provider, :installation_id, :user_login, :model, :code_verifier, :redirect_uri, :expires_at)"
                ),
                {
                    "state": state,
                    "provider": provider,
                    "installation_id": installation_id,
                    "user_login": user_login,
                    "model": model,
                    "code_verifier": code_verifier,
                    "redirect_uri": redirect_uri,
                    "expires_at": int(time.time()) + 600,
                },
            )

    async def oauth_authorization(
        self, provider: str, state: str
    ) -> tuple[int, str, str, str, str] | None:
        async with self.engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT installation_id, user_login, model, code_verifier, redirect_uri "
                    "FROM langmesh_github_oauth_authorizations "
                    "WHERE state = :state AND provider = :provider AND expires_at >= :now"
                ),
                {"state": state, "provider": provider, "now": int(time.time())},
            )
            row = result.first()

        return (int(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4])) if row else None

    async def consume_oauth_authorization(self, state: str) -> bool:
        async with self.engine.begin() as connection:
            deleted = await connection.execute(
                text("DELETE FROM langmesh_github_oauth_authorizations WHERE state = :state"),
                {"state": state},
            )
        return deleted.rowcount == 1

    async def begin_setup(self, installation_id: int) -> str:
        token = secrets.token_urlsafe(32)
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO langmesh_github_setup_sessions "
                    "(token, installation_id, expires_at) VALUES (:token, :installation_id, :expires_at)"
                ),
                {
                    "token": token,
                    "installation_id": installation_id,
                    "expires_at": int(time.time()) + 600,
                },
            )
        return token

    async def authenticate_setup(self, token: str, user_login: str) -> tuple[int, str] | None:
        async with self.engine.begin() as connection:
            result = await connection.execute(
                text(
                    "SELECT installation_id FROM langmesh_github_setup_sessions "
                    "WHERE token = :token AND expires_at >= :now"
                ),
                {"token": token, "now": int(time.time())},
            )
            row = result.first()
            if row is None:
                return None
            await connection.execute(
                text(
                    "UPDATE langmesh_github_setup_sessions SET user_login = :user_login WHERE token = :token"
                ),
                {"user_login": user_login, "token": token},
            )
        return int(row[0]), user_login

    async def setup(self, token: str) -> tuple[int, str] | None:
        async with self.engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT installation_id, user_login FROM langmesh_github_setup_sessions "
                    "WHERE token = :token AND expires_at >= :now AND user_login != ''"
                ),
                {"token": token, "now": int(time.time())},
            )
            row = result.first()
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
        self._checkpoints: Checkpoints = SQLAlchemyCheckpoints(store.engine)
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
        workspace.parent.mkdir(parents=True, exist_ok=True)
        header = _git_header(token)
        command = [
            "git",
            "-c",
            f"{_git_header_key()}={header}",
            "clone",
            f"https://github.com/{repository}.git",
            str(workspace),
        ]
        subprocess.run(command, check=True, capture_output=True, text=True)

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
        try:
            await asyncio.to_thread(self._checkout, mention.repository, workspace, token)
            runner = self._runner(token)
            if mention.kind == "pull" and not mention.head_ref:
                pull = await asyncio.to_thread(
                    self.github.request,
                    f"/repos/{mention.repository}/pulls/{mention.number}",
                    token,
                )
                mention = (
                    mention_from_event(
                        event,
                        event_name="pull_request",
                        repository=mention.repository,
                        pull=pull,
                        known_turn=True,
                        bot_login=f"{slug}[bot]",
                    )
                    or mention
                )
            checkout = await asyncio.to_thread(
                prepare_tree,
                mention,
                workspace,
                token=token,
                app_slug=slug,
                app_id=self.settings.app_id,
                run=runner,
            )
            followup = await asyncio.to_thread(
                thread_has_prior_bot_comment,
                event,
                repository=mention.repository,
                token=token,
                api=self.settings.github_api_url,
                bot_login=f"{slug}[bot]",
                ignore_ids=(ack,),
            )

            try:
                answer = await run_turn(
                    mention,
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
                return self._publish(mention, workspace, token, runner)

            pull_url = await asyncio.to_thread(publish_if_needed)
            await update_existing_comment(posted_reply(answer, pull_url))
            logger.info(
                "finished GitHub mention delivery id=%s attempt=%s session=%s pull_request=%s",
                delivery_id,
                attempt,
                mention.session_id,
                bool(pull_url),
            )
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
    store = Store(settings)
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
