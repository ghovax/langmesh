"""Daemon-owned filesystem storage for complete tool-output artifacts."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import BinaryIO

from langmesh.base.contracts.ports import ArtifactReference, ArtifactWriter

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")


def _valid_identifier(identifier: str) -> str:
    if not _IDENTIFIER.fullmatch(identifier):
        raise ValueError(f"Invalid artifact identifier: {identifier!r}")
    return identifier


class _FileArtifactWriter:
    def __init__(
        self,
        reference: ArtifactReference,
        handle: BinaryIO,
        incoming_path: Path,
        target_path: Path,
    ) -> None:
        self._reference = reference
        self._handle = handle
        self._incoming_path = incoming_path
        self._target_path = target_path
        self._closed = False
        self._failure: BaseException | None = None
        self._lock = asyncio.Lock()
        self._size = 0

    @property
    def reference(self) -> ArtifactReference:
        return self._reference

    async def write(self, data: bytes) -> None:
        async with self._lock:
            if self._closed:
                raise RuntimeError("artifact writer is closed")
            if self._failure is not None:
                raise RuntimeError("artifact writer failed") from self._failure
            await asyncio.to_thread(self._handle.write, data)
            self._size += len(data)

    def _commit(self) -> None:
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()
        os.replace(self._incoming_path, self._target_path)
        directory = os.open(self._target_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    async def close(self) -> ArtifactReference:
        async with self._lock:
            if self._failure is not None:
                raise RuntimeError("artifact writer failed") from self._failure
            if not self._closed:
                try:
                    await asyncio.to_thread(self._commit)
                except BaseException as error:
                    self._failure = error
                    raise
                self._closed = True
                self._reference = ArtifactReference(
                    identifier=self._reference.identifier,
                    name=self._reference.name,
                    media_type=self._reference.media_type,
                    size=self._size,
                )
            return self._reference


class FileArtifacts:
    """Store artifacts atomically beneath a caller-selected directory."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    async def create(self, name: str, media_type: str, *, identifier: str = "") -> ArtifactWriter:
        from langmesh.base.primitives.identifiers import new_id

        resolved_identifier = _valid_identifier(identifier or new_id("artifact"))
        incoming_path = self._directory / f".{resolved_identifier}.incoming"
        target_path = self._directory / resolved_identifier

        def open_incoming() -> BinaryIO:
            self._directory.mkdir(parents=True, exist_ok=True)
            if target_path.exists():
                raise FileExistsError(f"Artifact already exists: {resolved_identifier}")
            return incoming_path.open("xb")

        handle = await asyncio.to_thread(open_incoming)
        return _FileArtifactWriter(
            ArtifactReference(resolved_identifier, name, media_type),
            handle,
            incoming_path,
            target_path,
        )

    async def read(self, identifier: str) -> bytes | None:
        path = self._directory / _valid_identifier(identifier)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            return None


__all__ = ["FileArtifacts"]
