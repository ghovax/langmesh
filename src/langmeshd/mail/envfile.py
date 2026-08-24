"""Load `mail.env` into this process so `langmesh mail` does not need a wrapping `xargs`.

systemd and the Docker entrypoint already inject that file. A checkout command does not,
unless the file is applied here. Existing non-empty environment values win, matching
LANGMESH_MAIL_* over the file. Gmail 16-character app passwords copied with spaces are
compacted so systemd EnvironmentFile can load them.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from langmeshd.commons.configuration import compact_mail_secret

logger = logging.getLogger(__name__)

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


def mail_env_problem() -> str:
    """Why LANGMESH_MAIL_ENV cannot be loaded, or empty when the path is usable or unset."""
    explicit = os.environ.get("LANGMESH_MAIL_ENV", "").strip()
    if not explicit:
        return ""
    path = Path(explicit).expanduser()
    if path.is_file():
        return ""
    return f"LANGMESH_MAIL_ENV is not a file ({explicit})."


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


def _should_replace(key: str, prepared: str, current: str | None) -> bool:
    """Whether mail.env should win over a value already in the environment.

    Existing non-empty values win, except a truncated Gmail app password: systemd
    EnvironmentFile cannot parse unquoted spaces, so it may keep only the first
    four-character group of a 16-character secret the file still has in full.
    """
    if not prepared.strip():
        return False
    if current is None or not current.strip():
        return True
    if key not in _PASSWORD_KEYS:
        return False
    have = compact_mail_secret(current)
    if have == prepared:
        return current != prepared
    token = current.strip().replace(" ", "")
    return (
        len(prepared) == 16
        and prepared.isalnum()
        and prepared.startswith(token)
        and 0 < len(token) < 16
    )


def normalize_mail_env_text(text: str) -> str:
    """Rewrite Gmail display-spaced app passwords so systemd EnvironmentFile can load them."""
    lines: list[str] = []
    for raw in text.splitlines(keepends=True):
        ending = ""
        line = raw
        if line.endswith("\n"):
            ending = "\n"
            line = line[:-1]
        if line.endswith("\r"):
            line = line[:-1]
        parsed = _parse_line(line)
        if parsed is None:
            lines.append(line + ending)
            continue
        key, value = parsed
        prepared = _prepared(key, value)
        if key in _PASSWORD_KEYS and prepared != value:
            lines.append(f"{key}={prepared}{ending}")
            continue
        lines.append(line + ending)
    return "".join(lines)


def install_mail_env(source: Path, destination: Path) -> None:
    """Copy mail.env, compacting Gmail app-password spaces, as mode 0600."""
    text = source.read_text(encoding="utf-8")
    normalized = normalize_mail_env_text(text)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    tmp.write_text(normalized, encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, destination)


def apply_mail_env() -> Path | None:
    """Fill blank LANGMESH_MAIL_* and provider keys from mail.env. Returns the file loaded."""
    path = mail_env_path()
    if path is None:
        problem = mail_env_problem()
        if problem:
            logger.debug("%s", problem)
        return None
    loaded = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        logger.warning("could not read %s: %s", path, error)
        return None
    for raw in lines:
        parsed = _parse_line(raw.rstrip("\r"))
        if parsed is None:
            continue
        key, value = parsed
        prepared = _prepared(key, value)
        if not _should_replace(key, prepared, os.environ.get(key)):
            continue
        os.environ[key] = prepared
        loaded += 1
    if loaded:
        logger.info("loaded %s from %s", loaded, path)
    else:
        logger.debug("mail.env %s", path)
    return path
