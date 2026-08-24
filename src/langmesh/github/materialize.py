"""Copy secret files onto the XDG secret-file layout. Stdlib only.

``.github/secrets/<name>`` is the git-tree copy of ``$XDG_DATA_HOME/langmesh/secrets/<name>``.
Empty XDG files are filled from the checkout. Existing XDG files are left alone, so a
self-hosted runner can keep keys only under XDG.

On a GitHub-hosted runner the checkout cannot hold keys (this repository is public).
The job passes the Actions-store values into this process for the write only, under
the underscore names GitHub allows, and this file writes ``github.api_key`` and
``github.app.private_key``. LangMesh does not read those underscore names, and they
are not ``LANGMESH_`` variables.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

try:
    from langmesh.github.files import workspace_secrets_directory, xdg_secrets_directory
except ImportError:
    from files import workspace_secrets_directory, xdg_secrets_directory

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# GitHub secret names cannot contain dots. One step env → one secret file.
_ACTIONS_FILES = (
    ("github_api_key", "github.api_key"),
    ("github_app_private_key", "github.app.private_key"),
)


def _write(directory: Path, name: str, value: str, *, overwrite: bool = False) -> None:
    text = value.strip()
    if not text:
        return
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    destination = directory / name
    if not overwrite and destination.is_file() and destination.read_text(encoding="utf-8").strip():
        return
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            if not text.endswith("\n"):
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _write_actions_store() -> None:
    """Hosted-runner store → secret files. No-op when those values are absent."""
    if os.environ.get("GITHUB_ACTIONS", "").lower() != "true":
        return
    checkout = workspace_secrets_directory()
    xdg = xdg_secrets_directory()
    for env_name, file_name in _ACTIONS_FILES:
        _write(checkout, file_name, os.environ.get(env_name, ""), overwrite=True)
        _write(xdg, file_name, os.environ.get(env_name, ""), overwrite=True)


def main() -> None:
    _write_actions_store()
    source = workspace_secrets_directory()
    if not source.is_dir():
        return
    destination = xdg_secrets_directory()
    try:
        names = os.listdir(source)
    except OSError:
        return
    for name in names:
        if name in {"README", "README.md"} or not _NAME.match(name):
            continue
        path = source / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        _write(destination, name, text)


if __name__ == "__main__":
    main()
