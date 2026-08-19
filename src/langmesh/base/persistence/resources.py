"""fsspec-backed workspace resources and their explicit POSIX materialization boundary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, MutableMapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import tempfile
from typing import Any, Protocol, runtime_checkable
import uuid

import fsspec
from fsspec import AbstractFileSystem
from fsspec.implementations.local import LocalFileSystem
from langmesh.base.persistence.observation_store import NativeFileSubscription


def resource_path(value: str | PurePosixPath) -> str:
    """Normalize a logical resource key and reject absolute paths and traversal."""
    raw = str(value).replace("\\", "/")
    candidate = PurePosixPath(raw)
    if (
        raw in {"", "."}
        or candidate.is_absolute()
        or candidate.as_posix() != raw
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ValueError(f"resource paths must be relative and normalized, got {value!r}")
    return candidate.as_posix()


def _join(root: str, relative: str = "") -> str:
    normalized_root = root.rstrip("/")
    normalized_relative = resource_path(relative) if relative else ""
    return "/".join(part for part in (normalized_root, normalized_relative) if part)


@dataclass(frozen=True)
class ResourceChange:
    """One committed logical change, independent of a watcher's native vocabulary."""

    path: str
    kind: str  # created | modified | deleted


@runtime_checkable
class ResourceSubscription(Protocol):
    """An installed event subscription. Construction is the readiness boundary."""

    async def next(self) -> list[ResourceChange]: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class ResourceChangeSource(Protocol):
    """Push notifications for a filesystem that can provide them; polling is never implied."""

    def subscribe(self, prefix: str = "") -> ResourceSubscription: ...


class ResourceWatchUnsupported(RuntimeError):
    """The selected fsspec backend has no event source configured."""


@runtime_checkable
class WorkspaceResourcesLike(Protocol):
    """The library-facing workspace port; fsspec adapters are the standard implementation."""

    @property
    def local_path(self) -> Path | None:
        """The stable POSIX root, or ``None`` when a lease must materialize one."""
        ...

    async def read(self, path: str) -> bytes | None: ...

    async def list(self, prefix: str = "") -> list[str]: ...

    async def write(self, path: str, data: bytes) -> None: ...

    async def delete(self, path: str) -> None: ...

    def subscribe(self, prefix: str = "") -> ResourceSubscription: ...

    def watch(self, prefix: str = "") -> AsyncIterator[ResourceChange]: ...

    def materialize(self) -> AbstractAsyncContextManager["MaterializedResources"]: ...


class LocalResourceChanges:
    """Native watchdog notifications translated to logical fsspec keys."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().absolute()

    def subscribe(self, prefix: str = "") -> ResourceSubscription:
        root = self.root
        normalized = resource_path(prefix) if prefix else ""
        if not normalized:
            raise ResourceWatchUnsupported(
                "the native local adapter requires a file prefix; supply a directory event adapter for a tree"
            )
        native = NativeFileSubscription(root / normalized)

        class _LocalSubscription:
            async def next(self) -> list[ResourceChange]:
                nonlocal native
                changes = await native.next()
                if native.requires_rebase(changes):
                    replacement = NativeFileSubscription(root / normalized)
                    await asyncio.to_thread(native.close)
                    native = replacement
                translated: list[ResourceChange] = []
                for raw_kind, raw_path in changes:
                    path = Path(raw_path)
                    try:
                        relative = path.relative_to(root).as_posix()
                    except ValueError:
                        continue
                    translated.append(
                        ResourceChange(
                            relative, {1: "created", 2: "modified", 3: "deleted"}[raw_kind]
                        )
                    )
                return translated

            async def aclose(self) -> None:
                await asyncio.to_thread(native.close)

        return _LocalSubscription()


class InProcessResourceChanges:
    """Commit notifications for resources mutated through this process."""

    def __init__(self) -> None:
        self._subscribers: set[tuple[str, asyncio.Queue[list[ResourceChange]]]] = set()

    def subscribe(self, prefix: str = "") -> ResourceSubscription:
        normalized = resource_path(prefix) if prefix else ""
        queue: asyncio.Queue[list[ResourceChange]] = asyncio.Queue()
        entry = (normalized, queue)
        self._subscribers.add(entry)
        owner = self

        class _InProcessSubscription:
            async def next(self) -> list[ResourceChange]:
                return await queue.get()

            async def aclose(self) -> None:
                owner._subscribers.discard(entry)

        return _InProcessSubscription()

    def publish(self, changes: list[ResourceChange]) -> None:
        for prefix, queue in tuple(self._subscribers):
            matching = [
                change
                for change in changes
                if not prefix or change.path == prefix or change.path.startswith(f"{prefix}/")
            ]
            if matching:
                queue.put_nowait(matching)


class MaterializedResources:
    """A stable local view whose explicit sync publishes path-native changes."""

    def __init__(self, path: Path, synchronize: Any = None, refresh: Any = None) -> None:
        self.path = path
        self._synchronize = synchronize
        self._refresh = refresh

    async def sync(self) -> None:
        if self._synchronize is not None:
            await self._synchronize()

    async def refresh(self) -> None:
        """Replace the view with its source's current state at an explicit safe boundary."""
        if self._refresh is not None:
            await self._refresh()


