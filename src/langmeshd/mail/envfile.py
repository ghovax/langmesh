"""Parse a leftover mail.env so it can be imported into YAML policy and secret files.

The live configuration is configuration.yaml plus ``$XDG_DATA_HOME/langmesh/secrets``.
``mail.env`` is not a second schema; install and start copy it once when it is present.
"""

from __future__ import annotations

import os
from pathlib import Path

from langmeshd.commons.configuration import compact_mail_secret

_PASSWORD_KEYS = frozenset(
    {
        "LANGMESH_MAIL_PASSWORD",
        "LANGMESH_MAIL_IMAP_PASSWORD",
        "LANGMESH_MAIL_SMTP_PASSWORD",
    }
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def mail_env_path() -> Path | None:
    """The mail.env this command should load, or None when none is present."""
    explicit = os.environ.get("LANGMESH_MAIL_ENV", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    for candidate in (
        Path.cwd() / "mail.env",
        Path("/run/secrets/mail.env"),
        Path("/srv/langmesh/mail.env"),
        _repo_root() / "mail.env",
        _repo_root() / "packaging" / "mail" / "mail.env",
    ):
        if candidate.is_file():
            return candidate
    return None


def parse_mail_env(path: Path) -> dict[str, str]:
    """KEY=value pairs from a leftover mail.env, compacted for Gmail app passwords."""
    pairs: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for raw in lines:
        parsed = _parse_line(raw.rstrip("\r"))
        if parsed is None:
            continue
        key, value = parsed
        prepared = _prepared(key, value)
        if prepared.strip():
            pairs[key] = prepared
    return pairs


def _parse_line(line: str) -> tuple[str, str] | None:
    text = line.strip().lstrip("\ufeff")
    if not text or text.startswith("#"):
        return None
    if text.startswith("export "):
        text = text[7:].lstrip()
    if "=" not in text:
        return None
    key, value = text.split("=", 1)
    key = key.strip()
    if not key or not key.isidentifier() or not key.isascii():
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return key, value


def _prepared(key: str, value: str) -> str:
    return compact_mail_secret(value) if key in _PASSWORD_KEYS else value


