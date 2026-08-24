"""Write GitHub Actions secrets onto the secret-file layout. Stdlib only.

The mention job reads files, not environment variables. This step is the importer:
it copies ``LANGMESH_API_KEY`` and ``LANGMESH_APP_PRIVATE_KEY`` into empty files
under ``$XDG_DATA_HOME/langmesh/secrets``.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path


def _directory() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg.startswith("/"):
        return Path(xdg) / "langmesh" / "secrets"
    return Path.home() / ".local" / "share" / "langmesh" / "secrets"


def _write(name: str, value: str) -> None:
    text = value.strip()
    if not text:
        return
    directory = _directory()
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
    _write("github.api_key", os.environ.get("LANGMESH_API_KEY") or "")
    _write("github.app.private_key", os.environ.get("LANGMESH_APP_PRIVATE_KEY") or "")


if __name__ == "__main__":
    main()
