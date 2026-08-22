"""Explicit checkpoint adapters for embedded sessions."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections.abc import Callable
from typing import TypeVar

from langmesh.runtime.session_control import SessionCheckpoint

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_Result = TypeVar("_Result")


class SQLiteCheckpoints:
    """Session checkpoints in a caller-owned SQLite connection."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        table: str = "langmesh_checkpoints",
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


__all__ = ["SQLiteCheckpoints"]
