"""Load `mail.env` into this process so `langmesh mail` does not need a wrapping `xargs`.

systemd and the Docker entrypoint already inject that file. A checkout command does not,
unless the file is applied here. Existing non-empty environment values win, matching
LANGMESH_MAIL_* over the file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


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
        if not value.strip():
            continue
        current = os.environ.get(key)
        if current is not None and current.strip():
            continue
        os.environ[key] = value
        loaded += 1
    if loaded:
        logger.info("loaded %s from %s", loaded, path)
    else:
        logger.debug("mail.env %s", path)
    return path
