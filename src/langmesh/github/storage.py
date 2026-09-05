"""Durable relational state for the hosted GitHub App."""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from models_provider import OAuthTokens, ProviderAuthentication
from sqlalchemy import BigInteger, Index, String, Text, and_, exists, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, aliased, mapped_column

logger = logging.getLogger("langmesh.github.storage")


class Base(DeclarativeBase):
    """The hosted service's complete relational schema."""


class Installation(Base):
    __tablename__ = "github_installations"

    installation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_login: Mapped[str] = mapped_column(Text, default="")
    account_type: Mapped[str] = mapped_column(Text, default="unknown")
    provider: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(Text, default="")
    api_key: Mapped[str] = mapped_column(Text, default="")
    oauth_tokens: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[int] = mapped_column(BigInteger)


class SessionFence(Base):
    __tablename__ = "github_session_fences"

    session_id: Mapped[str] = mapped_column(Text, primary_key=True)
    last_sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    completed_sequence: Mapped[int] = mapped_column(BigInteger, default=0)


class ProviderUsage(Base):
    __tablename__ = "github_provider_usage"

    installation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[int] = mapped_column(BigInteger)


class Delivery(Base):
    __tablename__ = "github_deliveries"
    __table_args__ = (
        Index("github_deliveries_ready", "status", "received_at"),
        Index("github_deliveries_session_order", "session_id", "session_sequence", "status"),
    )

    delivery_id: Mapped[str] = mapped_column(Text, primary_key=True)
    event_name: Mapped[str] = mapped_column(Text)
    installation_id: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    received_at: Mapped[int] = mapped_column(BigInteger)
    claimed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    attempts: Mapped[int] = mapped_column(BigInteger, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    next_attempt_at: Mapped[int] = mapped_column(BigInteger, default=0)
    comment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    session_id: Mapped[str] = mapped_column(Text, default="")
    session_sequence: Mapped[int] = mapped_column(BigInteger, default=0)


class SetupSession(Base):
    __tablename__ = "github_setup_sessions"
    __table_args__ = (Index("github_setup_sessions_expiry", "expires_at"),)

    token: Mapped[str] = mapped_column(Text, primary_key=True)
    installation_id: Mapped[int] = mapped_column(BigInteger)
    user_login: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[int] = mapped_column(BigInteger)


class OAuthAuthorization(Base):
    __tablename__ = "github_oauth_authorizations"
    __table_args__ = (Index("github_oauth_authorizations_expiry", "expires_at"),)

    state: Mapped[str] = mapped_column(Text, primary_key=True)
    provider: Mapped[str] = mapped_column(Text)
    installation_id: Mapped[int] = mapped_column(BigInteger)
    user_login: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(Text)
    code_verifier: Mapped[str] = mapped_column(Text)
    redirect_uri: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[int] = mapped_column(BigInteger)


class ViewerLink(Base):
    __tablename__ = "github_session_viewer_links"
    __table_args__ = (Index("github_session_viewer_links_session", "session_id", unique=True),)

    token: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[str] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(BigInteger)


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


def delivery_session_id(event_name: str, payload: str) -> str:
    """Return the durable conversation key carried by a GitHub event."""
    try:
        event = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(event, Mapping):
        return ""
    repository_data = event.get("repository") or {}
    issue = event.get("issue") or {}
    pull_request = event.get("pull_request") or {}
    if not isinstance(repository_data, Mapping):
        return ""
    if not isinstance(issue, Mapping):
        issue = {}
    if not isinstance(pull_request, Mapping):
        pull_request = {}
    repository = str(repository_data.get("full_name") or "").strip()
    number = issue.get("number") or pull_request.get("number")
    if not repository or not number:
        return ""
    try:
        number = int(number)
    except (TypeError, ValueError):
        return ""
    is_pull_request = event_name in {"pull_request", "pull_request_review_comment"} or bool(
        issue.get("pull_request")
    )
    kind = "pull" if is_pull_request else "issue"
    return f"github:{repository}:{kind}:{number}"


class Store:
    """Durable GitHub service state backed by a configured SQLAlchemy database."""

    def __init__(self, database_url: str, encryption_key_path: Path) -> None:
        self._cipher = Fernet(encryption_key_path.read_bytes().strip())
        self.engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    async def _begin_sqlite_write(self, session: AsyncSession) -> None:
        """Serialize SQLite writers where row-level locks are unavailable."""
        if session.bind is not None and session.bind.dialect.name == "sqlite":
            await session.execute(text("BEGIN IMMEDIATE"))

    async def _next_sequence(self, session: AsyncSession, session_id: str) -> int:
        dialect = session.bind.dialect.name if session.bind is not None else ""
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:
            fence = await session.get(SessionFence, session_id, with_for_update=True)
            if fence is None:
                fence = SessionFence(session_id=session_id, last_sequence=1)
                session.add(fence)
                await session.flush()
                return 1
            fence.last_sequence += 1
            await session.flush()
            return fence.last_sequence
        statement = (
            insert(SessionFence)
            .values(session_id=session_id, last_sequence=1, completed_sequence=0)
            .on_conflict_do_update(
                index_elements=[SessionFence.session_id],
                set_={"last_sequence": SessionFence.last_sequence + 1},
            )
            .returning(SessionFence.last_sequence)
        )
        return int((await session.execute(statement)).scalar_one())

    async def enqueue(
        self, delivery_id: str, event_name: str, installation_id: int, payload: str
    ) -> bool:
        if not delivery_id:
            return False
        session_id = delivery_session_id(event_name, payload)
        try:
            async with self._sessions.begin() as session:
                await self._begin_sqlite_write(session)
                if await session.get(Delivery, delivery_id) is not None:
                    return False
                sequence = await self._next_sequence(session, session_id) if session_id else 0
                session.add(
                    Delivery(
                        delivery_id=delivery_id,
                        event_name=event_name,
                        installation_id=installation_id,
                        payload=payload,
                        status="queued",
                        received_at=int(time.time()),
                        session_id=session_id,
                        session_sequence=sequence,
                    )
                )
                await session.flush()
        except IntegrityError:
            return False
        return True

    async def claim(self, stale_after: int = 900) -> dict[str, Any] | None:
        now = int(time.time())
        while True:
            superseded = False
            async with self._sessions.begin() as session:
                await self._begin_sqlite_write(session)
                candidate = aliased(Delivery)
                prior = aliased(Delivery)
                earlier_active = exists(
                    select(prior.delivery_id).where(
                        prior.session_id == candidate.session_id,
                        prior.session_sequence < candidate.session_sequence,
                        prior.status.in_(("queued", "processing")),
                    )
                )
                statement = (
                    select(candidate)
                    .where(
                        or_(
                            and_(candidate.status == "queued", candidate.next_attempt_at <= now),
                            and_(
                                candidate.status == "processing",
                                candidate.claimed_at < now - stale_after,
                            ),
                        ),
                        or_(candidate.session_id == "", ~earlier_active),
                    )
                    .order_by(candidate.received_at, candidate.delivery_id)
                    .limit(1)
                )
                if session.bind is not None and session.bind.dialect.name != "sqlite":
                    statement = statement.with_for_update(skip_locked=True)
                delivery = (await session.execute(statement)).scalar_one_or_none()
                if delivery is None:
                    return None
                was_processing = delivery.status == "processing"
                recovered = was_processing or delivery.attempts > 0
                if delivery.session_id and delivery.session_sequence:
                    fence = await session.get(
                        SessionFence,
                        delivery.session_id,
                        with_for_update=(
                            session.bind is not None and session.bind.dialect.name != "sqlite"
                        ),
                    )
                    if fence is not None and delivery.session_sequence <= fence.completed_sequence:
                        completed_sequence = fence.completed_sequence
                        delivery.status = "superseded"
                        delivery.claimed_at = None
                        delivery.next_attempt_at = 0
                        delivery.last_error = "superseded by a newer completed session delivery"
                        logger.info(
                            "superseded GitHub delivery id=%s session=%s sequence=%s completed_sequence=%s",
                            delivery.delivery_id,
                            delivery.session_id,
                            delivery.session_sequence,
                            completed_sequence,
                        )
                        superseded = True
                if not superseded:
                    delivery.status = "processing"
                    delivery.claimed_at = now
                    delivery.attempts += 1
                    claimed = {
                        "delivery_id": delivery.delivery_id,
                        "event_name": delivery.event_name,
                        "installation_id": delivery.installation_id,
                        "payload": delivery.payload,
                        "status": "processing" if was_processing else "queued",
                        "attempts": delivery.attempts,
                        "session_id": delivery.session_id,
                        "session_sequence": delivery.session_sequence,
                        "recovered": recovered,
                    }
            if superseded:
                continue
            return claimed

    async def complete(self, delivery_id: str) -> None:
        async with self._sessions.begin() as session:
            await self._begin_sqlite_write(session)
            lock = session.bind is not None and session.bind.dialect.name != "sqlite"
            delivery = await session.get(Delivery, delivery_id, with_for_update=lock)
            if delivery is None or delivery.status != "processing":
                return
            delivery.status = "completed"
            if not delivery.session_id or not delivery.session_sequence:
                return
            fence = await session.get(SessionFence, delivery.session_id, with_for_update=lock)
            if fence is not None:
                fence.completed_sequence = max(fence.completed_sequence, delivery.session_sequence)

    async def schedule_retry(self, delivery_id: str, error: str, delay: int) -> None:
        async with self._sessions.begin() as session:
            await self._begin_sqlite_write(session)
            delivery = await session.get(Delivery, delivery_id)
            if delivery is None:
                return
            delivery.status = "queued"
            delivery.claimed_at = None
            delivery.next_attempt_at = int(time.time()) + max(1, delay)
            delivery.last_error = error

    async def release_processing(self, delivery_id: str, error: str) -> bool:
        """Return a still-processing delivery to the queue without reviving completed work."""
        async with self._sessions.begin() as session:
            await self._begin_sqlite_write(session)
            lock = session.bind is not None and session.bind.dialect.name != "sqlite"
            delivery = await session.get(Delivery, delivery_id, with_for_update=lock)
            if delivery is None or delivery.status != "processing":
                return False
            delivery.status = "queued"
            delivery.claimed_at = None
            delivery.next_attempt_at = int(time.time()) + 1
            delivery.last_error = error
            return True

    async def mark_failed(self, delivery_id: str, error: str) -> None:
        async with self._sessions.begin() as session:
            await self._begin_sqlite_write(session)
            delivery = await session.get(Delivery, delivery_id)
            if delivery is None:
                return
            delivery.status = "failed"
            delivery.claimed_at = None
            delivery.next_attempt_at = 0
            delivery.last_error = error

    async def comment_id_for_delivery(self, delivery_id: str) -> int | None:
        async with self._sessions() as session:
            value = await session.scalar(
                select(Delivery.comment_id).where(Delivery.delivery_id == delivery_id)
            )
        return int(value) if value is not None else None

    async def remember_comment_id(self, delivery_id: str, comment_id: int) -> None:
        async with self._sessions.begin() as session:
            delivery = await session.get(Delivery, delivery_id)
            if delivery is not None:
                delivery.comment_id = comment_id

    async def viewer_token(self, session_id: str) -> str:
        """Return the opaque durable token that grants read-only access to one session."""
        if not session_id:
            raise ValueError("session_id must not be empty")
        for _ in range(3):
            token = secrets.token_urlsafe(32)
            try:
                async with self._sessions.begin() as session:
                    await self._begin_sqlite_write(session)
                    existing = await session.scalar(
                        select(ViewerLink.token).where(ViewerLink.session_id == session_id)
                    )
                    if existing:
                        return str(existing)
                    session.add(
                        ViewerLink(token=token, session_id=session_id, created_at=int(time.time()))
                    )
                    await session.flush()
                    return token
            except IntegrityError:
                continue
        raise RuntimeError("could not create a session viewer link")

    async def session_for_viewer_token(self, token: str) -> str | None:
        """Resolve a viewer token without exposing session identifiers in the link itself."""
        if not token:
            return None
        async with self._sessions() as session:
            return await session.scalar(
                select(ViewerLink.session_id).where(ViewerLink.token == token)
            )

    async def viewer_context(self, session_id: str) -> dict[str, Any] | None:
        """Return non-secret GitHub context and lifecycle status for a session viewer."""
        async with self._sessions() as session:
            deliveries = list(
                (
                    await session.scalars(
                        select(Delivery)
                        .where(Delivery.session_id == session_id)
                        .order_by(Delivery.received_at, Delivery.delivery_id)
                    )
                ).all()
            )
            if not deliveries:
                return None
            installation = await session.get(Installation, deliveries[0].installation_id)
            provider_usage = await session.get(ProviderUsage, deliveries[0].installation_id)

        source: Mapping[str, Any] = {}
        try:
            decoded = json.loads(deliveries[0].payload)
            if isinstance(decoded, Mapping):
                source = decoded
        except json.JSONDecodeError:
            pass
        subscription_usage: dict[str, Any] | None = None
        if provider_usage is not None and provider_usage.snapshot:
            try:
                decoded_usage = json.loads(provider_usage.snapshot)
            except (TypeError, json.JSONDecodeError):
                decoded_usage = None
            if isinstance(decoded_usage, dict):
                subscription_usage = decoded_usage
        issue = source.get("issue")
        pull_request = source.get("pull_request")
        comment = source.get("comment")
        repository = source.get("repository")
        if not isinstance(issue, Mapping):
            issue = {}
        if not isinstance(pull_request, Mapping):
            pull_request = {}
        if not isinstance(comment, Mapping):
            comment = {}
        if not isinstance(repository, Mapping):
            repository = {}
        kind = "pull" if pull_request or issue.get("pull_request") else "issue"
        source_url = str(
            comment.get("html_url") or issue.get("html_url") or pull_request.get("html_url") or ""
        ).strip()
        source_messages: list[dict[str, str]] = []
        for delivery in deliveries:
            try:
                delivery_payload = json.loads(delivery.payload)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(delivery_payload, Mapping):
                continue
            delivery_source = (
                delivery_payload.get("comment")
                or delivery_payload.get("pull_request")
                or delivery_payload.get("issue")
                or {}
            )
            if not isinstance(delivery_source, Mapping):
                continue
            body = str(delivery_source.get("body") or "")
            if not body:
                continue
            delivery_user = delivery_source.get("user") or {}
            author = (
                str(delivery_user.get("login") or "") if isinstance(delivery_user, Mapping) else ""
            )
            source_messages.append({"body": body, "author": author})
        status = "completed"
        if any(delivery.status == "processing" for delivery in deliveries):
            status = "working"
        elif any(delivery.status == "queued" for delivery in deliveries):
            status = "queued"
        elif any(delivery.status == "failed" for delivery in deliveries):
            status = "failed"
        updated_at = max(delivery.claimed_at or delivery.received_at for delivery in deliveries)
        return {
            "repository": str(repository.get("full_name") or ""),
            "number": int(issue.get("number") or pull_request.get("number") or 0),
            "kind": kind,
            "title": str(issue.get("title") or pull_request.get("title") or ""),
            "source_url": source_url,
            "source_messages": source_messages,
            "provider": str(installation.provider if installation is not None else ""),
            "model": str(installation.model if installation is not None else ""),
            "subscription_usage": subscription_usage,
            "status": status,
            "updated_at": datetime.fromtimestamp(updated_at, timezone.utc).isoformat(),
        }

    async def configuration(self, installation_id: int) -> InstallationConfiguration | None:
        async with self._sessions() as session:
            installation = await session.get(Installation, installation_id)
        if installation is None or not installation.provider:
            return None
        key = (
            self._cipher.decrypt(installation.api_key.encode()).decode()
            if installation.api_key
            else ""
        )
        oauth_tokens: Mapping[str, Any] | None = None
        if installation.oauth_tokens:
            try:
                decoded = json.loads(
                    self._cipher.decrypt(installation.oauth_tokens.encode()).decode()
                )
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                raise RuntimeError("Stored OAuth credentials are invalid") from error
            if not isinstance(decoded, Mapping):
                raise RuntimeError("Stored OAuth credentials are invalid")
            oauth_tokens = dict(decoded)
        return InstallationConfiguration(
            installation.provider, installation.model, key, oauth_tokens
        )

    async def save_provider_usage(self, installation_id: int, snapshot: Mapping[str, Any]) -> None:
        async with self._sessions.begin() as session:
            values = {
                "installation_id": installation_id,
                "snapshot": json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
                "updated_at": int(time.time()),
            }
            dialect = session.bind.dialect.name if session.bind is not None else ""
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert

                await session.execute(
                    insert(ProviderUsage)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=[ProviderUsage.installation_id],
                        set_={
                            "snapshot": values["snapshot"],
                            "updated_at": values["updated_at"],
                        },
                    )
                )
                return
            if dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert

                await session.execute(
                    insert(ProviderUsage)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=[ProviderUsage.installation_id],
                        set_={
                            "snapshot": values["snapshot"],
                            "updated_at": values["updated_at"],
                        },
                    )
                )
                return
            usage = await session.get(ProviderUsage, installation_id)
            if usage is None:
                usage = ProviderUsage(**values)
                session.add(usage)
                return
            usage.snapshot = values["snapshot"]
            usage.updated_at = values["updated_at"]

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
        async with self._sessions.begin() as session:
            installation = await session.get(Installation, installation_id)
            if installation is None:
                installation = Installation(
                    installation_id=installation_id,
                    updated_at=int(time.time()),
                )
                session.add(installation)
            installation.account_login = account_login
            installation.account_type = account_type
            installation.provider = provider
            installation.model = model
            if api_key is not None:
                installation.api_key = (
                    self._cipher.encrypt(api_key.encode()).decode() if api_key else ""
                )
            if clear_oauth:
                installation.oauth_tokens = ""
            installation.updated_at = int(time.time())

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
        async with self._sessions.begin() as session:
            installation = await session.get(Installation, installation_id)
            if installation is None:
                installation = Installation(
                    installation_id=installation_id,
                    updated_at=int(time.time()),
                )
                session.add(installation)
            selected_model = model.strip() or (
                installation.model if installation.provider == provider else ""
            )
            if not selected_model:
                raise ValueError("OAuth authentication requires a model")
            installation.provider = provider
            installation.model = selected_model
            installation.api_key = ""
            installation.oauth_tokens = encrypted
            installation.updated_at = int(time.time())

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
        async with self._sessions.begin() as session:
            session.add(
                OAuthAuthorization(
                    state=state,
                    provider=provider,
                    installation_id=installation_id,
                    user_login=user_login,
                    model=model,
                    code_verifier=code_verifier,
                    redirect_uri=redirect_uri,
                    expires_at=int(time.time()) + 600,
                )
            )

    async def oauth_authorization(
        self, provider: str, state: str
    ) -> tuple[int, str, str, str, str] | None:
        async with self._sessions() as session:
            authorization = await session.scalar(
                select(OAuthAuthorization).where(
                    OAuthAuthorization.state == state,
                    OAuthAuthorization.provider == provider,
                    OAuthAuthorization.expires_at >= int(time.time()),
                )
            )
        if authorization is None:
            return None
        return (
            authorization.installation_id,
            authorization.user_login,
            authorization.model,
            authorization.code_verifier,
            authorization.redirect_uri,
        )

    async def consume_oauth_authorization(self, state: str) -> bool:
        async with self._sessions.begin() as session:
            authorization = await session.get(OAuthAuthorization, state)
            if authorization is None:
                return False
            await session.delete(authorization)
        return True

    async def begin_setup(self, installation_id: int) -> str:
        token = secrets.token_urlsafe(32)
        async with self._sessions.begin() as session:
            session.add(
                SetupSession(
                    token=token,
                    installation_id=installation_id,
                    expires_at=int(time.time()) + 600,
                )
            )
        return token

    async def authenticate_setup(self, token: str, user_login: str) -> tuple[int, str] | None:
        async with self._sessions.begin() as session:
            setup = await session.scalar(
                select(SetupSession).where(
                    SetupSession.token == token,
                    SetupSession.expires_at >= int(time.time()),
                )
            )
            if setup is None:
                return None
            setup.user_login = user_login
            installation_id = setup.installation_id
        return installation_id, user_login

    async def setup(self, token: str) -> tuple[int, str] | None:
        async with self._sessions() as session:
            setup = await session.scalar(
                select(SetupSession).where(
                    SetupSession.token == token,
                    SetupSession.expires_at >= int(time.time()),
                    SetupSession.user_login != "",
                )
            )
        return (setup.installation_id, setup.user_login) if setup is not None else None


__all__ = [
    "Base",
    "Delivery",
    "Installation",
    "InstallationConfiguration",
    "ProviderUsage",
    "OAuthAuthorization",
    "ViewerLink",
    "SessionFence",
    "SetupSession",
    "Store",
    "delivery_session_id",
]
