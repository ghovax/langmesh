"""Explicit checkpoint adapters for embedded sessions."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections.abc import Callable
from typing import TypeVar

from sqlalchemy import Column, DateTime, MetaData, String, Table, Text, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from langmesh.runtime.session_control import SessionCheckpoint

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_Result = TypeVar("_Result")


class SQLiteCheckpoints:
    """Session checkpoints in a caller-owned SQLite connection."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        table: str = "checkpoints",
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be a sqlite3.Connection")
        if not _IDENTIFIER.fullmatch(table):
            raise ValueError("table must be a plain SQLite identifier")
        if connection.in_transaction:
            raise ValueError("connection must not have an open transaction")
        self._connection = connection
        self._table = table
        self._lock = asyncio.Lock()
        self._transaction(
            lambda: self._connection.execute(
                f"CREATE TABLE IF NOT EXISTS {self._table} (session_id TEXT PRIMARY KEY, checkpoint TEXT NOT NULL)"
            )
        )

    def _transaction(self, action: Callable[[], _Result]) -> _Result:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            result = action()
            self._connection.commit()
            return result
        except BaseException:
            self._connection.rollback()
            raise

    async def save(self, session_id: str, checkpoint: SessionCheckpoint) -> None:
        if not session_id:
            raise ValueError("session_id must not be empty")
        if not isinstance(checkpoint, SessionCheckpoint):
            raise TypeError("checkpoint must be a SessionCheckpoint value")
        payload = json.dumps(checkpoint.to_data(), ensure_ascii=False, separators=(",", ":"))
        async with self._lock:
            self._transaction(
                lambda: self._connection.execute(
                    f"INSERT INTO {self._table} (session_id, checkpoint) VALUES (?, ?) ON CONFLICT(session_id) DO UPDATE SET checkpoint = excluded.checkpoint",
                    (session_id, payload),
                )
            )

    async def load(self, session_id: str) -> SessionCheckpoint | None:
        if not session_id:
            raise ValueError("session_id must not be empty")
        async with self._lock:
            row = self._connection.execute(
                f"SELECT checkpoint FROM {self._table} WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        data = json.loads(str(row[0]))
        if not isinstance(data, dict):
            raise ValueError("stored checkpoint must be a JSON object")
        return SessionCheckpoint.from_data(data)


class SQLAlchemyCheckpoints:
    """Session checkpoints in a caller-owned SQLAlchemy async engine."""

    def __init__(self, engine: AsyncEngine, *, table: str = "session_checkpoints") -> None:
        if not isinstance(engine, AsyncEngine):
            raise TypeError("engine must be a sqlalchemy.ext.asyncio.AsyncEngine")
        if not _IDENTIFIER.fullmatch(table):
            raise ValueError("table must be a plain SQL identifier")
        self._engine = engine
        self._metadata = MetaData()
        self._table = Table(
            table,
            self._metadata,
            Column("session_id", String, primary_key=True),
            Column("checkpoint", Text, nullable=False),
            Column(
                "updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()
            ),
        )

    async def initialize(self) -> None:
        async with self._engine.begin() as connection:
            await connection.run_sync(self._metadata.create_all)

    async def save(self, session_id: str, checkpoint: SessionCheckpoint) -> None:
        if not session_id:
            raise ValueError("session_id must not be empty")
        if not isinstance(checkpoint, SessionCheckpoint):
            raise TypeError("checkpoint must be a SessionCheckpoint value")
        payload = json.dumps(checkpoint.to_data(), ensure_ascii=False, separators=(",", ":"))
        async with self._engine.begin() as connection:
            values = {"session_id": session_id, "checkpoint": payload}
            if connection.dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert

                statement = insert(self._table).values(**values)
                statement = statement.on_conflict_do_update(
                    index_elements=[self._table.c.session_id],
                    set_={"checkpoint": payload, "updated_at": func.now()},
                )
                await connection.execute(statement)
            elif connection.dialect.name == "sqlite":
                from sqlalchemy.dialects.sqlite import insert

                statement = insert(self._table).values(**values)
                statement = statement.on_conflict_do_update(
                    index_elements=[self._table.c.session_id],
                    set_={"checkpoint": payload, "updated_at": func.now()},
                )
                await connection.execute(statement)
            else:
                changed = await connection.execute(
                    update(self._table)
                    .where(self._table.c.session_id == session_id)
                    .values(checkpoint=payload, updated_at=func.now())
                )
                if not changed.rowcount:
                    await connection.execute(self._table.insert().values(**values))

    async def load(self, session_id: str) -> SessionCheckpoint | None:
        if not session_id:
            raise ValueError("session_id must not be empty")
        async with self._engine.connect() as connection:
            result = await connection.execute(
                select(self._table.c.checkpoint).where(self._table.c.session_id == session_id)
            )
            row = result.first()
        if row is None:
            return None
        data = json.loads(str(row[0]))
        if not isinstance(data, dict):
            raise ValueError("stored checkpoint must be a JSON object")
        return SessionCheckpoint.from_data(data)


__all__ = ["SQLiteCheckpoints", "SQLAlchemyCheckpoints"]
