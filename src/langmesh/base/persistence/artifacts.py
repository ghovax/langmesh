"""Filesystem-backed storage for complete tool outputs."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO

from langmesh.base.contracts.ports import ArtifactReference, ArtifactWriter


class _DirectoryArtifactWriter:
    def __init__(
        self,
        path: Path,
        stream: BinaryIO,
        reference: ArtifactReference,
    ) -> None:
        self._path = path
        self._stream = stream
        self._reference = reference
        self._closed = False

    @property
    def reference(self) -> ArtifactReference:
        return self._reference

    async def write(self, data: bytes) -> None:
        if self._closed:
            raise RuntimeError("artifact writer is closed")
        self._stream.write(data)

    async def close(self) -> ArtifactReference:
        if not self._closed:
            self._closed = True
            self._stream.close()
            self._reference = replace(self._reference, size=self._path.stat().st_size)
        return self._reference


class DirectoryArtifacts:
    """Complete tool outputs streamed into a caller-owned directory."""

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory).expanduser().resolve()
        self._directory.mkdir(parents=True, exist_ok=True)
        self._active: set[str] = set()

    @staticmethod
    def _file_name(identifier: str) -> str:
        return hashlib.sha256(identifier.encode()).hexdigest()

    def _path(self, identifier: str) -> Path:
        return self._directory / self._file_name(identifier)

    async def create(
        self,
        name: str,
        media_type: str,
        *,
        identifier: str = "",
    ) -> ArtifactWriter:
        reference = ArtifactReference(
            identifier=identifier or f"artifact-{uuid.uuid4()}",
            name=name,
            media_type=media_type,
        )
        path = self._path(reference.identifier)
        if reference.identifier in self._active or path.exists():
            raise FileExistsError(f"Artifact already exists: {reference.identifier}")
        self._active.add(reference.identifier)
        try:
            stream = path.open("xb")
        except BaseException:
            self._active.discard(reference.identifier)
            raise
        return _DirectoryArtifactWriter(path, stream, reference)

    async def read(self, identifier: str) -> bytes | None:
        path = self._path(identifier)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
