"""Daemon-owned private values persisted atomically across process restarts."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
from typing import Callable


def ensure_private_value(path: Path, create: Callable[[], bytes]) -> bytes:
    """Read a nonempty private value or atomically install exactly one newly created value."""
    try:
        existing = path.read_bytes()
    except FileNotFoundError:
        existing = None
    if existing == b"":
        raise RuntimeError(f"Private value at {path} is empty.")
    if existing is not None:
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        created = create()
        if not created:
            raise ValueError("A private value must not be empty.")
        with os.fdopen(descriptor, "wb") as temporary:
            descriptor = -1
            temporary.write(created)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError as error:
            winner = path.read_bytes()
            if not winner:
                raise RuntimeError(f"Private value at {path} is empty.") from error
            return winner
        return created
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


__all__ = ["ensure_private_value"]
