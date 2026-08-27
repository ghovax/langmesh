"""A durable mailbox job: one inbound message, from discovery through SMTP, surviving pause and reboot.

SQLite is the source of truth. IMAP UNSEEN is only how new mail is found. Flags are written
after the reply is stored. A Message-ID outlives a UIDVALIDITY change.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from langmeshd.commons.paths import mail_database_path
from langmeshd.mail.body import canonical_message_id

DISCOVERED = "discovered"
SUBMITTED = "submitted"
COMPLETED = "completed"
POSTED = "posted"
SEEN = "seen"
SKIPPED = "skipped"

INCOMPLETE = (DISCOVERED, SUBMITTED, COMPLETED, POSTED)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MailItem:
    """One inbound mail and everything needed to finish it without going back to IMAP."""

    id: str
    mailbox: str
    uidvalidity: int
    uid: int
    message_id: str
    thread_key: str
    sender: str
    subject: str
    references: str
    in_reply_to: str
    body: str
    text_to_send: str
    first: bool
    client_message_id: str
    session_id: str
    turn_id: str
    injected: bool
    reply_text: str
    outbound_message_id: str
    state: str
    skip_reason: str
    updated_at: str


def _row(row: sqlite3.Row) -> MailItem:
    return MailItem(
        id=str(row["id"]),
        mailbox=str(row["mailbox"]),
        uidvalidity=int(row["uidvalidity"] or 0),
        uid=int(row["uid"] or 0),
        message_id=str(row["message_id"] or ""),
        thread_key=str(row["thread_key"] or ""),
        sender=str(row["sender"] or ""),
        subject=str(row["subject"] or ""),
        references=str(row["references_header"] or ""),
        in_reply_to=str(row["in_reply_to"] or ""),
        body=str(row["body"] or ""),
        text_to_send=str(row["text_to_send"] or ""),
        first=bool(row["first"]),
        client_message_id=str(row["client_message_id"] or ""),
        session_id=str(row["session_id"] or ""),
        turn_id=str(row["turn_id"] or ""),
        injected=bool(row["injected"]),
        reply_text=str(row["reply_text"] or ""),
        outbound_message_id=str(row["outbound_message_id"] or ""),
        state=str(row["state"] or ""),
        skip_reason=str(row["skip_reason"] or ""),
        updated_at=str(row["updated_at"] or ""),
    )


class ThreadStore:
    """Jobs and the thread → session map. One file, fsync'd, so a freeze or kill cannot lose a commit.

    DELETE journaling rather than WAL: a volume snapshot or copy of this path is the whole
    truth, with no sidecar -wal/-shm that a naive backup would drop.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or mail_database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=DELETE")
        self._connection.execute("PRAGMA synchronous=EXTRA")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA temp_store=MEMORY")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS threads (
                thread_key TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                last_message_id TEXT NOT NULL DEFAULT '',
                last_references TEXT NOT NULL DEFAULT '',
                reply_address TEXT NOT NULL DEFAULT '',
                subject TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY,
                mailbox TEXT NOT NULL,
                uidvalidity INTEGER NOT NULL DEFAULT 0,
                uid INTEGER NOT NULL DEFAULT 0,
                message_id TEXT NOT NULL DEFAULT '',
                thread_key TEXT NOT NULL DEFAULT '',
                sender TEXT NOT NULL DEFAULT '',
                subject TEXT NOT NULL DEFAULT '',
                references_header TEXT NOT NULL DEFAULT '',
                in_reply_to TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                text_to_send TEXT NOT NULL DEFAULT '',
                first INTEGER NOT NULL DEFAULT 0,
                client_message_id TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                turn_id TEXT NOT NULL DEFAULT '',
                injected INTEGER NOT NULL DEFAULT 0,
                reply_text TEXT NOT NULL DEFAULT '',
                outbound_message_id TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL,
                skip_reason TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_items_state ON items(state);
            CREATE INDEX IF NOT EXISTS idx_items_message_id ON items(message_id);
            CREATE INDEX IF NOT EXISTS idx_items_uid ON items(mailbox, uidvalidity, uid);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_items_message_id_unique
                ON items(message_id) WHERE message_id != '';
            CREATE UNIQUE INDEX IF NOT EXISTS idx_items_uid_unique
                ON items(mailbox, uidvalidity, uid);
            CREATE INDEX IF NOT EXISTS idx_threads_session ON threads(session_id);
            CREATE INDEX IF NOT EXISTS idx_items_session ON items(session_id);
            CREATE INDEX IF NOT EXISTS idx_items_outbound
                ON items(outbound_message_id) WHERE outbound_message_id != '';
            CREATE TABLE IF NOT EXISTS thread_ids (
                message_id TEXT PRIMARY KEY,
                thread_key TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_thread_ids_thread ON thread_ids(thread_key);
            """
        )
        self._backfill_ids()

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def close(self) -> None:
        self._connection.close()

    def _remember(self, connection: sqlite3.Connection, thread_key: str, message_id: str) -> None:
        message_id = canonical_message_id(message_id)
        if not thread_key or not message_id:
            return
        connection.execute(
            """
            INSERT INTO thread_ids(message_id, thread_key) VALUES (?, ?)
            ON CONFLICT(message_id) DO UPDATE SET thread_key = excluded.thread_key
            """,
            (message_id, thread_key),
        )

    def _backfill_ids(self) -> None:
        """Existing jobs already know inbound and outbound ids; keep them after this table appears."""
        if self._connection.execute("SELECT 1 FROM thread_ids LIMIT 1").fetchone() is not None:
            return
        with self._txn() as connection:
            for row in connection.execute(
                "SELECT thread_key, message_id, outbound_message_id FROM items"
            ):
                self._remember(
                    connection, str(row["thread_key"] or ""), str(row["message_id"] or "")
                )
                self._remember(
                    connection,
                    str(row["thread_key"] or ""),
                    str(row["outbound_message_id"] or ""),
                )
            for row in connection.execute("SELECT thread_key, last_message_id FROM threads"):
                self._remember(
                    connection, str(row["thread_key"] or ""), str(row["last_message_id"] or "")
                )

    def remember_message_id(self, thread_key: str, message_id: str) -> None:
        """An inbound or outbound Message-ID that must keep resolving to this thread."""
        with self._txn() as connection:
            self._remember(connection, thread_key, message_id)

    def session_id(self, thread_key: str) -> str:
        row = self._connection.execute(
            "SELECT session_id FROM threads WHERE thread_key = ?", (thread_key,)
        ).fetchone()
        return str(row["session_id"]) if row is not None else ""

    def thread_key_for_session(self, session_id: str) -> str:
        if not session_id:
            return ""
        row = self._connection.execute(
            "SELECT thread_key FROM threads WHERE session_id = ? LIMIT 1",
            (session_id,),
        ).fetchone()
        return str(row["thread_key"]) if row is not None else ""

    def binding_for_session(self, session_id: str) -> dict[str, str] | None:
        if not session_id:
            return None
        row = self._connection.execute(
            """
            SELECT thread_key, last_message_id, last_references, reply_address, subject
            FROM threads WHERE session_id = ? LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "thread_key": str(row["thread_key"] or ""),
            "last_message_id": str(row["last_message_id"] or ""),
            "last_references": str(row["last_references"] or ""),
            "reply_address": str(row["reply_address"] or ""),
            "subject": str(row["subject"] or ""),
        }

    def get(self, item_id: str) -> MailItem | None:
        row = self._connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return _row(row) if row is not None else None

    def item_by_outbound_message_id(self, message_id: str) -> MailItem | None:
        message_id = canonical_message_id(message_id)
        if not message_id:
            return None
        row = self._connection.execute(
            """
            SELECT * FROM items WHERE outbound_message_id = ?
            ORDER BY updated_at DESC LIMIT 1
            """,
            (message_id,),
        ).fetchone()
        return _row(row) if row is not None else None

    def resolve_thread_key(self, identifiers: list[str], fallback: str) -> str:
        """The thread an In-Reply-To or References id already belongs to, else `fallback`.

        Progress emails are not jobs, so their Message-IDs live here rather than on an item.
        A reply to a progress note must continue the same session.
        """
        for identifier in identifiers:
            identifier = canonical_message_id(identifier)
            if not identifier:
                continue
            named = self._connection.execute(
                "SELECT thread_key FROM thread_ids WHERE message_id = ?",
                (identifier,),
            ).fetchone()
            if named is not None and named["thread_key"]:
                return str(named["thread_key"])
            item = self.item_by_message_id(identifier) or self.item_by_outbound_message_id(
                identifier
            )
            if item is not None and item.thread_key:
                return item.thread_key
            row = self._connection.execute(
                "SELECT thread_key FROM threads WHERE last_message_id = ? LIMIT 1",
                (identifier,),
            ).fetchone()
            if row is not None:
                return str(row["thread_key"])
        return fallback

    def items_for_session(self, session_id: str) -> list[MailItem]:
        if not session_id:
            return []
        rows = self._connection.execute(
            "SELECT * FROM items WHERE session_id = ? ORDER BY uid ASC, id ASC",
            (session_id,),
        ).fetchall()
        return [_row(row) for row in rows]

    def latest_item_for_session(self, session_id: str) -> MailItem | None:
        if not session_id:
            return None
        row = self._connection.execute(
            """
            SELECT * FROM items
            WHERE session_id = ? AND state != ?
            ORDER BY uid DESC, updated_at DESC LIMIT 1
            """,
            (session_id, SKIPPED),
        ).fetchone()
        return _row(row) if row is not None else None

    def note_outbound(self, session_id: str, *, message_id: str, references: str) -> None:
        key = self.thread_key_for_session(session_id)
        if not key:
            return
        with self._txn() as connection:
            connection.execute(
                """
                UPDATE threads SET last_message_id = ?, last_references = ?, updated_at = ?
                WHERE thread_key = ?
                """,
                (message_id, references, _now(), key),
            )
            self._remember(connection, key, message_id)

    def mark_replied(
        self,
        session_id: str,
        *,
        reply_text: str,
        outbound_message_id: str,
        expected: tuple[str, ...] = (SUBMITTED, DISCOVERED),
    ) -> None:
        """One SMTP reply covers every inbound still waiting on this session."""
        now = _now()
        key = self.thread_key_for_session(session_id)
        with self._txn() as connection:
            connection.execute(
                f"""
                UPDATE items SET
                    state = ?, reply_text = ?, outbound_message_id = ?, updated_at = ?
                WHERE session_id = ? AND state IN ({",".join("?" * len(expected))})
                """,
                (POSTED, reply_text, outbound_message_id, now, session_id, *expected),
            )
            self._remember(connection, key, outbound_message_id)

    def bind(
        self,
        thread_key: str,
        session_id: str,
        *,
        message_id: str,
        references: str,
        reply_address: str,
        subject: str,
    ) -> None:
        from langmeshd.mail.body import reference_chain

        now = _now()
        inbound = canonical_message_id(message_id)
        with self._txn() as connection:
            existing = connection.execute(
                """
                SELECT last_message_id, last_references, reply_address, subject
                FROM threads WHERE thread_key = ?
                """,
                (thread_key,),
            ).fetchone()
            if existing is None:
                last_id = inbound
                last_refs = references
            else:
                stored_last = canonical_message_id(str(existing["last_message_id"] or ""))
                stored_refs = str(existing["last_references"] or "")
                known = {
                    token for token in reference_chain(stored_refs, stored_last).split() if token
                }
                if inbound and inbound in known:
                    # Remint of mail already in this thread: keep the latest id, including a
                    # progress Message-ID, so a later In-Reply-To still names this session.
                    last_id = stored_last or inbound
                    last_refs = stored_refs or references
                else:
                    last_id = inbound or stored_last
                    last_refs = reference_chain(stored_refs, stored_last, references, inbound)
                reply_address = reply_address or str(existing["reply_address"] or "")
                subject = subject or str(existing["subject"] or "")
            connection.execute(
                """
                INSERT INTO threads(
                    thread_key, session_id, last_message_id, last_references,
                    reply_address, subject, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_key) DO UPDATE SET
                    session_id = excluded.session_id,
                    last_message_id = excluded.last_message_id,
                    last_references = excluded.last_references,
                    reply_address = excluded.reply_address,
                    subject = excluded.subject,
                    updated_at = excluded.updated_at
                """,
                (thread_key, session_id, last_id, last_refs, reply_address, subject, now),
            )
            # Waiting jobs on this thread follow the live session, including a remint after the
            # previous worker disappeared. Finished rows keep the session that already mailed.
            connection.execute(
                f"""
                UPDATE items SET session_id = ?, updated_at = ?
                WHERE thread_key = ? AND state IN ({",".join("?" * 2)})
                """,
                (session_id, now, thread_key, DISCOVERED, SUBMITTED),
            )
            self._remember(connection, thread_key, inbound)

    def item_by_message_id(self, message_id: str) -> MailItem | None:
        message_id = canonical_message_id(message_id)
        if not message_id:
            return None
        row = self._connection.execute(
            "SELECT * FROM items WHERE message_id = ? ORDER BY updated_at DESC LIMIT 1",
            (message_id,),
        ).fetchone()
        return _row(row) if row is not None else None

    def item_by_uid(self, mailbox: str, uidvalidity: int, uid: int) -> MailItem | None:
        row = self._connection.execute(
            """
            SELECT * FROM items
            WHERE mailbox = ? AND uidvalidity = ? AND uid = ?
            """,
            (mailbox, uidvalidity, uid),
        ).fetchone()
        return _row(row) if row is not None else None

    def incomplete(self) -> list[MailItem]:
        rows = self._connection.execute(
            f"""
            SELECT * FROM items
            WHERE state IN ({",".join("?" * len(INCOMPLETE))})
            ORDER BY updated_at ASC
            """,
            INCOMPLETE,
        ).fetchall()
        return [_row(row) for row in rows]

    def put_discovered(
        self,
        *,
        mailbox: str,
        uidvalidity: int,
        uid: int,
        message_id: str,
        thread_key: str,
        sender: str,
        subject: str,
        references: str,
        in_reply_to: str,
        body: str,
        text_to_send: str,
        first: bool,
    ) -> MailItem:
        existing = (
            self.item_by_message_id(message_id)
            if message_id
            else self.item_by_uid(mailbox, uidvalidity, uid)
        )
        if existing is not None:
            if existing.state in {SUBMITTED, COMPLETED, POSTED, SEEN, SKIPPED}:
                if existing.uid != uid or existing.uidvalidity != uidvalidity:
                    return self.update(existing.id, uid=uid, uidvalidity=uidvalidity)
                return existing
            with self._txn() as connection:
                connection.execute(
                    """
                    UPDATE items SET
                        mailbox = ?, uidvalidity = ?, uid = ?, thread_key = ?,
                        sender = ?, subject = ?, references_header = ?, in_reply_to = ?,
                        body = ?, text_to_send = ?, first = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        mailbox,
                        uidvalidity,
                        uid,
                        thread_key,
                        sender,
                        subject,
                        references,
                        in_reply_to,
                        body,
                        text_to_send,
                        int(first),
                        _now(),
                        existing.id,
                    ),
                )
                self._remember(connection, thread_key, message_id)
            refreshed = self._connection.execute(
                "SELECT * FROM items WHERE id = ?", (existing.id,)
            ).fetchone()
            assert refreshed is not None
            return _row(refreshed)
        item_id = uuid.uuid4().hex
        client_message_id = f"mail-{item_id}"
        now = _now()
        try:
            with self._txn() as connection:
                connection.execute(
                    """
                    INSERT INTO items(
                        id, mailbox, uidvalidity, uid, message_id, thread_key, sender, subject,
                        references_header, in_reply_to, body, text_to_send, first, client_message_id,
                        session_id, turn_id, injected, reply_text, outbound_message_id, state,
                        skip_reason, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', 0, '', '', ?, '', ?)
                    """,
                    (
                        item_id,
                        mailbox,
                        uidvalidity,
                        uid,
                        message_id,
                        thread_key,
                        sender,
                        subject,
                        references,
                        in_reply_to,
                        body,
                        text_to_send,
                        int(first),
                        client_message_id,
                        DISCOVERED,
                        now,
                    ),
                )
                self._remember(connection, thread_key, message_id)
        except sqlite3.IntegrityError:
            raced = (
                self.item_by_message_id(message_id)
                if message_id
                else self.item_by_uid(mailbox, uidvalidity, uid)
            )
            if raced is not None:
                return raced
            raise
        created = self._connection.execute(
            "SELECT * FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        assert created is not None
        return _row(created)

    def mark_skipped(
        self, *, mailbox: str, uidvalidity: int, uid: int, message_id: str, reason: str
    ) -> MailItem:
        existing = (
            self.item_by_message_id(message_id)
            if message_id
            else self.item_by_uid(mailbox, uidvalidity, uid)
        )
        if existing is not None:
            return self.update(existing.id, state=SKIPPED, skip_reason=reason)
        item_id = uuid.uuid4().hex
        try:
            with self._txn() as connection:
                connection.execute(
                    """
                    INSERT INTO items(
                        id, mailbox, uidvalidity, uid, message_id, thread_key, sender, subject,
                        references_header, in_reply_to, body, text_to_send, first, client_message_id,
                        session_id, turn_id, injected, reply_text, outbound_message_id, state,
                        skip_reason, updated_at
                    ) VALUES (?, ?, ?, ?, ?, '', '', '', '', '', '', '', 0, '', '', '', 0, '', '', ?, ?, ?)
                    """,
                    (item_id, mailbox, uidvalidity, uid, message_id, SKIPPED, reason, _now()),
                )
        except sqlite3.IntegrityError:
            raced = (
                self.item_by_message_id(message_id)
                if message_id
                else self.item_by_uid(mailbox, uidvalidity, uid)
            )
            if raced is not None:
                return self.update(raced.id, state=SKIPPED, skip_reason=reason)
            raise
        created = self._connection.execute(
            "SELECT * FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        assert created is not None
        return _row(created)

    def update(self, item_id: str, **fields: object) -> MailItem:
        allowed = {
            "session_id",
            "turn_id",
            "injected",
            "reply_text",
            "outbound_message_id",
            "state",
            "skip_reason",
            "first",
            "text_to_send",
            "uid",
            "uidvalidity",
        }
        assignments = ["updated_at = ?"]
        values: list[object] = [_now()]
        for name, value in fields.items():
            if name not in allowed:
                raise ValueError(name)
            column = "injected" if name == "injected" else name
            if name == "injected" or name == "first":
                value = int(bool(value))
            assignments.append(f"{column} = ?")
            values.append(value)
        values.append(item_id)
        with self._txn() as connection:
            connection.execute(f"UPDATE items SET {', '.join(assignments)} WHERE id = ?", values)
        row = self._connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        assert row is not None
        return _row(row)

    def already_completed(self, mailbox: str, uidvalidity: int, uid: int, message_id: str) -> bool:
        item = self.item_by_message_id(message_id) if message_id else None
        if item is None:
            item = self.item_by_uid(mailbox, uidvalidity, uid)
        if item is None:
            return False
        return item.state in {POSTED, SEEN, SKIPPED}


def session_is_mailbox(session_id: str) -> bool:
    """Whether this daemon session is a mailbox thread. Does not create the job file."""
    if not session_id:
        return False
    path = mail_database_path()
    if not path.is_file():
        return False
    store = ThreadStore(path)
    try:
        return bool(store.thread_key_for_session(session_id))
    finally:
        store.close()
