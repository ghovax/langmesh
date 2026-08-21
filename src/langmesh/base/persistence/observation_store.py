"""Read the current observational-memory registry owned by a workspace or location.

An SQLAlchemy Core view over the agent-maintained registry: read-only (`mode=ro`), re-validating
the documented columnar schema before trusting any row. A missing or broken registry is never a
crash — it is reported as metadata with a ``status`` so the model hears about it and repairs it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import os
from pathlib import Path
import re
from urllib.parse import quote
from enum import IntEnum
from typing import Any, AsyncIterator, Literal

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine
from sqlalchemy import func, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import NullPool
from watchdog.events import FileSystemEvent, FileSystemEventHandler, FileSystemMovedEvent
from watchdog.observers import Observer

from langmesh.base.content.observations import (
    DirectiveEntry,
    ObservationEntry,
    ObservationSnapshot,
    RegistryCounts,
    RegistryMetadata,
    RegistryTimestamps,
)


OBSERVATIONS_FILENAME = "observations.sqlite"
#: The ledgers the registry holds, each a table of one row per entry with explicit columns rather than a JSON payload.
_LEDGERS = ("observations", "directives")


#: Each ledger's exact column layout as (name, declared type, NOT NULL, PRIMARY KEY) from `PRAGMA table_info`.
_ENTRY_COLUMNS = {
    "observations": [
        ("entry_id", "TEXT", 1, 1),
        ("category", "TEXT", 1, 0),
        ("claim", "TEXT", 1, 0),
        ("detail", "TEXT", 1, 0),
        ("evidence", "TEXT", 0, 0),
        ("standing", "TEXT", 1, 0),
        ("files", "TEXT", 0, 0),
        ("updated_at", "TEXT", 1, 0),
    ],
    "directives": [
        ("entry_id", "TEXT", 1, 1),
        ("kind", "TEXT", 1, 0),
        ("summary", "TEXT", 1, 0),
        ("detail", "TEXT", 0, 0),
        ("occasion", "TEXT", 0, 0),
        ("files", "TEXT", 0, 0),
        ("updated_at", "TEXT", 1, 0),
    ],
}

#: The invariants each table's declared CHECKs must carry, as tokens that must appear in its compact CREATE TABLE.
_CHECK_TOKENS = {
    "registry_meta": ["CHECK(ID=1)", "CHECK(REVISION>=0)"],
    "observations": {
        "entry_id": ["LENGTH(TRIM(ENTRY_ID))>0"],
        "category": ["CATEGORYIN", "FACT", "DECISION", "CONSTRAINT", "FAILURE", "ARTIFACT", "OPEN"],
        "claim": ["LENGTH(TRIM(CLAIM))>0"],
        "detail": ["LENGTH(TRIM(DETAIL))>0"],
        "evidence": ["EVIDENCEISNULLORLENGTH(TRIM(EVIDENCE))>0"],
        "standing": ["STANDINGIN", "VERIFIED", "REPORTED", "INFERRED"],
        "files": ["FILESISNULLORLENGTH(TRIM(FILES))>0"],
    },
    "directives": {
        "entry_id": ["LENGTH(TRIM(ENTRY_ID))>0"],
        "kind": ["KINDIN", "REQUIREMENT", "PREFERENCE"],
        "summary": ["LENGTH(TRIM(SUMMARY))>0"],
        "detail": ["DETAILISNULLORLENGTH(TRIM(DETAIL))>0"],
        "occasion": ["OCCASIONISNULLORLENGTH(TRIM(OCCASION))>0"],
        "files": ["FILESISNULLORLENGTH(TRIM(FILES))>0"],
    },
}


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
                changes = {(kind, str(Path(os.fsdecode(event.src_path)).resolve(strict=False)))}
                if isinstance(event, FileSystemMovedEvent):
                    changes = {
                        (
                            NativeFileChange.DELETED,
                            str(Path(os.fsdecode(event.src_path)).resolve(strict=False)),
                        ),
                        (
                            NativeFileChange.CREATED,
                            str(Path(os.fsdecode(event.dest_path)).resolve(strict=False)),
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


def _timestamp(value: object) -> datetime | None:
    """Parse one stored ISO timestamp when present."""
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value is not None else None


def _validate_row(ledger: str, entry_id: str, columns: dict[str, Any]) -> None:
    """Check one row's column values, the invariants SQLite's CHECKs hold and the reader re-checks."""
    if ledger == "observations":
        if columns.get("category") not in {
            "fact",
            "decision",
            "constraint",
            "failure",
            "artifact",
            "open",
        }:
            raise ObservationRegistryError(f"{ledger}/{entry_id}: invalid category")
        if columns.get("standing") not in {"verified", "reported", "inferred"}:
            raise ObservationRegistryError(f"{ledger}/{entry_id}: invalid standing")
        required = ("claim", "detail")
        optional = ("evidence",)
    else:
        if columns.get("kind") not in {"requirement", "preference"}:
            raise ObservationRegistryError(f"{ledger}/{entry_id}: invalid kind")
        required = ("summary",)
        optional = ("detail", "occasion")
    for field in required:
        if not _nonempty(columns.get(field)):
            raise ObservationRegistryError(f"{ledger}/{entry_id}: {field} must be non-empty")
    for field in optional:
        if field in columns and not _nonempty(columns[field]):
            raise ObservationRegistryError(
                f"{ledger}/{entry_id}: {field} must be non-empty when present"
            )
    if "files" in columns:
        files = columns["files"]
        # A files column is one newline-separated series of paths, not a JSON list, so a writer cannot forget a value.
        if isinstance(files, list):
            paths = [str(path) for path in files]
        else:
            paths = files.splitlines()
        if not paths or any(not _nonempty(path) for path in paths):
            raise ObservationRegistryError(
                f"{ledger}/{entry_id}: files must be non-empty newline-separated paths"
            )


#: Each ledger's columns as (field, sqlite column name), in wire order after entry_id and before updated_at.
_LEDGER_FIELDS = {
    "observations": (
        "category",
        "claim",
        "detail",
        "evidence",
        "standing",
        "files",
    ),
    "directives": ("kind", "summary", "detail", "occasion", "files"),
}

#: SQLite declared types to the SQLAlchemy types this reader binds them to.
_TYPE_MAP = {"TEXT": String, "INTEGER": Integer}


class SQLiteObservationStore:
    """A read-only application view of the current-state registry the agent manages."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self._engine = self._engine_for(self.path)
        # The reader's table defs come from the same column fingerprint validation enforces, so a Core select can never drift from it.
        self._metadata = MetaData()
        self._tables: dict[str, Table] = {
            name: Table(
                name,
                self._metadata,
                *(
                    Column(
                        column_name,
                        _TYPE_MAP[declared_type],
                        primary_key=bool(primary_key),
                        nullable=not bool(not_null),
                    )
                    for column_name, declared_type, not_null, primary_key in columns
                ),
            )
            for name, columns in _ENTRY_COLUMNS.items()
        }
        self._registry_meta = Table(
            "registry_meta",
            self._metadata,
            Column("id", Integer, primary_key=True),
            Column("revision", Integer, nullable=False),
        )

    @staticmethod
    def _engine_for(path: Path) -> Engine:
        """A read-only engine: the file URI carries ``mode=ro``, so a missing file cannot be created and a replacement can never be written."""
        url = make_url("sqlite+pysqlite://").set(
            database=f"file:{quote(path.as_posix())}",
            query={"mode": "ro", "uri": "true"},
        )
        return create_engine(url, poolclass=NullPool)

    def _connection(self) -> Any:
        """One read-only connection with a deferred read snapshot, matching the original driver's transaction shape."""
        connection = self._engine.connect()
        connection.exec_driver_sql("BEGIN")
        return connection

    async def revision(self) -> int:
        return await asyncio.to_thread(self._revision_sync)

    def _revision_sync(self) -> int:
        if not self.path.exists():
            return 0
        with self._connection() as connection:
            return self._validate_schema(connection)

    async def snapshot(self) -> ObservationSnapshot:
        """Read one revision and its entries from the same SQLite snapshot."""
        return await asyncio.to_thread(self._snapshot_sync)

    async def describe(self) -> RegistryMetadata:
        """Return prompt-safe registry metadata, never raising: a missing or broken registry is itself reported."""
        return await asyncio.to_thread(self.describe_sync)

    def describe_sync(self) -> RegistryMetadata:
        """Read bounded metadata from one validated transaction without loading payloads.

        Never raises: a registry that is missing or mis-schemed is itself the report — ``status: missing|broken`` plus a ``problem`` message.
        """
        if not self.path.exists():
            return self._empty_description()
        revision = 0
        aggregates: dict[str, tuple[int, object, object]] = {}
        problem = ""
        try:
            with self._connection() as connection:
                revision = self._validate_schema(connection)
                aggregates = self._aggregates(connection)
        except ObservationRegistryError as error:
            problem = str(error)
        return self._registry_description(revision, aggregates, problem=problem)

    def _aggregates(self, connection: Any) -> dict[str, tuple[int, object, object]]:
        """Per-ledger (count, earliest, latest) for ledgers whose schema validated."""
        aggregates: dict[str, tuple[int, object, object]] = {}
        for ledger in _LEDGERS:
            table = self._tables[ledger]
            count = connection.execute(select(func.count()).select_from(table)).scalar()
            earliest = connection.execute(select(func.min(table.c.updated_at))).scalar()
            latest = connection.execute(select(func.max(table.c.updated_at))).scalar()
            aggregates[ledger] = (int(count or 0), earliest, latest)
        return aggregates

    def _registry_description(
        self,
        revision: int,
        aggregates: dict[str, tuple[int, object, object]],
        *,
        problem: str,
    ) -> RegistryMetadata:
        """The descriptor every call path produces, unified on ``status`` and ``problem``."""
        earliest_values = [
            parsed
            for values in aggregates.values()
            if (parsed := _timestamp(values[1])) is not None
        ]
        latest_values = [
            parsed
            for values in aggregates.values()
            if (parsed := _timestamp(values[2])) is not None
        ]
        exists = self.path.exists()
        return RegistryMetadata(
            path=str(self.path),
            exists=exists,
            revision=revision,
            counts=RegistryCounts(
                observations=aggregates.get("observations", (0, None, None))[0],
                directives=aggregates.get("directives", (0, None, None))[0],
            ),
            updated_at=RegistryTimestamps(
                earliest=min(earliest_values) if earliest_values else None,
                latest=max(latest_values) if latest_values else None,
            ),
            status=_registry_status(exists, problem=problem),
            problem=problem,
        )

    async def snapshot_with_metadata(self) -> tuple[ObservationSnapshot, RegistryMetadata]:
        """Return the interface payload and prompt descriptor from one validated read."""
        return await asyncio.to_thread(self._snapshot_with_metadata_sync)

    def _snapshot_with_metadata_sync(self) -> tuple[ObservationSnapshot, RegistryMetadata]:
        snapshot = self._snapshot_sync()
        return snapshot, self._describe_snapshot(snapshot)

    def _describe_snapshot(self, snapshot: ObservationSnapshot) -> RegistryMetadata:
        timestamps = [entry.updated_at for entry in (*snapshot.observations, *snapshot.directives)]
        exists = self.path.exists()
        return RegistryMetadata(
            path=str(self.path),
            exists=exists,
            revision=snapshot.revision,
            counts=RegistryCounts(
                observations=len(snapshot.observations), directives=len(snapshot.directives)
            ),
            updated_at=RegistryTimestamps(
                earliest=min(timestamps) if timestamps else None,
                latest=max(timestamps) if timestamps else None,
            ),
            status=_registry_status(exists),
        )

    def _empty_description(self) -> RegistryMetadata:
        return RegistryMetadata(path=str(self.path))

    async def watch(self) -> AsyncIterator[ObservationSnapshot]:
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

    def _snapshot_sync(self) -> ObservationSnapshot:
        if not self.path.exists():
            return ObservationSnapshot()
        with self._connection() as connection:
            revision = self._validate_schema(connection)
            rows_by_ledger: dict[str, list[tuple[Any, ...]]] = {}
            for ledger in _LEDGERS:
                table = self._tables[ledger]
                columns = ("entry_id", *_LEDGER_FIELDS[ledger], "updated_at")
                rows_by_ledger[ledger] = [
                    tuple(value for value in row)
                    for row in connection.execute(
                        select(*(table.c[column] for column in columns)).order_by(
                            table.c.updated_at, table.c.entry_id
                        )
                    ).all()
                ]
        observations: list[ObservationEntry] = []
        directives: list[DirectiveEntry] = []
        for ledger, rows in rows_by_ledger.items():
            columns = ("entry_id", *_LEDGER_FIELDS[ledger], "updated_at")
            for row in rows:
                values = dict(zip(columns, row, strict=True))
                entry_id = str(values["entry_id"])
                updated_at = str(values["updated_at"])
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
                # Optional fields are absent when NULL; `files` surfaces as its newline-parsed list.
                entry = {
                    field: values[field].splitlines()
                    if field == "files" and isinstance(values[field], str)
                    else values[field]
                    for field in _LEDGER_FIELDS[ledger]
                    if values[field] is not None
                }
                _validate_row(ledger, entry_id, entry)
                entry.update(id=entry_id, updated_at=parsed_timestamp)
                if ledger == "observations":
                    observations.append(ObservationEntry.model_validate(entry))
                else:
                    directives.append(DirectiveEntry.model_validate(entry))
        return ObservationSnapshot(
            revision=revision,
            observations=tuple(observations),
            directives=tuple(directives),
        )

    def _validate_schema(self, connection: Any) -> int:
        """Verify the registry matches the documented columnar schema before any row is read.

        The ledgers are plain columns with NOT NULL and CHECK invariants rather than one JSON
        payload BLOB, so a writer cannot silently drop a required field. The reader re-checks
        every row semantically in ``_validate_row``; this pass checks the schema itself.
        """
        integrity = connection.exec_driver_sql("PRAGMA quick_check").scalar()
        if not isinstance(integrity, str) or integrity.strip().lower() != "ok":
            raise ObservationRegistryError(
                f"SQLite integrity check failed: {integrity if integrity is not None else 'no result'}"
            )
        expected = {
            "registry_meta": [
                ("id", "INTEGER", 1, 1),
                ("revision", "INTEGER", 1, 0),
            ],
            **_ENTRY_COLUMNS,
        }
        user_objects = {
            (str(name), str(kind))
            for name, kind in connection.exec_driver_sql(
                "SELECT name, type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).all()
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
                for row in connection.exec_driver_sql(f"PRAGMA table_info({table})").all()
            ]
            if found != columns:
                raise ObservationRegistryError(
                    f"{table} columns must be {columns}; found {found or 'no table'}"
                )
        for table in ("observations", "directives"):
            table_sql = str(
                connection.exec_driver_sql(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).scalar()
            )
            compact_table_sql = re.sub(r'["`\[\]]', "", table_sql.upper())
            compact_table_sql = "".join(compact_table_sql.split())
            if "WITHOUTROWID" not in compact_table_sql:
                raise ObservationRegistryError(f"{table} must be declared WITHOUT ROWID")
            required = _CHECK_TOKENS[table]
            missing = [
                token
                for tokens in required.values()
                for token in tokens
                if token not in compact_table_sql
            ]
            if missing:
                raise ObservationRegistryError(
                    f"{table} is missing schema invariants: {', '.join(missing)}"
                )
        index_sql = {
            str(name): str(definition).upper()
            for name, definition in connection.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_%'"
            ).all()
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
            connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='registry_meta'"
            ).scalar()
        )
        compact_meta_sql = "".join(re.sub(r'["`\[\]]', "", meta_sql.upper()).split())
        missing_meta = [
            token for token in _CHECK_TOKENS["registry_meta"] if token not in compact_meta_sql
        ]
        if missing_meta:
            raise ObservationRegistryError(
                "registry_meta is missing schema invariants: " + ", ".join(missing_meta)
            )
        journal_mode = str(connection.exec_driver_sql("PRAGMA journal_mode").scalar()).lower()
        if journal_mode != "delete":
            raise ObservationRegistryError(
                f"journal_mode must be DELETE for a self-contained tracked registry; found {journal_mode}"
            )
        rows = connection.exec_driver_sql("SELECT id, revision FROM registry_meta").all()
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


def _registry_status(exists: bool, *, problem: str = "") -> Literal["missing", "ok", "broken"]:
    """A registry is ``missing``, ``ok``, or ``broken``; ``broken`` is the only problem state."""
    if not exists:
        return "missing"
    return "broken" if problem else "ok"


__all__ = [
    "OBSERVATIONS_FILENAME",
    "ObservationRegistryError",
    "SQLiteObservationStore",
]
