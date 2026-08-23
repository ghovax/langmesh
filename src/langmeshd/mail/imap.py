"""The IMAP IDLE loop: wait for EXISTS, then fetch UNSEEN. No polling from inside a turn."""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from email import message_from_bytes
from email.message import Message
from email.policy import default
from typing import Optional

from aioimaplib import IMAP4, IMAP4_SSL, STOP_WAIT_SERVER_PUSH

from langmesh.base.primitives.errors import log_fields
from langmeshd.commons.configuration import EmailConfiguration
from langmeshd.mail.clock import ResumeClock, StaleConnection

logger = logging.getLogger(__name__)


def _client(configuration: EmailConfiguration) -> IMAP4:
    host = configuration.effective_imap_host
    port = configuration.effective_imap_port
    if configuration.effective_imap_ssl:
        return IMAP4_SSL(host=host, port=port, timeout=30)
    return IMAP4(host=host, port=port, timeout=30)


def _uidvalidity(response_lines: list) -> int:
    """UIDVALIDITY from a SELECT response, or 0 when the server omitted it."""
    for line in response_lines:
        text = line.decode() if isinstance(line, (bytes, bytearray)) else str(line)
        match = re.search(r"UIDVALIDITY\s+(\d+)", text, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return 0


def _uids(response_lines: list) -> list[int]:
    numbers: list[int] = []
    for line in response_lines:
        text = line.decode() if isinstance(line, (bytes, bytearray)) else str(line)
        if text.upper() in {"OK", "NO", "BAD", "SEARCH"}:
            continue
        for token in text.split():
            if token.isdigit():
                numbers.append(int(token))
    return numbers


def _fetch_payload(lines: list) -> bytes:
    for line in lines:
        if isinstance(line, (bytes, bytearray)) and b"\n" in line:
            return bytes(line)
    for line in lines:
        if isinstance(line, (bytes, bytearray)) and len(line) > 80:
            return bytes(line)
    return b""


def _push_has_exists(payload: object) -> bool:
    if payload == STOP_WAIT_SERVER_PUSH:
        return False
    chunks = payload if isinstance(payload, (list, tuple)) else [payload]
    for chunk in chunks:
        text = chunk.decode() if isinstance(chunk, (bytes, bytearray)) else str(chunk)
        if "EXISTS" in text.upper():
            return True
    return False


class Inbox:
    """One IMAP session: LOGIN, SELECT, IDLE, FETCH. Commands other than IDLE run only between IDLEs."""

    def __init__(
        self, configuration: EmailConfiguration, clock: Optional[ResumeClock] = None
    ) -> None:
        self.configuration = configuration
        self.clock = clock or ResumeClock()
        self.imap: Optional[IMAP4] = None
        self.uidvalidity = 0
        self._wake = asyncio.Event()

    async def connect(self) -> None:
        self.imap = _client(self.configuration)
        await self.imap.wait_hello_from_server()
        login = await self.imap.login(
            self.configuration.effective_imap_username,
            self.configuration.effective_imap_password,
        )
        if login.result != "OK":
            raise RuntimeError("IMAP login was refused.")
        selected = await self.imap.select(self.configuration.effective_imap_mailbox)
        if selected.result != "OK":
            raise RuntimeError("IMAP SELECT was refused.")
        self.uidvalidity = _uidvalidity(list(selected.lines))
        self._enable_keepalive()
        logger.info(
            "mail inbox selected",
            extra=log_fields(
                mailbox=self.configuration.effective_imap_mailbox,
                uidvalidity=self.uidvalidity,
            ),
        )

    def _enable_keepalive(self) -> None:
        """TCP keepalive so a paused container's dead IMAP socket fails instead of hanging forever."""
        if self.imap is None:
            return
        transport = getattr(getattr(self.imap, "protocol", None), "transport", None)
        sock = transport.get_extra_info("socket") if transport is not None else None
        if sock is None:
            return
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if hasattr(socket, "TCP_KEEPIDLE"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 20)
            if hasattr(socket, "TCP_KEEPINTVL"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
            if hasattr(socket, "TCP_KEEPCNT"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
            if hasattr(socket, "TCP_USER_TIMEOUT"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, 45_000)
        except OSError:
            logger.debug("mail could not enable TCP keepalive", exc_info=True)

    async def noop(self) -> None:
        """Prove the selected connection is still the same mailbox, after a pause or idle timeout."""
        assert self.imap is not None
        response = await self.imap.noop()
        if response.result != "OK":
            raise RuntimeError("IMAP NOOP failed.")

    async def close(self) -> None:
        if self.imap is None:
            return
        try:
            async with asyncio.timeout(5):
                try:
                    if self.imap.has_pending_idle():
                        self.imap.idle_done()
                except Exception:  # noqa: BLE001
                    logger.debug("mail idle_done failed during close", exc_info=True)
                try:
                    await self.imap.logout()
                except Exception:  # noqa: BLE001
                    logger.debug("mail IMAP logout failed", exc_info=True)
        except Exception:  # noqa: BLE001 — a hung logout after pause is dropped, not waited out
            protocol = getattr(self.imap, "protocol", None)
            transport = getattr(protocol, "transport", None)
            if transport is not None:
                transport.abort()
        self.imap = None

    async def unseen(self) -> list[int]:
        assert self.imap is not None
        response = await self.imap.uid_search("UNSEEN")
        if response.result != "OK":
            return []
        return _uids(list(response.lines))

    async def fetch(self, uid: int) -> Optional[Message]:
        assert self.imap is not None
        response = await self.imap.uid("fetch", str(uid), "(BODY.PEEK[])")
        if response.result != "OK":
            return None
        raw = _fetch_payload(list(response.lines))
        if not raw:
            return None
        parsed = message_from_bytes(raw, policy=default)
        return parsed

    async def mark_seen(self, uid: int) -> None:
        assert self.imap is not None
        await self.imap.uid("store", str(uid), "+FLAGS.SILENT", "(\\Seen)")

    def idling(self) -> bool:
        return bool(self.imap is not None and self.imap.has_pending_idle())

    def request_wake(self) -> None:
        """Leave IDLE so FETCH or STORE can run. Safe if we are not in IDLE."""
        self._wake.set()
        if self.imap is not None and self.imap.has_pending_idle():
            try:
                self.imap.idle_done()
            except Exception:  # noqa: BLE001
                logger.debug("mail idle_done from wake failed", exc_info=True)

    async def idle_until_exists(self) -> None:
        """Block in IDLE until EXISTS, a command wants the socket, or the RFC timeout.

        FETCH never runs from inside IDLE. Every two seconds we check whether the host slept.
        """
        assert self.imap is not None
        self._wake.clear()
        idle = await self.imap.idle_start(timeout=int(self.configuration.idle_timeout_seconds))
        try:
            while self.imap.has_pending_idle():
                if self._wake.is_set():
                    break
                try:
                    pushed = await self.imap.wait_server_push(timeout=2.0)
                except TimeoutError:
                    if self._wake.is_set():
                        break
                    if self.clock.jumped():
                        raise StaleConnection("the host slept or the clock jumped") from None
                    continue
                if pushed == STOP_WAIT_SERVER_PUSH or _push_has_exists(pushed):
                    break
        finally:
            if self.imap.has_pending_idle():
                self.imap.idle_done()
            try:
                await idle
            except Exception:  # noqa: BLE001
                logger.debug("mail IDLE task ended with error", exc_info=True)
