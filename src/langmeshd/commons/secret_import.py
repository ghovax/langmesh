"""Lift secret values out of YAML into 0600 files, then compact mail passwords.

Policy stays in configuration.yaml. Credentials are files under
``$XDG_DATA_HOME/langmesh/secrets``. Environment variables are not configuration.
"""

from __future__ import annotations

from typing import Any

from langmesh.base.secrets import (
    COMPOSIO_API_KEY,
    EMAIL_IMAP_PASSWORD,
    EMAIL_OAUTH_CLIENT_SECRET,
    EMAIL_OAUTH_REFRESH_TOKEN,
    EMAIL_SMTP_PASSWORD,
    EXA_API_KEY,
    FIRECRAWL_API_KEY,
    JINA_API_KEY,
    import_if_empty,
    provider_api_key_name,
    read_secret,
    write_secret,
)
from langmeshd.commons import configuration_file
from langmeshd.commons.configuration import compact_mail_secret
from langmeshd.commons.paths import configuration_file_path


_SECRET_FROM_YAML = (
    ("exa.api_key", EXA_API_KEY),
    ("jina.api_key", JINA_API_KEY),
    ("firecrawl.api_key", FIRECRAWL_API_KEY),
    ("composio.api_key", COMPOSIO_API_KEY),
    ("email.imap.password", EMAIL_IMAP_PASSWORD),
    ("email.smtp.password", EMAIL_SMTP_PASSWORD),
    ("email.oauth.client_secret", EMAIL_OAUTH_CLIENT_SECRET),
    ("email.oauth.refresh_token", EMAIL_OAUTH_REFRESH_TOKEN),
)


def _read_path(document: dict[str, Any], path: str) -> Any:
    node: Any = document
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set_path(document: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node: dict[str, Any] = document
    for part in parts[:-1]:
        existing = node.get(part)
        if not isinstance(existing, dict):
            existing = {}
            node[part] = existing
        node = existing
    node[parts[-1]] = value


def lift_yaml_secrets(document: dict[str, Any]) -> bool:
    """Copy YAML secret fields into empty files and clear them from the document."""
    changed = False
    for path, name in _SECRET_FROM_YAML:
        raw = _read_path(document, path)
        if not isinstance(raw, str) or not raw.strip():
            continue
        value = (
            compact_mail_secret(raw)
            if name in {EMAIL_IMAP_PASSWORD, EMAIL_SMTP_PASSWORD}
            else raw.strip()
        )
        import_if_empty(name, value)
        _set_path(document, path, "")
        changed = True
    providers = document.get("providers")
    if isinstance(providers, dict):
        for identifier, entry in list(providers.items()):
            if not isinstance(entry, dict):
                continue
            key = entry.get("api_key")
            if not isinstance(key, str) or not key.strip():
                continue
            import_if_empty(provider_api_key_name(str(identifier)), key.strip())
            entry["api_key"] = ""
            changed = True
    return changed


def import_into_files() -> None:
    """Strip YAML secrets into files and compact Gmail app-password spaces."""
    if configuration_file_path().exists():
        document = configuration_file.load() or {}
        if lift_yaml_secrets(document):
            invalid = configuration_file.rejects(document)
            if not invalid:
                configuration_file.save(document)
    imap = compact_mail_secret(read_secret(EMAIL_IMAP_PASSWORD))
    if imap and read_secret(EMAIL_IMAP_PASSWORD) != imap:
        write_secret(EMAIL_IMAP_PASSWORD, imap)
    smtp = compact_mail_secret(read_secret(EMAIL_SMTP_PASSWORD))
    if smtp and read_secret(EMAIL_SMTP_PASSWORD) != smtp:
        write_secret(EMAIL_SMTP_PASSWORD, smtp)


__all__ = ["import_into_files", "lift_yaml_secrets"]
