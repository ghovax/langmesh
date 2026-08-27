"""Daemon-owned persistence for account-provider credentials."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from functools import lru_cache
import json
from typing import Any, cast

from models_provider import ApiKeyCredential, ChatGPTTokens, CursorTokens
from langmesh.base.secrets import provider_api_key_name, read_secret, write_secret
from langmeshd.commons.paths import oauth_token_path
from langmeshd.commons.atomic_file import write_text


_TOKEN_TYPES = {"chatgpt": ChatGPTTokens, "cursor": CursorTokens}


class FileCredentialStore:
    """Stores each provider's tokens in its daemon data file."""

    def load(self, provider_identifier: str) -> Any:
        token_type = _TOKEN_TYPES.get(provider_identifier)
        if token_type is not None:
            path = oauth_token_path(provider_identifier)
            if path.exists():
                try:
                    return token_type(**json.loads(path.read_text()))
                except (OSError, ValueError, TypeError):
                    return None
        value = read_secret(provider_api_key_name(provider_identifier))
        return ApiKeyCredential(value) if value else None

    def save(self, provider_identifier: str, tokens: Any) -> None:
        if isinstance(tokens, ApiKeyCredential):
            if provider_identifier in _TOKEN_TYPES:
                oauth_token_path(provider_identifier).unlink(missing_ok=True)
            write_secret(provider_api_key_name(provider_identifier), tokens.api_key)
            return
        if provider_identifier not in _TOKEN_TYPES or not is_dataclass(tokens):
            raise TypeError(f"Unsupported credentials for {provider_identifier!r}.")
        write_secret(provider_api_key_name(provider_identifier), "")
        write_text(
            oauth_token_path(provider_identifier),
            json.dumps(asdict(cast(Any, tokens)), separators=(",", ":")),
        )

    def clear(self, provider_identifier: str) -> None:
        if provider_identifier in _TOKEN_TYPES:
            oauth_token_path(provider_identifier).unlink(missing_ok=True)
        write_secret(provider_api_key_name(provider_identifier), "")


@lru_cache(maxsize=1)
def file_credential_store() -> FileCredentialStore:
    """Return the daemon process's shared credential store."""
    return FileCredentialStore()


__all__ = ["FileCredentialStore", "file_credential_store"]
