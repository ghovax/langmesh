"""Daemon-owned disposable directories for isolated library operations."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path


class FilesystemScratchSpaces:
    """Create and release isolated scratch directories beneath a caller-selected root."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _create(self, prefix: str) -> str:
        self._root.mkdir(parents=True, exist_ok=True)
        return tempfile.mkdtemp(prefix=f"{prefix}-", dir=self._root)

    async def create(self, prefix: str) -> str:
        return await asyncio.to_thread(self._create, prefix)

    async def release(self, directory: str) -> None:
        await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)


__all__ = ["FilesystemScratchSpaces"]
