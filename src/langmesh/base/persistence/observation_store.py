"""Read the current observational-memory registry owned by a workspace or location."""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from enum import IntEnum
from typing import Any, AsyncIterator

from watchdog.events import FileSystemEvent, FileSystemEventHandler, FileSystemMovedEvent
from watchdog.observers import Observer


OBSERVATIONS_FILENAME = "observations.sqlite"
_LEDGERS = ("observations", "directives")
_OBSERVATION_FIELDS = {"category", "claim", "detail", "evidence", "standing", "files"}
_DIRECTIVE_FIELDS = {"kind", "summary", "detail", "occasion", "files"}


class ObservationRegistryError(ValueError):
    """A registry cannot be read as the current schema, stated so an agent can repair it."""


class NativeFileChange(IntEnum):
    CREATED = 1
    MODIFIED = 2
    DELETED = 3


class NativeFileSubscription:
    """An already-open native file subscription, so reading after construction cannot miss a commit."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self.agents_root = self.path.parent
        self.watch_root = self.agents_root if self.agents_root.is_dir() else self.agents_root.parent
        self._closed = False
        self._loop = asyncio.get_running_loop()
        self._events: asyncio.Queue[set[tuple[NativeFileChange, str]]] = asyncio.Queue()
        subscription = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event: FileSystemEvent) -> None:
                if subscription._closed:
                    return
                if event.event_type in {"opened", "closed", "closed_no_write"}:
                    return
                kind = {
                    "created": NativeFileChange.CREATED,
                    "modified": NativeFileChange.MODIFIED,
                    "deleted": NativeFileChange.DELETED,
                    "moved": NativeFileChange.MODIFIED,
                }.get(event.event_type)
                if kind is None:
                    return
                changes = {(kind, str(Path(event.src_path).resolve(strict=False)))}
                if isinstance(event, FileSystemMovedEvent):
                    changes = {
                        (NativeFileChange.DELETED, str(Path(event.src_path).resolve(strict=False))),
                        (
                            NativeFileChange.CREATED,
                            str(Path(event.dest_path).resolve(strict=False)),
                        ),
                    }
                subscription._loop.call_soon_threadsafe(subscription._events.put_nowait, changes)

        self._observer = Observer()
        self._observer.schedule(Handler(), str(self.watch_root), recursive=False)
        # `start()` starts each native emitter before returning, so construction is the subscription-readiness boundary and a following snapshot cannot open a race gap.
        self._observer.start()

    @property
    def needs_rebase(self) -> bool:
        return (self.watch_root == self.agents_root) != self.agents_root.is_dir()

    def requires_rebase(self, changes: set[tuple[NativeFileChange, str]]) -> bool:
        """Whether this batch replaced the watched directory or changed which root exists."""
        agents = self.agents_root.resolve(strict=False)
        return self.needs_rebase or any(
            Path(changed).resolve(strict=False) == agents for _, changed in changes
        )

    async def next(self) -> set[tuple[NativeFileChange, str]]:
        """Wait for relevant native events, coalescing only events already queued."""
        target = str(self.path)
        agents = str(self.agents_root)
        watch_root = str(self.watch_root)
        while not self._closed:
            raw = await self._events.get()
            while not self._events.empty():
                raw.update(self._events.get_nowait())
            changes = {
                (change, changed)
                for change, changed in raw
                # macOS FSEvents may coalesce creation of a missing `.agents` child into a change on its subscribed parent. That event is the signal to rebase and take the now-complete registry snapshot; it is not a polling fallback.
                if changed == target
                or (changed == agents and change is not NativeFileChange.MODIFIED)
                or (changed == watch_root and watch_root != agents and self.needs_rebase)
            }
            if changes:
                return changes
        return set()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._observer.stop()
        self._observer.join()
        if not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._events.put_nowait, set())


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_payload(ledger: str, entry_id: str, payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ObservationRegistryError(f"{ledger}/{entry_id}: payload must be a JSON object")
    allowed = _OBSERVATION_FIELDS if ledger == "observations" else _DIRECTIVE_FIELDS
    unexpected = sorted(set(payload) - allowed)
    if unexpected:
        raise ObservationRegistryError(
            f"{ledger}/{entry_id}: unsupported payload fields: {', '.join(unexpected)}"
        )
    if ledger == "observations":
        if payload.get("category") not in {
            "fact",
            "decision",
            "constraint",
            "failure",
            "artifact",
            "open",
        }:
            raise ObservationRegistryError(f"{ledger}/{entry_id}: invalid category")
        if payload.get("standing") not in {"verified", "reported", "inferred"}:
            raise ObservationRegistryError(f"{ledger}/{entry_id}: invalid standing")
        required = ("claim", "detail")
        optional = ("evidence",)
    else:
        if payload.get("kind") not in {"requirement", "preference"}:
            raise ObservationRegistryError(f"{ledger}/{entry_id}: invalid kind")
        required = ("summary",)
        optional = ("detail", "occasion")
    for field in required:
        if not _nonempty(payload.get(field)):
            raise ObservationRegistryError(f"{ledger}/{entry_id}: {field} must be non-empty")
    for field in optional:
        if field in payload and not _nonempty(payload[field]):
            raise ObservationRegistryError(
                f"{ledger}/{entry_id}: {field} must be non-empty when present"
            )

    if "files" in payload:
        files = payload["files"]
        if (
            not isinstance(files, list)
            or not files
            or any(not _nonempty(path) for path in files)
        ):
            raise ObservationRegistryError(
                f"{ledger}/{entry_id}: files must be a non-empty list of non-empty paths"
            )


class SQLiteObservationStore:
    """A read-only application view of the current-state registry the agent manages."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)

    def _connect_read_only(self) -> sqlite3.Connection:
        """Open an existing registry without a deletion race ever creating a replacement file."""
        return sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True, timeout=30)

    async def revision(self) -> int:
        return await asyncio.to_thread(self._revision_sync)

    def _revision_sync(self) -> int:
        if not self.path.exists():
            return 0
        with self._connect_read_only() as connection:
            return self._validate_schema(connection)

    async def entries(self) -> dict[str, list[dict[str, Any]]]:
        return (await self.snapshot())["entries"]

    async def snapshot(self) -> dict[str, Any]:
        """Read one revision and its entries from the same SQLite snapshot."""
        return await asyncio.to_thread(self._snapshot_sync)

    async def describe(self) -> dict[str, Any]:
        """Return prompt-safe registry metadata without exposing any observation payload."""
        return await asyncio.to_thread(self.describe_sync)

    def describe_sync(self) -> dict[str, Any]:
        """Read bounded metadata from one validated transaction without loading payloads."""
        if not self.path.exists():
            return self._empty_description()
        with self._connect_read_only() as connection:
            connection.execute("BEGIN")
            revision = self._validate_schema(connection)
            aggregates: dict[str, tuple[int, object, object]] = {}
            for ledger in _LEDGERS:
                count, earliest, latest = connection.execute(
                    f"SELECT COUNT(*), MIN(updated_at), MAX(updated_at) FROM {ledger}"
                ).fetchone()
                aggregates[ledger] = (int(count), earliest, latest)
        earliest_values = [
            str(values[1]) for values in aggregates.values() if values[1] is not None
        ]
        latest_values = [str(values[2]) for values in aggregates.values() if values[2] is not None]
        return {
            "path": str(self.path),
            "exists": True,
            "revision": revision,
            "counts": {ledger: aggregates.get(ledger, (0, None, None))[0] for ledger in _LEDGERS},
            "updated_at": {
                "earliest": min(earliest_values) if earliest_values else None,
                "latest": max(latest_values) if latest_values else None,
            },
        }

    async def snapshot_with_metadata(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return the interface payload and prompt descriptor from one validated read."""
        return await asyncio.to_thread(self._snapshot_with_metadata_sync)

    def _snapshot_with_metadata_sync(self) -> tuple[dict[str, Any], dict[str, Any]]:
        snapshot = self._snapshot_sync()
        return snapshot, self._describe_snapshot(snapshot)

    def _describe_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        timestamps = [
            str(entry["updated_at"]) for ledger in _LEDGERS for entry in snapshot["entries"][ledger]
        ]
        return {
            "path": str(self.path),
            "exists": self.path.exists(),
            "revision": snapshot["revision"],
            "counts": {ledger: len(snapshot["entries"][ledger]) for ledger in _LEDGERS},
            "updated_at": {
                "earliest": min(timestamps) if timestamps else None,
                "latest": max(timestamps) if timestamps else None,
            },
        }

    def _empty_description(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "exists": False,
            "revision": 0,
            "counts": {ledger: 0 for ledger in _LEDGERS},
            "updated_at": {"earliest": None, "latest": None},
        }

    async def watch(self) -> AsyncIterator[dict[str, Any]]:
        """Yield the current snapshot and each distinct committed snapshot after it."""
        subscription = NativeFileSubscription(self.path)
        try:
            previous = await self.snapshot()
            yield previous
            while True:
                changes = await subscription.next()
                if not changes:
                    return
                if subscription.requires_rebase(changes):
                    replacement = NativeFileSubscription(self.path)
                    await asyncio.to_thread(subscription.close)
                    subscription = replacement
                current = await self.snapshot()
                if current != previous:
                    previous = current
                    yield current
        finally:
            await asyncio.to_thread(subscription.close)

    def _snapshot_sync(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "revision": 0,
                "entries": {ledger: [] for ledger in _LEDGERS},
            }
        with self._connect_read_only() as connection:
            connection.execute("BEGIN")
            revision = self._validate_schema(connection)
            rows_by_ledger: dict[str, list[tuple[str, object, str]]] = {}
            for ledger in _LEDGERS:
                rows_by_ledger[ledger] = [
                    (str(entry_id), payload, str(updated_at))
                    for entry_id, payload, updated_at in connection.execute(
                        f"SELECT entry_id, payload, updated_at FROM {ledger} "
                        "ORDER BY updated_at, entry_id"
                    ).fetchall()
                ]
        entries: dict[str, list[dict[str, Any]]] = {ledger: [] for ledger in _LEDGERS}
        for ledger, rows in rows_by_ledger.items():
            for entry_id, payload, updated_at in rows:
                if not _nonempty(entry_id):
                    raise ObservationRegistryError(f"{ledger}/<empty>: entry_id must be non-empty")
                if not _nonempty(updated_at):
                    raise ObservationRegistryError(
                        f"{ledger}/{entry_id}: updated_at must be non-empty"
                    )
                try:
                    parsed_timestamp = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                except ValueError as error:
                    raise ObservationRegistryError(
                        f"{ledger}/{entry_id}: updated_at must be an ISO 8601 timestamp"
                    ) from error
                if parsed_timestamp.tzinfo is None:
                    raise ObservationRegistryError(
                        f"{ledger}/{entry_id}: updated_at must include a UTC offset"
                    )
                try:
                    # Payloads are stored as BLOBs of UTF-8 JSON, which json.loads accepts directly.
                    entry = json.loads(payload)
                except (json.JSONDecodeError, TypeError) as error:
                    raise ObservationRegistryError(
                        f"{ledger}/{entry_id}: payload is not valid JSON"
                    ) from error
                _validate_payload(ledger, entry_id, entry)
                entry.update(id=entry_id, updated_at=updated_at)
                entries[ledger].append(entry)
        return {"revision": revision, "entries": entries}

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> int:
        integrity = connection.execute("PRAGMA quick_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise ObservationRegistryError(
                f"SQLite integrity check failed: {integrity[0] if integrity else 'no result'}"
            )
        entry_columns = [
            ("entry_id", "TEXT", 1, 1),
            ("payload", "BLOB", 1, 0),
            ("updated_at", "TEXT", 1, 0),
        ]
        expected = {
            "registry_meta": [
                ("id", "INTEGER", 0, 1),
                ("revision", "INTEGER", 1, 0),
            ],
            "observations": entry_columns,
            "directives": entry_columns,
        }
        user_objects = {
            (str(name), str(kind))
            for name, kind in connection.execute(
                "SELECT name, type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        expected_objects = {
            ("registry_meta", "table"),
            ("observations", "table"),
            ("directives", "table"),
            ("idx_observations_updated_at", "index"),
            ("idx_directives_updated_at", "index"),
        }
        if user_objects != expected_objects:
            raise ObservationRegistryError(
                f"registry objects must be {sorted(expected_objects)}; found {sorted(user_objects)}"
            )
        for table, columns in expected.items():
            found = [
                (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]))
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            ]
            if found != columns:
                raise ObservationRegistryError(
                    f"{table} columns must be {columns}; found {found or 'no table'}"
                )
        for table in ("observations", "directives"):
            table_sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()[0]
            ).upper()
            compact_table_sql = "".join(table_sql.split())
            if "WITHOUT ROWID" not in table_sql:
                raise ObservationRegistryError(f"{table} must be declared WITHOUT ROWID")
            if "CHECK(JSON_VALID(PAYLOAD))" not in compact_table_sql:
                raise ObservationRegistryError(f"{table} is missing CHECK(json_valid(payload))")
        index_sql = {
            str(name): str(definition).upper()
            for name, definition in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        if "idx_observations_updated_at" not in index_sql or (
            "ONOBSERVATIONS" not in "".join(index_sql["idx_observations_updated_at"].split())
            or "UPDATED_AT" not in "".join(index_sql["idx_observations_updated_at"].split())
        ):
            raise ObservationRegistryError(
                "idx_observations_updated_at must index observations(updated_at)"
            )
        if "idx_directives_updated_at" not in index_sql or (
            "ONDIRECTIVES" not in "".join(index_sql["idx_directives_updated_at"].split())
            or "UPDATED_AT" not in "".join(index_sql["idx_directives_updated_at"].split())
        ):
            raise ObservationRegistryError(
                "idx_directives_updated_at must index directives(updated_at)"
            )
        meta_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='registry_meta'"
            ).fetchone()[0]
        ).upper()
        if "CHECK(ID=1)" not in "".join(meta_sql.split()):
            raise ObservationRegistryError("registry_meta is missing CHECK(id=1)")
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if journal_mode != "delete":
            raise ObservationRegistryError(
                f"journal_mode must be DELETE for a self-contained tracked registry; found {journal_mode}"
            )
        rows = connection.execute("SELECT id, revision FROM registry_meta").fetchall()
        if (
            len(rows) != 1
            or type(rows[0][0]) is not int
            or type(rows[0][1]) is not int
            or rows[0][0] != 1
            or rows[0][1] < 0
        ):
            raise ObservationRegistryError(
                "registry_meta must contain exactly one row with id=1 and a non-negative revision"
            )
        return int(rows[0][1])


__all__ = [
    "OBSERVATIONS_FILENAME",
    "ObservationRegistryError",
    "SQLiteObservationStore",
]
