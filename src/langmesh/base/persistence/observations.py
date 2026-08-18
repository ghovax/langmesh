"""Configured observational-memory access over workspace resources."""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator

from langmesh.base.configuration import Configuration
from langmesh.base.persistence.observation_store import OBSERVATIONS_FILENAME, SQLiteObservationStore
from langmesh.base.persistence.resources import MaterializedResources, WorkspaceResourcesLike


class ObservationRegistry:
    """A configured read-only view of one workspace's observational-memory registry."""

    def __init__(
        self,
        resources: WorkspaceResourcesLike,
        *,
        configuration: Configuration | None = None,
    ) -> None:
        if not isinstance(resources, WorkspaceResourcesLike):
            raise TypeError("resources must satisfy WorkspaceResourcesLike")
        self._resources = resources
        self._configuration = configuration or Configuration()

    def _path(self, materialized: MaterializedResources) -> Path:
        return materialized.path / self._subscription_path()

    def _subscription_path(self) -> str:
        agents_root = Path(self._configuration.AGENTS_ROOT_DIRECTORY).expanduser()
        if not agents_root.is_absolute():
            return (agents_root / OBSERVATIONS_FILENAME).as_posix()
        local_root = self._resources.local_path
        if local_root is None:
            raise ValueError(
                "an absolute AGENTS_ROOT_DIRECTORY cannot be addressed through non-local workspace resources"
            )
        try:
            return (agents_root / OBSERVATIONS_FILENAME).relative_to(local_root).as_posix()
        except ValueError as error:
            raise ValueError(
                "AGENTS_ROOT_DIRECTORY must be inside the configured workspace resources"
            ) from error

    @staticmethod
    def _logical_path(path: Path, root: Path) -> str:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return str(path)

    def _describe_path(self, descriptor: dict[str, Any], path: Path, root: Path) -> dict[str, Any]:
        return {**descriptor, "path": self._logical_path(path, root)}

    async def load(self) -> dict[str, Any]:
        """Read the validated current state without creating or changing it."""
        async with self._resources.materialize() as materialized:
            return await SQLiteObservationStore(self._path(materialized)).snapshot()

    async def describe(self) -> dict[str, Any]:
        """Read path and summary metadata without loading observation payloads."""
        async with self._resources.materialize() as materialized:
            path = self._path(materialized)
            descriptor = await SQLiteObservationStore(path).describe()
            return self._describe_path(descriptor, path, materialized.path)

    async def watch(self) -> AsyncIterator[dict[str, Any]]:
        """Yield the initial state and each distinct state after a committed resource event."""
        subscription = self._resources.subscribe(self._subscription_path())
        try:
            previous: dict[str, Any] | None = None
            while True:
                async with self._resources.materialize() as materialized:
                    path = self._path(materialized)
                    snapshot, descriptor = await SQLiteObservationStore(
                        path
                    ).snapshot_with_metadata()
                    current = {
                        **snapshot,
                        "metadata": self._describe_path(descriptor, path, materialized.path),
                    }
                if current != previous:
                    previous = current
                    yield current
                await subscription.next()
        finally:
            await subscription.aclose()


__all__ = ["ObservationRegistry"]