class _LocalMaterialization(AbstractAsyncContextManager[MaterializedResources]):
    def __init__(self, path: Path) -> None:
        self._view = MaterializedResources(path)

    async def __aenter__(self) -> MaterializedResources:
        return self._view

    async def __aexit__(self, *_exception: object) -> None:
        return None


class _FsspecMaterialization(AbstractAsyncContextManager[MaterializedResources]):
    def __init__(self, resources: "WorkspaceResources") -> None:
        self._resources = resources
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._view: MaterializedResources | None = None
        self._baseline: dict[str, bytes] = {}

    async def __aenter__(self) -> MaterializedResources:
        self._temporary = tempfile.TemporaryDirectory(prefix="langmesh-resources-")
        local_root = Path(self._temporary.name)
        self._baseline = {
            relative: await self._resources.read(relative) or b""
            for relative in await self._resources.list()
        }
        for relative, data in sorted(self._baseline.items()):
            destination = local_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        self._view = MaterializedResources(
            local_root,
            lambda: self._publish(local_root),
            lambda: self._refresh(local_root),
        )
        return self._view

    async def _refresh(self, local_root: Path) -> None:
        remote = {
            relative: await self._resources.read(relative) or b""
            for relative in await self._resources.list()
        }
        for existing in [path for path in local_root.rglob("*") if path.is_file()]:
            relative = existing.relative_to(local_root).as_posix()
            if relative not in remote:
                existing.unlink()
        for relative, data in remote.items():
            destination = local_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        self._baseline = remote

    async def _publish(self, local_root: Path) -> None:
        current = {
            path.relative_to(local_root).as_posix(): path.read_bytes()
            for path in local_root.rglob("*")
            if path.is_file()
        }
        removed = set(self._baseline) - set(current)
        changed = {path: data for path, data in current.items() if self._baseline.get(path) != data}
        if removed or changed:
            # fsspec owns protocol-specific publication and transaction semantics. Backends whose files implement commit/discard publish every changed object at commit.
            def publish() -> None:
                with self._resources.filesystem.transaction:
                    for relative in sorted(removed):
                        self._resources.filesystem.rm(self._resources.remote_path(relative))
                    for relative, data in sorted(changed.items()):
                        target = self._resources.remote_path(relative)
                        parent = target.rsplit("/", 1)[0] if "/" in target else ""
                        if parent:
                            self._resources.filesystem.makedirs(parent, exist_ok=True)
                        self._resources.filesystem.pipe_file(target, data)

            await asyncio.to_thread(publish)
        notifications = [ResourceChange(path, "deleted") for path in sorted(removed)] + [
            ResourceChange(path, "created" if path not in self._baseline else "modified")
            for path in sorted(changed)
        ]
        self._baseline = current
        self._resources._publish(notifications)

    async def __aexit__(self, *_exception: object) -> None:
        if self._view is not None:
            await self._view.sync()
        if self._temporary is not None:
            self._temporary.cleanup()


