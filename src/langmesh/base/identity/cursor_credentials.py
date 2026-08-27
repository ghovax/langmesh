"""Compatibility names for models-provider's Cursor authentication adapter."""

from models_provider.auth import (
    AuthenticationError,
    CredentialStore,
    CursorLoginFlow,
    CursorTokens,
    current_credential_store,
    valid_cursor_tokens,
)

CursorAuthError = AuthenticationError
PROVIDER = "cursor"
API_BASE_URL = "https://api2.cursor.sh"


def load_tokens(store: CredentialStore | None = None) -> CursorTokens | None:
    value = (store or current_credential_store()).load(PROVIDER)
    return value if isinstance(value, CursorTokens) else None


def save_tokens(tokens: CursorTokens, store: CredentialStore | None = None) -> None:
    (store or current_credential_store()).save(PROVIDER, tokens)


def clear_tokens(store: CredentialStore | None = None) -> None:
    (store or current_credential_store()).clear(PROVIDER)


def is_signed_in() -> bool:
    return load_tokens() is not None


async def valid_tokens() -> CursorTokens:
    try:
        return await valid_cursor_tokens()
    except AuthenticationError as error:
        raise CursorAuthError(str(error)) from error


def _is_display_account(value: str) -> bool:
    return bool(value.strip()) and "|" not in value


def _account_of(access_token: str) -> str:
    import base64
    import json

    try:
        _, payload, _ = access_token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, TypeError, json.JSONDecodeError):
        return ""
    for claim_name in ("email", "email_address", "name", "preferred_username", "authId"):
        value = claims.get(claim_name)
        if isinstance(value, str) and _is_display_account(value):
            return value.strip()
    return ""


__all__ = [
    "API_BASE_URL", "CursorAuthError", "CursorLoginFlow", "CursorTokens", "clear_tokens",
    "is_signed_in", "load_tokens", "save_tokens", "valid_tokens",
]
