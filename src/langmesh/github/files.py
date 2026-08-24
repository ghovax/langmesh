"""Where mention secret files live. Stdlib only: ack and the App token run before the venv.

Runtime secrets are ``$XDG_DATA_HOME/langmesh/secrets``. The checkout copy is
``.github/secrets``, next to ``langmesh.yaml``. The job copies checkout files onto
the XDG directory; a self-hosted runner can keep the files only under XDG.
"""

from __future__ import annotations

import os
from pathlib import Path

APPLICATION = "langmesh"
SECRETS_DIRNAME = "secrets"
WORKSPACE_SECRETS = "secrets"


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
