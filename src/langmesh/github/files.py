"""Where mention secret files and job files live. Stdlib only: ack and the App token run before the venv.

Runtime secrets are ``$XDG_DATA_HOME/langmesh/secrets``. The checkout copy is
``.github/secrets``, next to ``langmesh.yaml``. The job copies checkout files onto
the XDG directory; a self-hosted runner can keep the files only under XDG.

Job-local values (the acknowledgement comment id, the App slug) are files under
``.github/langmesh/``, same directory as the session artifact. That directory is
gitignored. They are not environment variables and not committed policy.
"""

from __future__ import annotations

import os
from pathlib import Path

APPLICATION = "langmesh"
SECRETS_DIRNAME = "secrets"
WORKSPACE_SECRETS = "secrets"
STATE_DIRECTORY = ".github/langmesh"
ACK_ID_NAME = "acknowledgement.id"
APP_SLUG_NAME = "app-slug"


def workspace_root() -> Path:
    return Path(os.environ.get("GITHUB_WORKSPACE") or os.getcwd()).resolve()


def workspace_secrets_directory() -> Path:
    return workspace_root() / ".github" / WORKSPACE_SECRETS


def xdg_secrets_directory() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg.startswith("/"):
        return Path(xdg) / APPLICATION / SECRETS_DIRNAME
    return Path.home() / ".local" / "share" / APPLICATION / SECRETS_DIRNAME


def secret_file(name: str) -> Path | None:
    """The first existing non-empty file of this name under XDG, then the checkout."""
    for directory in (xdg_secrets_directory(), workspace_secrets_directory()):
        path = directory / name
        try:
            if path.is_file() and path.read_text(encoding="utf-8").strip():
                return path
        except OSError:
            continue
    return None


def job_directory(workspace: Path | None = None) -> Path:
    root = Path(workspace).resolve() if workspace is not None else workspace_root()
    return root / STATE_DIRECTORY


def write_job_file(name: str, value: str, workspace: Path | None = None) -> Path:
    """Replace one job-local file. Empty value removes it."""
    directory = job_directory(workspace)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    text = value.strip()
    if not text:
        path.unlink(missing_ok=True)
        return path
    path.write_text(f"{text}\n", encoding="utf-8")
    return path


def read_job_file(name: str, workspace: Path | None = None) -> str:
    path = job_directory(workspace) / name
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
