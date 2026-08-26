"""Daemon-owned persistence for account-provider credentials."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from functools import lru_cache
import json
from typing import Any, cast

from models_provider import ChatGPTTokens, CursorTokens
from langmeshd.commons.paths import oauth_token_path
from langmeshd.commons.atomic_file import write_text


_TOKEN_TYPES = {"chatgpt": ChatGPTTokens, "cursor": CursorTokens}


class FileCredentialStore:
    """Stores each provider's tokens in its daemon data file."""

    def load(self, provider_identifier: str) -> Any:
        path = oauth_token_path(provider_identifier)
        if not path.exists():
            return None
        token_type = _TOKEN_TYPES.get(provider_identifier)
        if token_type is None:
            return None
        try:
            return token_type(**json.loads(path.read_text()))
        except (OSError, ValueError, TypeError):
            return None

    def save(self, provider_identifier: str, tokens: Any) -> None:
        if provider_identifier not in _TOKEN_TYPES or not is_dataclass(tokens):
            raise TypeError(f"Unsupported credentials for {provider_identifier!r}.")
        write_text(
            oauth_token_path(provider_identifier),
            json.dumps(asdict(cast(Any, tokens)), separators=(",", ":")),
        )

    def clear(self, provider_identifier: str) -> None:
        oauth_token_path(provider_identifier).unlink(missing_ok=True)


@lru_cache(maxsize=1)
def file_credential_store() -> FileCredentialStore:
    """Return the daemon process's shared credential store."""
    return FileCredentialStore()


__all__ = ["FileCredentialStore", "file_credential_store"]