class WorkspaceResources:
    """A rooted fsspec filesystem plus optional event notifications for that backend."""

    def __init__(
        self,
        filesystem: AbstractFileSystem,
        root: str = "",
        *,
        changes: ResourceChangeSource | None = None,
    ) -> None:
        if not isinstance(filesystem, AbstractFileSystem):
            raise TypeError("filesystem must implement fsspec.AbstractFileSystem")
        if isinstance(filesystem, LocalFileSystem) and not Path(root).is_absolute():
            raise ValueError("a local fsspec resource root must be an absolute path")
        self.filesystem = filesystem
        self.root = root.rstrip("/")
        self._changes = changes

    @classmethod
    def local(cls, root: str | Path) -> "WorkspaceResources":
        path = Path(root).expanduser().absolute()
        return cls(LocalFileSystem(auto_mkdir=True), str(path), changes=LocalResourceChanges(path))

    @classmethod
    def memory(cls, files: Mapping[str, bytes | str] | None = None) -> "WorkspaceResources":
        # MemoryFileSystem instances share global state, so every workspace receives an opaque root.
        resources = cls(
            fsspec.filesystem("memory"),
            f"langmesh/{uuid.uuid4().hex}",
            changes=InProcessResourceChanges(),
        )
        if files:
            with resources.filesystem.transaction:
                for path, value in files.items():
                    with resources.filesystem.open(resources.remote_path(path), "wb") as handle:
                        handle.write(value.encode() if isinstance(value, str) else bytes(value))  # type: ignore[arg-type]
        return resources

    def remote_path(self, relative: str = "") -> str:
        return _join(self.root, relative)

    @property
    def local_path(self) -> Path | None:
        return Path(self.root) if isinstance(self.filesystem, LocalFileSystem) else None

    async def read(self, path: str) -> bytes | None:
        target = self.remote_path(path)

        def read() -> bytes | None:
            if not self.filesystem.isfile(target):
                return None
            raw = self.filesystem.cat_file(target)
            return raw if isinstance(raw, bytes) else raw.encode() if isinstance(raw, str) else None

        return await asyncio.to_thread(read)

    async def list(self, prefix: str = "") -> list[str]:
        target = self.remote_path(prefix)

        def find() -> list[str]:
            if not self.filesystem.exists(target):
                return []
            paths = [target] if self.filesystem.isfile(target) else self.filesystem.find(target)
            root = self.root.rstrip("/")

            def relative(path: str) -> str:
                value = str(path)
                for candidate, candidate_root in (
                    (value, root),
                    (value.lstrip("/"), root.lstrip("/")),
                ):
                    prefix = f"{candidate_root}/" if candidate_root else ""
                    if prefix and candidate.startswith(prefix):
                        return resource_path(candidate[len(prefix) :])
                    if not prefix:
                        return resource_path(candidate.lstrip("/"))
                raise ValueError(f"filesystem returned {value!r} outside resource root {root!r}")

            return sorted(relative(path) for path in paths if self.filesystem.isfile(path))

        return await asyncio.to_thread(find)

    async def write(self, path: str, data: bytes) -> None:
        target = self.remote_path(path)
        normalized = resource_path(path)
        existed = await asyncio.to_thread(self.filesystem.isfile, target)

        def write() -> None:
            parent = target.rsplit("/", 1)[0] if "/" in target else ""
            if parent:
                self.filesystem.makedirs(parent, exist_ok=True)
            with self.filesystem.transaction:
                with self.filesystem.open(target, "wb") as handle:
                    handle.write(data)  # type: ignore[arg-type]

        await asyncio.to_thread(write)
        self._publish([ResourceChange(normalized, "modified" if existed else "created")])

    async def delete(self, path: str) -> None:
        target = self.remote_path(path)
        normalized = resource_path(path)
        if await asyncio.to_thread(self.filesystem.exists, target):
            await asyncio.to_thread(self.filesystem.rm, target)
            self._publish([ResourceChange(normalized, "deleted")])

    def _publish(self, changes: list[ResourceChange]) -> None:
        publish = getattr(self._changes, "publish", None)
        if callable(publish):
            publish(changes)

    def subscribe(self, prefix: str = "") -> ResourceSubscription:
        if self._changes is None:
            raise ResourceWatchUnsupported(
                f"{type(self.filesystem).__name__} has no ResourceChangeSource; supply the backend's native event adapter"
            )
        return self._changes.subscribe(prefix)

    async def watch(self, prefix: str = "") -> AsyncIterator[ResourceChange]:
        subscription = self.subscribe(prefix)
        try:
            while True:
                for change in await subscription.next():
                    yield change
        finally:
            await subscription.aclose()

    @classmethod
    def from_mapper(
        cls, mapper: MutableMapping[str, bytes], *, changes: ResourceChangeSource | None = None
    ) -> "WorkspaceResources":
        filesystem = getattr(mapper, "fs", None)
        root = getattr(mapper, "root", None)
        if not isinstance(filesystem, AbstractFileSystem) or not isinstance(root, str):
            raise TypeError("mapper must be an fsspec FSMap with `fs` and `root`")
        return cls(filesystem, root, changes=changes)

    @classmethod
    def from_url(
        cls, url: str, *, changes: ResourceChangeSource | None = None, **storage_options
    ) -> "WorkspaceResources":
        filesystem, root = fsspec.core.url_to_fs(url, **storage_options)
        return cls(filesystem, root, changes=changes)

    def materialize(self) -> AbstractAsyncContextManager[MaterializedResources]:
        if isinstance(self.filesystem, LocalFileSystem):
            return _LocalMaterialization(Path(self.root))
        return _FsspecMaterialization(self)


