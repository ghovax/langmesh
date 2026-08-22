"""Crash-safe publication of daemon-owned text files."""

from __future__ import annotations

import os
from pathlib import Path
import uuid


def write_bytes(path: str | Path, content: bytes, *, mode: int = 0o600) -> None:
    """Atomically replace a file after syncing its contents."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def write_text(path: str | Path, text: str, *, mode: int = 0o600) -> None:
    """Atomically replace a UTF-8 text file after syncing its contents."""
    write_bytes(path, text.encode(), mode=mode)


__all__ = ["write_bytes", "write_text"]
