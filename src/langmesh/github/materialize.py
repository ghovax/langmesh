"""Copy checkout secret files onto the XDG secret-file layout. Stdlib only.

``.github/secrets/<name>`` is the git-tree copy of ``$XDG_DATA_HOME/langmesh/secrets/<name>``.
Empty XDG files are filled from the checkout. Existing XDG files are left alone, so a
self-hosted runner can keep keys only under XDG. Environment variables are not read.
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


def _write(directory: Path, name: str, value: str) -> None:
    text = value.strip()
    if not text:
        return
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    destination = directory / name
    if destination.is_file() and destination.read_text(encoding="utf-8").strip():
        return
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
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
