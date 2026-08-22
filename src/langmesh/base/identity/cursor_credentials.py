"""Cursor-subscription authentication for the `cursor` model provider."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
import urllib.parse
import uuid as uuid_module
from dataclasses import dataclass
from typing import Optional

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_delay,
    wait_exponential,
)

from langmesh.base.contracts.ports import CredentialStore
from langmesh.base.identity.credential_store import credential_store
from langmesh.base.primitives.limits import current_limits


# Cursor's own login surface and API host, which are the addresses its own client uses.
LOGIN_URL = "https://cursor.com/loginDeepControl"
API_BASE_URL = "https://api2.cursor.sh"
POLL_URL = f"{API_BASE_URL}/auth/poll"
# The endpoint Cursor's client posts a refresh token to in order to mint a fresh access token.
REFRESH_URL = f"{API_BASE_URL}/auth/exchange_user_api_key"

PROVIDER = "cursor"

# Serializes token refreshes, so concurrent turns noticing an expiry produce one refresh rather than a stampede.
_refresh_lock = asyncio.Lock()


class CursorAuthError(RuntimeError):
    """Raised when a Cursor-subscription call cannot be authenticated."""


class _SignInPending(Exception):
    """The browser has not finished yet, which is the poll's normal answer and what the retry retries on."""


@dataclass
class CursorTokens:
    """The persisted result of a successful sign-in, with `account` there only so the interface can name it."""

    access_token: str
    refresh_token: str
    account: str
    expires_at: float

    def is_expired(self, leeway_seconds: float | None = None) -> bool:
        if leeway_seconds is None:
            leeway_seconds = current_limits().credential_refresh_leeway
        return time.time() >= (self.expires_at - leeway_seconds)


def load_tokens(store: CredentialStore | None = None) -> Optional[CursorTokens]:
    """Load tokens from the caller-owned store, or return ``None`` when signed out."""
    tokens = (store or credential_store()).load(PROVIDER)
    return tokens if isinstance(tokens, CursorTokens) else None


def save_tokens(tokens: CursorTokens, store: CredentialStore | None = None) -> None:
    """Place tokens in the caller-owned credential store."""
    (store or credential_store()).save(PROVIDER, tokens)


def clear_tokens(store: CredentialStore | None = None) -> None:
    (store or credential_store()).clear(PROVIDER)


def is_signed_in() -> bool:
    return load_tokens() is not None


# The verifier is random bytes and the challenge its digest, which is what the login page expects.


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _generate_verifier() -> str:
    return _b64url(secrets.token_bytes(32))


def _challenge_for(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode()).digest())


def _decode_jwt_claims(token: str) -> dict:
    """Decode a JWT payload without verifying it, reading only the expiry and subject for display."""
    try:
        _header, payload, _signature = token.split(".")
        claims = json.loads(_b64url_decode(payload))
        return claims if isinstance(claims, dict) else {}
    except (ValueError, TypeError):
        return {}


def _looks_like_jwt(value: object) -> bool:
    """Whether a value is plausibly a JWT, since Cursor sometimes returns an opaque key in that field."""
    if not isinstance(value, str):
        return False
    segments = value.split(".")
    return len(segments) == 3 and all(segments)


def _expiry_of(access_token: str) -> float:
    """The access token's own expiry, or a short guess when it is not a readable JWT."""
    exp = _decode_jwt_claims(access_token).get("exp")
    return float(exp) if isinstance(exp, (int, float)) else time.time() + 3600.0


def _is_display_account(value: str) -> bool:
    """Whether a claim is fit to show: an email or a name, never an Auth0 `provider|id` subject."""
    text = value.strip()
    if not text or "|" in text:
        return False
    return True


def _account_of(access_token: str) -> str:
    claims = _decode_jwt_claims(access_token)
    for claim in ("email", "email_address", "name", "preferred_username", "authId"):
        value = claims.get(claim)
        if isinstance(value, str) and _is_display_account(value):
            return value.strip()
    return ""


