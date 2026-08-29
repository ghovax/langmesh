"""Mailbox OAuth2: Authlib refreshes tokens; aioimaplib and aiosmtplib speak XOAUTH2.

Proton Mail is not an issuer. It has no IMAP OAuth; a paid plan uses Proton Bridge and a password.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from langmesh import PackagePromptLoader
from langmesh.base.secrets import EMAIL_OAUTH_REFRESH_TOKEN, write_secret
from langmeshd.commons.configuration import EmailConfiguration

logger = logging.getLogger("langmeshd.mail")
_CALLBACK = PackagePromptLoader(Path(__file__).resolve().parent, extension="html")

_ISSUERS: dict[str, dict[str, Any]] = {
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": ("https://mail.google.com/",),
        "extra_authorize": {"access_type": "offline", "prompt": "consent"},
    },
    "microsoft": {
        "authorize_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        "scopes": (
            "https://outlook.office.com/IMAP.AccessAsUser.All",
            "https://outlook.office.com/SMTP.Send",
            "offline_access",
        ),
        "extra_authorize": {},
    },
    "yahoo": {
        "authorize_url": "https://api.login.yahoo.com/oauth2/request_auth",
        "token_url": "https://api.login.yahoo.com/oauth2/get_token",
        "scopes": ("mail-r", "mail-w"),
        "extra_authorize": {},
    },
}

_refresh_lock = asyncio.Lock()
_cached: tuple[float, str] = (0.0, "")


@dataclass(frozen=True)
class OAuthEndpoints:
    issuer: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    extra_authorize: dict[str, str]


class MailOAuthError(RuntimeError):
    """The mailbox OAuth client cannot refresh or complete sign-in."""


def resolve_endpoints(configuration: EmailConfiguration) -> OAuthEndpoints:
    """Preset or custom token endpoints for this mailbox."""
    issuer = configuration.effective_oauth_issuer
    tenant = configuration.oauth.tenant.strip() or "common"
    preset = _ISSUERS.get(issuer)
    if preset is None and issuer and issuer != "custom":
        raise MailOAuthError(
            f"unknown email.oauth.issuer {issuer!r} "
            "(google, microsoft, yahoo, or custom with token_url)."
        )
    if preset is None:
        token_url = configuration.oauth.token_url.strip()
        authorize_url = configuration.oauth.authorize_url.strip()
        scopes = tuple(item.strip() for item in configuration.oauth.scopes if item.strip())
        if not token_url:
            raise MailOAuthError(
                "email.oauth.token_url is required when issuer is custom "
                "(or set email.oauth.issuer to google, microsoft, or yahoo)."
            )
        return OAuthEndpoints(
            issuer=issuer or "custom",
            authorize_url=authorize_url,
            token_url=token_url,
            scopes=scopes,
            extra_authorize={},
        )
    scopes = (
        tuple(item.strip() for item in configuration.oauth.scopes if item.strip())
        or preset["scopes"]
    )
    return OAuthEndpoints(
        issuer=issuer,
        authorize_url=(
            configuration.oauth.authorize_url.strip()
            or str(preset["authorize_url"]).format(tenant=tenant)
        ),
        token_url=(
            configuration.oauth.token_url.strip() or str(preset["token_url"]).format(tenant=tenant)
        ),
        scopes=scopes,
        extra_authorize=dict(preset["extra_authorize"]),
    )


def oauth_readiness_problem(configuration: EmailConfiguration) -> str:
    """Why OAuth cannot authenticate this mailbox, or empty when the refresh token is present."""
    if not configuration.uses_oauth:
        return ""
    if configuration.is_proton:
        return (
            "Proton Mail has no IMAP OAuth. Use Proton Bridge on this host "
            "with email.auth password and the Bridge password file."
        )
    if not configuration.effective_oauth_client_id:
        return "email.oauth.client_id is required when email.auth is oauth."
    try:
        resolve_endpoints(configuration)
    except MailOAuthError as error:
        return str(error)
    if not configuration.effective_oauth_refresh_token:
        return (
            "the secret file email.oauth.refresh_token is required "
            "(`langmesh mail auth` writes it)."
        )
    return ""


def _client(
    configuration: EmailConfiguration, *, redirect_uri: str = "", scopes: tuple[str, ...] = ()
) -> Any:
    from authlib.integrations.httpx_client import AsyncOAuth2Client

    secret = configuration.effective_oauth_client_secret or None
    kwargs: dict[str, Any] = {
        "client_id": configuration.effective_oauth_client_id,
        "client_secret": secret,
        "code_challenge_method": "S256",
    }
    if scopes:
        kwargs["scope"] = " ".join(scopes)
    if redirect_uri:
        kwargs["redirect_uri"] = redirect_uri
    if not secret:
        kwargs["token_endpoint_auth_method"] = "none"
    return AsyncOAuth2Client(**kwargs)


async def access_token(configuration: EmailConfiguration) -> str:
    """A current IMAP/SMTP access token, refreshing through Authlib when the cache is stale."""
    global _cached
    async with _refresh_lock:
        now = time.time()
        if _cached[1] and _cached[0] > now + 60:
            return _cached[1]
        endpoints = resolve_endpoints(configuration)
        refresh = configuration.effective_oauth_refresh_token
        if not refresh:
            raise MailOAuthError("the secret file email.oauth.refresh_token is required.")
        client = _client(configuration, scopes=endpoints.scopes)
        try:
            token = await client.refresh_token(endpoints.token_url, refresh_token=refresh)
        except Exception as error:  # noqa: BLE001 — Authlib wraps provider errors
            raise MailOAuthError(f"could not refresh the mailbox OAuth token ({error}).") from error
        finally:
            await client.aclose()
        access = str(token.get("access_token") or "").strip()
        if not access:
            raise MailOAuthError("the token endpoint returned no access_token.")
        rotated = str(token.get("refresh_token") or "").strip()
        if rotated and rotated != refresh:
            write_secret(EMAIL_OAUTH_REFRESH_TOKEN, rotated)
        expires_in = int(token.get("expires_in") or 3600)
        _cached = (now + max(expires_in, 60), access)
        return access


def _callback_page(message: str) -> str:
    """The page the browser shows after the OAuth provider redirects to this loopback."""
    return _CALLBACK.load("callback", {"message": html.escape(message)})


async def authorize(configuration: EmailConfiguration) -> str:
    """Browser sign-in; writes email.oauth.refresh_token and returns it."""
    if not configuration.effective_oauth_client_id:
        raise MailOAuthError("email.oauth.client_id is required.")
    if configuration.is_proton:
        raise MailOAuthError(
            "Proton Mail has no IMAP OAuth. Use Proton Bridge with email.auth password."
        )
    endpoints = resolve_endpoints(configuration)
    if not endpoints.authorize_url:
        raise MailOAuthError("email.oauth.authorize_url is required for this issuer.")
    redirect = configuration.effective_oauth_redirect_uri
    parsed = urllib.parse.urlparse(redirect)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/callback"
    captured: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:
            incoming = urllib.parse.urlparse(self.path)
            if incoming.path != path:
                self._page(404, "Not found")
                return
            query = urllib.parse.parse_qs(incoming.query)
            if query.get("error", [""])[0]:
                captured["error"] = query["error"][0]
                self._page(400, captured["error"])
                return
            if query.get("state", [""])[0] != state:
                captured["error"] = "State mismatch — sign-in aborted for safety."
                self._page(400, captured["error"])
                return
            if not query.get("code"):
                captured["error"] = "Authorization code missing."
                self._page(400, captured["error"])
                return
            captured["code"] = query["code"][0]
            self._page(200, "Signed in. You can close this tab and return to LangMesh.")

        def _page(self, status: int, message: str) -> None:
            body = _callback_page(message).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    client = _client(configuration, redirect_uri=redirect, scopes=endpoints.scopes)
    url, state = client.create_authorization_url(
        endpoints.authorize_url, **endpoints.extra_authorize
    )
    server = HTTPServer((host, port), CallbackHandler)
    server.timeout = 0.5
    logger.info("Open this URL to authorize the mailbox:\n%s", url)
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        logger.debug("could not open a browser", exc_info=True)
    deadline = time.monotonic() + 300
    try:
        while "code" not in captured and "error" not in captured:
            if time.monotonic() >= deadline:
                raise MailOAuthError("Mailbox OAuth sign-in timed out.")
            await asyncio.to_thread(server.handle_request)
        if "code" not in captured:
            raise MailOAuthError(captured.get("error") or "Mailbox OAuth sign-in failed.")
        token = await client.fetch_token(endpoints.token_url, code=captured["code"])
    finally:
        server.server_close()
        await client.aclose()
    refresh = str(token.get("refresh_token") or "").strip()
    if not refresh:
        raise MailOAuthError(
            "the token endpoint returned no refresh_token "
            "(Google needs a Desktop client with access_type=offline)."
        )
    write_secret(EMAIL_OAUTH_REFRESH_TOKEN, refresh)
    access = str(token.get("access_token") or "").strip()
    if access:
        global _cached
        expires_in = int(token.get("expires_in") or 3600)
        _cached = (time.time() + max(expires_in, 60), access)
    return refresh


__all__ = [
    "MailOAuthError",
    "OAuthEndpoints",
    "access_token",
    "authorize",
    "oauth_readiness_problem",
    "resolve_endpoints",
]
