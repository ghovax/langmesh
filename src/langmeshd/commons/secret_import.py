"""Copy platform-injected secrets into 0600 files, then read those files.

Policy stays in configuration.yaml. A leftover mail.env is imported once into
YAML (address, allow-list, agent) and into secret files (passwords, provider
keys). Environment variables are not a second schema: they fill empty files.
"""

from __future__ import annotations

from typing import Any

from langmesh.base.secrets import (
    COMPOSIO_API_KEY,
    EMAIL_IMAP_PASSWORD,
    EMAIL_SMTP_PASSWORD,
    EXA_API_KEY,
    FIRECRAWL_API_KEY,
    JINA_API_KEY,
    import_environment,
    import_if_empty,
    provider_api_key_name,
    read_secret,
    write_secret,
)
from langmeshd.commons import configuration_file
from langmeshd.commons.configuration import compact_mail_secret
from langmeshd.commons.paths import configuration_file_path
from langmeshd.mail.envfile import mail_env_path, parse_mail_env


_POLICY_FROM_ENV = (
    ("LANGMESH_MAIL_ADDRESS", "email.address"),
    ("LANGMESH_MAIL_AGENT", "email.agent"),
    ("LANGMESH_MAIL_WORKING_DIRECTORY", "email.working_directory"),
    ("LANGMESH_MAIL_IMAP_HOST", "email.imap.host"),
    ("LANGMESH_MAIL_IMAP_USER", "email.imap.username"),
    ("LANGMESH_MAIL_IMAP_MAILBOX", "email.imap.mailbox"),
    ("LANGMESH_MAIL_SMTP_HOST", "email.smtp.host"),
    ("LANGMESH_MAIL_SMTP_USER", "email.smtp.username"),
)

_SECRET_FROM_YAML = (
    ("exa.api_key", EXA_API_KEY),
    ("jina.api_key", JINA_API_KEY),
    ("firecrawl.api_key", FIRECRAWL_API_KEY),
    ("composio.api_key", COMPOSIO_API_KEY),
    ("email.imap.password", EMAIL_IMAP_PASSWORD),
    ("email.smtp.password", EMAIL_SMTP_PASSWORD),
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


def _empty_policy(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return not value
    return False


def lift_yaml_secrets(document: dict[str, Any]) -> bool:
    """Copy YAML secret fields into empty files and clear them from the document."""
    changed = False
    for path, name in _SECRET_FROM_YAML:
        raw = _read_path(document, path)
        if not isinstance(raw, str) or not raw.strip():
            continue
        value = compact_mail_secret(raw) if name.startswith("email.") else raw.strip()
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


def _fill_policy_from_pairs(document: dict[str, Any], pairs: dict[str, str]) -> bool:
    changed = False
    allow = (pairs.get("LANGMESH_MAIL_ALLOW_FROM") or "").strip()
    if allow and _empty_policy(_read_path(document, "email.allow_from")):
        _set_path(
            document,
            "email.allow_from",
            [item.strip() for item in allow.split(",") if item.strip()],
        )
        changed = True
    enabled = _read_path(document, "email.enabled")
    address = (pairs.get("LANGMESH_MAIL_ADDRESS") or "").strip()
    if address and not enabled:
        _set_path(document, "email.enabled", True)
        changed = True
    for variable, path in _POLICY_FROM_ENV:
        value = (pairs.get(variable) or "").strip()
        if not value:
            continue
        if _empty_policy(_read_path(document, path)):
            _set_path(document, path, value)
            changed = True
    imap_port = (pairs.get("LANGMESH_MAIL_IMAP_PORT") or "").strip()
    if imap_port and _empty_policy(_read_path(document, "email.imap.port")):
        _set_path(document, "email.imap.port", int(imap_port))
        changed = True
    smtp_port = (pairs.get("LANGMESH_MAIL_SMTP_PORT") or "").strip()
    if smtp_port and _empty_policy(_read_path(document, "email.smtp.port")):
        _set_path(document, "email.smtp.port", int(smtp_port))
        changed = True
    return changed


def import_mail_env_file() -> bool:
    """Lift a leftover mail.env into YAML policy and secret files. Returns whether YAML changed."""
    source = mail_env_path()
    if source is None:
        return False
    pairs = parse_mail_env(source)
    if not pairs:
        return False
    import_environment(pairs)
    document = configuration_file.load() or {}
    changed = _fill_policy_from_pairs(document, pairs)
    if changed:
        invalid = configuration_file.rejects(document)
        if invalid:
            raise ValueError(f"invalid configuration: {invalid}")
        configuration_file.save(document)
    return changed


def import_into_files() -> None:
    """Fill empty secret files from env, YAML, and a leftover mail.env, then strip YAML secrets."""
    import_environment()
    if configuration_file_path().exists():
        document = configuration_file.load() or {}
        if lift_yaml_secrets(document):
            invalid = configuration_file.rejects(document)
            if not invalid:
                configuration_file.save(document)
    import_mail_env_file()
    import_environment()
    imap = compact_mail_secret(read_secret(EMAIL_IMAP_PASSWORD))
    if imap and read_secret(EMAIL_IMAP_PASSWORD) != imap:
        write_secret(EMAIL_IMAP_PASSWORD, imap)
    smtp = compact_mail_secret(read_secret(EMAIL_SMTP_PASSWORD))
    if smtp and read_secret(EMAIL_SMTP_PASSWORD) != smtp:
        write_secret(EMAIL_SMTP_PASSWORD, smtp)


__all__ = ["import_into_files", "import_mail_env_file", "lift_yaml_secrets"]