def _tokens_from_payload(payload: dict, previous: Optional[CursorTokens] = None) -> CursorTokens:
    """Build a token set from a poll or refresh response, carrying the previous refresh token over when needed."""
    access_token = payload.get("accessToken") or ""
    if not access_token:
        raise CursorAuthError("Cursor returned no access token.")
    returned_refresh = payload.get("refreshToken")
    refresh_token = (
        str(returned_refresh)
        if _looks_like_jwt(returned_refresh)
        else (previous.refresh_token if previous else str(returned_refresh or ""))
    )
    return CursorTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        account=_account_of(access_token) or (previous.account if previous else ""),
        expires_at=_expiry_of(access_token),
    )


async def _refresh(tokens: CursorTokens) -> CursorTokens:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            REFRESH_URL,
            headers={
                "Authorization": f"Bearer {tokens.refresh_token}",
                "Content-Type": "application/json",
            },
            content="{}",
        )
        response.raise_for_status()
        return _tokens_from_payload(response.json(), previous=tokens)


async def valid_tokens() -> CursorTokens:
    """A live, non-expired token set, refreshing and re-persisting first when the access token is near expiry."""
    tokens = await asyncio.to_thread(load_tokens)
    if tokens is None:
        raise CursorAuthError(
            "Not signed in to Cursor. Sign in from Settings to use this provider."
        )
    if not tokens.is_expired():
        return tokens
    if not tokens.refresh_token:
        raise CursorAuthError("Cursor session expired and cannot be refreshed. Sign in again.")
    async with _refresh_lock:
        # Re-load inside the lock: another turn may have refreshed while we waited.
        current = await asyncio.to_thread(load_tokens) or tokens
        if not current.is_expired():
            return current
        try:
            refreshed = await _refresh(current)
        except httpx.HTTPError as error:
            raise CursorAuthError(f"Could not refresh the Cursor session: {error}") from error
        await asyncio.to_thread(save_tokens, refreshed)
        return refreshed


class CursorLoginFlow:
    """One browser sign-in, polled rather than redirected because Cursor's flow has no callback."""

    def __init__(self, store: CredentialStore | None = None) -> None:
        self._store = store or credential_store()
        self._verifier = _generate_verifier()
        self._uuid = str(uuid_module.uuid4())
        self._cancelled = False

    @property
    def authorize_url(self) -> str:
        parameters = {
            "challenge": _challenge_for(self._verifier),
            "uuid": self._uuid,
            "mode": "login",
            # Names the shape of the client being handed a token, which for this flow is the polling one.
            "redirectTarget": "cli",
        }
        return f"{LOGIN_URL}?{urllib.parse.urlencode(parameters)}"

    async def start(self) -> None:
        """Cursor's sign-in is polled, so there is no loopback callback server to bind."""
        return

    async def _ask(self, client: httpx.AsyncClient) -> CursorTokens:
        """One ask of whether the browser has finished, distinguishing pending from refused from unreachable."""
        if self._cancelled:
            raise CursorAuthError("Cursor sign-in was cancelled.")
        response = await client.get(
            POLL_URL, params={"uuid": self._uuid, "verifier": self._verifier}
        )
        if response.status_code == 404:
            raise _SignInPending
        if not response.is_success:
            raise CursorAuthError(f"Cursor refused the sign-in poll (HTTP {response.status_code}).")
        tokens = _tokens_from_payload(response.json())
        await asyncio.to_thread(save_tokens, tokens, self._store)
        return tokens

    async def wait(self) -> CursorTokens:
        """Wait for the browser sign-in to complete, then persist and return the tokens."""
        limits = current_limits()
        tokens: Optional[CursorTokens] = None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                async for attempt in AsyncRetrying(
                    retry=retry_if_exception_type((_SignInPending, httpx.HTTPError)),
                    wait=wait_exponential(
                        multiplier=limits.oauth_poll_interval,
                        # `max` is tenacity's parameter name for the ceiling, not ours.
                        max=limits.oauth_poll_ceiling,
                    ),
                    stop=stop_after_delay(limits.oauth_poll_give_up),
                ):
                    with attempt:
                        tokens = await self._ask(client)
        except RetryError as error:
            raise CursorAuthError("Cursor sign-in timed out. Try again.") from error
        assert tokens is not None, "the retry either produces tokens or raises"
        return tokens

    async def close(self) -> None:
        self._cancelled = True