class OverlayResources:
    """Ordered fsspec-backed layers with an explicit writable upper layer."""

    def __init__(self, *layers: WorkspaceResourcesLike, writable: WorkspaceResourcesLike) -> None:
        self.layers = (*layers, writable)
        self.writable = writable
        self._deleted: set[str] = set()

    @property
    def local_path(self) -> Path | None:
        return None

    async def read(self, path: str) -> bytes | None:
        normalized = resource_path(path)
        if normalized in self._deleted:
            return None
        for layer in reversed(self.layers):
            value = await layer.read(normalized)
            if value is not None:
                return value
        return None

    async def list(self, prefix: str = "") -> list[str]:
        found: set[str] = set()
        for layer in self.layers:
            found.update(await layer.list(prefix))
        return sorted(found - self._deleted)

    async def write(self, path: str, data: bytes) -> None:
        normalized = resource_path(path)
        self._deleted.discard(normalized)
        await self.writable.write(normalized, data)

    async def delete(self, path: str) -> None:
        normalized = resource_path(path)
        self._deleted.add(normalized)
        await self.writable.delete(normalized)

    async def watch(self, prefix: str = "") -> AsyncIterator[ResourceChange]:
        async for change in self.writable.watch(prefix):
            yield change

    def subscribe(self, prefix: str = "") -> ResourceSubscription:
        return self.writable.subscribe(prefix)

    def materialize(self) -> AbstractAsyncContextManager[MaterializedResources]:
        overlay = self

        class _OverlayMaterialization(AbstractAsyncContextManager[MaterializedResources]):
            def __init__(self) -> None:
                self._temporary: tempfile.TemporaryDirectory[str] | None = None
                self._view: MaterializedResources | None = None
                self._baseline: dict[str, bytes] = {}

            async def __aenter__(self) -> MaterializedResources:
                self._temporary = tempfile.TemporaryDirectory(prefix="langmesh-overlay-")
                root = Path(self._temporary.name)
                for path in await overlay.list():
                    data = await overlay.read(path)
                    if data is None:
                        continue
                    self._baseline[path] = data
                    destination = root / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(data)

                async def publish() -> None:
                    current = {
                        path.relative_to(root).as_posix(): path.read_bytes()
                        for path in root.rglob("*")
                        if path.is_file()
                    }
                    for removed in set(self._baseline) - set(current):
                        await overlay.delete(removed)
                    for path, data in current.items():
                        if self._baseline.get(path) != data:
                            await overlay.write(path, data)
                    self._baseline = current

                async def refresh() -> None:
                    remote = {
                        path: await overlay.read(path) or b"" for path in await overlay.list()
                    }
                    for existing in [path for path in root.rglob("*") if path.is_file()]:
                        relative = existing.relative_to(root).as_posix()
                        if relative not in remote:
                            existing.unlink()
                    for path, data in remote.items():
                        destination = root / path
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(data)
                    self._baseline = remote

                self._view = MaterializedResources(root, publish, refresh)
                return self._view

            async def __aexit__(self, *_exception: object) -> None:
                if self._view is not None:
                    await self._view.sync()
                if self._temporary is not None:
                    self._temporary.cleanup()

        return _OverlayMaterialization()


__all__ = [
    "LocalResourceChanges",
    "InProcessResourceChanges",
    "MaterializedResources",
    "OverlayResources",
    "ResourceChange",
    "ResourceChangeSource",
    "ResourceSubscription",
    "ResourceWatchUnsupported",
    "WorkspaceResources",
    "WorkspaceResourcesLike",
    "resource_path",
]
