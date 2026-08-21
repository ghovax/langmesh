"""Daemon-owned disposable directories for isolated library operations."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path


class FilesystemScratchSpaces:
    """Create and release isolated scratch directories beneath a caller-selected root."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._active: set[str] = set()

    def _create(self, prefix: str) -> str:
        self._root.mkdir(parents=True, exist_ok=True)
        return tempfile.mkdtemp(prefix=f"{prefix}-", dir=self._root)

    async def create(self, prefix: str) -> str:
        directory = await asyncio.to_thread(self._create, prefix)
        self._active.add(directory)
        return directory

    async def release(self, directory: str) -> None:
        if directory not in self._active:
            raise ValueError("scratch directory was not created by this adapter")
        self._active.remove(directory)
        try:
            await asyncio.to_thread(shutil.rmtree, directory)
        except FileNotFoundError:
            pass


__all__ = ["FilesystemScratchSpaces"]
