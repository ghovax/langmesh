"""Async, event-driven observation-registry fan-out for the interface and live agents."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from langmesh.base.persistence.observation_store import (
    SQLiteObservationStore,
    NativeFileSubscription,
)


logger = logging.getLogger(__name__)


class ObservationRegistryWatcher:
    """Lazily watch requested registries, sharing one native watcher per location."""

    def __init__(self, registry: Any, host: Any, broadcaster: Any, configuration: Any) -> None:
        self._registry = registry
        self._host = host
        self._broadcaster = broadcaster
        self._configuration = configuration
        self._tasks: dict[Path, asyncio.Task] = {}
        self._subscriptions: dict[Path, NativeFileSubscription] = {}
        self._snapshots: dict[Path, dict[str, Any]] = {}
        self._registration_locks: dict[Path, asyncio.Lock] = {}
        self._closed = False

    async def register(self, working_directory: str) -> dict[str, Any]:
        path = self._path_for(working_directory)
        lock = self._registration_locks.setdefault(path, asyncio.Lock())
        async with lock:
            if path in self._tasks and path in self._snapshots:
                return self._snapshots[path]
            # Install the native subscription before reading. A commit racing registration is therefore either in this snapshot or queued as the first watcher event.
            subscription = NativeFileSubscription(path)
            snapshot = await self._read(path)
            self._snapshots[path] = snapshot
            if path not in self._tasks and not self._closed:
                self._subscriptions[path] = subscription
                task = asyncio.create_task(self._watch(path, subscription))
                self._tasks[path] = task
                task.add_done_callback(
                    lambda completed, target=path: self._retire(target, completed)
                )
            else:
                await asyncio.to_thread(subscription.close)
            return snapshot

    def _path_for(self, working_directory: str) -> Path:
        return self._configuration.observation_database_for(working_directory).resolve(strict=False)

    async def _read(self, path: Path) -> dict[str, Any]:
        """Read once after the native watcher reports a settled event."""
        store = SQLiteObservationStore(path)
        try:
            snapshot, metadata = await store.snapshot_with_metadata()
        except Exception as error:  # noqa: BLE001 — reported as registry feedback below
            message = str(error) or type(error).__name__
        else:
            return {**snapshot, "metadata": metadata, "error": ""}
        # A registry that refused to validate is itself reported: describe() never raises and carries `status: broken|missing` plus the problem.
        try:
            metadata = await store.describe()
        except Exception:  # noqa: BLE001 — a descriptor is best-effort around a broken file
            metadata = {}
        previous = self._snapshots.get(path) or {
            "revision": 0,
            "entries": {"observations": [], "directives": []},
            "metadata": {
                "path": str(path),
                "exists": path.exists(),
                "revision": 0,
                "counts": {"observations": 0, "directives": 0},
                "updated_at": {"earliest": None, "latest": None},
                "status": "broken",
                "problem": message,
            },
        }
        return {**previous, "metadata": metadata, "error": message}

    async def _watch(self, path: Path, subscription: NativeFileSubscription) -> None:
        try:
            while not self._closed:
                changes = await subscription.next()
                if not changes:
                    return
                if subscription.requires_rebase(changes):
                    replacement = NativeFileSubscription(path)
                    self._subscriptions[path] = replacement
                    await asyncio.to_thread(subscription.close)
                    subscription = replacement
                snapshot = await self._read(path)
                if snapshot != self._snapshots.get(path):
                    self._snapshots[path] = snapshot
                    await self._publish(path, snapshot)
        finally:
            await asyncio.to_thread(subscription.close)

    async def _publish(self, path: Path, snapshot: dict[str, Any]) -> None:
        sessions = self._sessions_for(path)
        self._broadcaster.publish(
            {
                "type": "observation_registry_changed",
                "sessions": [record.id for record in sessions],
                **snapshot,
            }
        )
        await asyncio.gather(
            *(
                self._host.dispatch(
                    record.id,
                    "session/observation-registry",
                    {
                        "error": snapshot.get("error") or "",
                        "metadata": snapshot.get("metadata") or {},
                    },
                )
                for record in sessions
                if self._host.hosts(record.id)
            ),
            return_exceptions=True,
        )

    def _sessions_for(self, path: Path) -> list[Any]:
        return [
            record
            for record in self._registry.live()
            if self._path_for(record.runtime_working_directory or record.working_directory) == path
        ]

    def _retire(self, path: Path, task: asyncio.Task) -> None:
        if self._tasks.get(path) is task:
            self._tasks.pop(path, None)
            self._subscriptions.pop(path, None)
        if not task.cancelled():
            try:
                task.result()
            except Exception:
                logger.exception("observation registry watcher stopped for %s", path)

    async def aclose(self) -> None:
        self._closed = True
        tasks = list(self._tasks.values())
        self._tasks.clear()
        self._registration_locks.clear()
        subscriptions = list(self._subscriptions.values())
        self._subscriptions.clear()
        await asyncio.gather(
            *(asyncio.to_thread(subscription.close) for subscription in subscriptions),
            return_exceptions=True,
        )
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
