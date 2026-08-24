"""Private values as one 0600 file each, never mixed with policy configuration.

The directory is ``$XDG_DATA_HOME/langmesh/secrets``.
Each file is named by the dotted path of the value it holds, for example
``providers.anthropic.api_key`` or ``email.imap.password``. The mention Action
reads ``github.api_key`` and ``github.app.private_key`` from this directory
(copied from ``.github/secrets`` in the checkout when those files are present).

Reads never create the directory. Writes create it at mode 0700 and replace the
file atomically at mode 0600. Environment variables are not configuration: a
platform that only injects vendor env (Fly leftover ``OPENCODE_API_KEY``) may
copy those values into empty files before anything here is read. ``LANGMESH_*``
is not imported.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from langmesh.base.identity.providers import PROVIDERS, provider_env_vars

APPLICATION = "langmesh"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PROVIDER_KEY = re.compile(r"^providers\.([A-Za-z0-9][A-Za-z0-9._-]*)\.api_key$")
SECRETS_DIRNAME = "secrets"
GITHUB_API_KEY = "github.api_key"
GITHUB_APP_PRIVATE_KEY = "github.app.private_key"
EMAIL_IMAP_PASSWORD = "email.imap.password"
EMAIL_SMTP_PASSWORD = "email.smtp.password"
EXA_API_KEY = "exa.api_key"
JINA_API_KEY = "jina.api_key"
FIRECRAWL_API_KEY = "firecrawl.api_key"
COMPOSIO_API_KEY = "composio.api_key"


def secrets_directory() -> Path:
    """Where secret files live. Does not create the directory."""
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg.startswith("/"):
        return Path(xdg) / APPLICATION / SECRETS_DIRNAME
    return Path.home() / ".local" / "share" / APPLICATION / SECRETS_DIRNAME


def provider_api_key_name(provider_identifier: str) -> str:
    return f"providers.{provider_identifier.strip()}.api_key"


def secret_path(name: str) -> Path:
    if not _NAME.match(name) or ".." in name or "/" in name:
        raise ValueError(f"invalid secret name: {name!r}")
    return secrets_directory() / name


def read_secret(name: str) -> str:
    """The file's text, stripped, or empty when it is missing."""
    path = secret_path(name)
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_secret(name: str, value: str) -> None:
    """Replace one secret file atomically at mode 0600, or remove it when the value is empty."""
    text = value.strip()
    destination = secret_path(name)
    if not text:
        destination.unlink(missing_ok=True)
        return
    directory = secrets_directory()
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    destination = secret_path(name)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def import_if_empty(name: str, value: str) -> bool:
    """Write ``value`` only when the file is missing or empty. Returns whether it wrote."""
    prepared = value.strip()
    if not prepared:
        return False
    if read_secret(name):
        return False
    write_secret(name, prepared)
    return True


def provider_keys_from_files() -> dict[str, str]:
    """Non-empty ``providers.<id>.api_key`` files, keyed by provider identifier."""
    directory = secrets_directory()
    if not directory.is_dir():
        return {}
    keys: dict[str, str] = {}
    try:
        names = os.listdir(directory)
    except OSError:
        return {}
    for name in names:
        matched = _PROVIDER_KEY.match(name)
        if matched is None:
            continue
        value = read_secret(name)
        if value:
            keys[matched.group(1)] = value
    return keys


def import_environment(environ: dict[str, str] | None = None) -> int:
    """Copy conventional environment values into empty secret files. Returns how many wrote."""
    env = os.environ if environ is None else environ
    written = 0
    mapping = (
        ("EXA_API_KEY", EXA_API_KEY),
        ("JINA_API_KEY", JINA_API_KEY),
        ("FIRECRAWL_API_KEY", FIRECRAWL_API_KEY),
        ("COMPOSIO_API_KEY", COMPOSIO_API_KEY),
    )
    for variable, name in mapping:
        if import_if_empty(name, env.get(variable) or ""):
            written += 1
    seen: set[str] = set()
    for identifier, definition in PROVIDERS.items():
        if definition.native:
            continue
        names = provider_env_vars(identifier)
        value = ""
        for variable in names:
            value = (env.get(variable) or "").strip()
            if value:
                break
        if not value:
            continue
        key_name = provider_api_key_name(definition.credential_identifier or identifier)
        if key_name in seen:
            continue
        seen.add(key_name)
        if import_if_empty(key_name, value):
            written += 1
    return written


__all__ = [
    "COMPOSIO_API_KEY",
    "EMAIL_IMAP_PASSWORD",
    "EMAIL_SMTP_PASSWORD",
    "EXA_API_KEY",
    "FIRECRAWL_API_KEY",
    "GITHUB_API_KEY",
    "GITHUB_APP_PRIVATE_KEY",
    "JINA_API_KEY",
    "import_environment",
    "import_if_empty",
    "provider_api_key_name",
    "provider_keys_from_files",
    "read_secret",
    "secret_path",
    "secrets_directory",
    "write_secret",
]
