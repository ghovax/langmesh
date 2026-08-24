"""The IMAP IDLE loop: wait for EXISTS or RECENT, then fetch UNSEEN. No polling from inside a turn."""

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


_FETCH_LITERAL = re.compile(
    rb"(?:BODY(?:\.PEEK)?\[\]|RFC822(?:\.PEEK)?)\s*\{(\d+)\}", re.IGNORECASE
)


def _as_text(line: object) -> str:
    if isinstance(line, (bytes, bytearray)):
        return bytes(line).decode("utf-8", errors="replace")
    return str(line)


def imap_reason(response: object, secret: str = "") -> str:
    """The server's reason for a failed IMAP command, with any copied secret removed."""
    lines = getattr(response, "lines", None) or []
    parts: list[str] = []
    for line in lines:
        text = " ".join(_as_text(line).split())
        if secret and secret in text:
            text = text.replace(secret, "********")
        if text and text.upper() not in {"OK", "NO", "BAD", "SEARCH"}:
            parts.append(text)
    if parts:
        return "; ".join(parts)
    result = str(getattr(response, "result", "") or "").strip()
    return result or "refused"


def _as_bytes(line: object) -> bytes:
    if isinstance(line, (bytes, bytearray)):
        return bytes(line)
    return str(line).encode("utf-8", errors="replace")


def _looks_like_rfc822(payload: bytes) -> bool:
    if not payload or payload.startswith(b"(") or payload.startswith(b")"):
        return False
    lowered = payload[:32].lower()
    if lowered.startswith(b"from:") or lowered.startswith(b"return-path:"):
        return True
    return b"\r\n\r\n" in payload or b"\n\n" in payload


def _fetch_payload(lines: list) -> bytes:
    """The RFC822 body from UID FETCH. aioimaplib splits `{size}` literals onto their own line."""
    chunks = [_as_bytes(line) for line in lines]
    for index, chunk in enumerate(chunks):
        match = _FETCH_LITERAL.search(chunk)
        if match:
            size = int(match.group(1))
            start = match.end()
            if chunk[start : start + 2] == b"\r\n":
                start += 2
            elif chunk[start : start + 1] == b"\n":
                start += 1
            if start < len(chunk):
                body = chunk[start : start + size] if size else chunk[start:]
                if body:
                    return body
            if index + 1 < len(chunks):
                nxt = chunks[index + 1]
                return nxt[:size] if size and size <= len(nxt) else nxt
    for chunk in chunks:
        if _looks_like_rfc822(chunk):
            return chunk
    return b""


def _push_has_new_mail(payload: object) -> bool:
    """Whether an IDLE push is new mail: EXISTS, or RECENT when the server omitted EXISTS."""
    if payload == STOP_WAIT_SERVER_PUSH:
        return False
    chunks = payload if isinstance(payload, (list, tuple)) else [payload]
    for chunk in chunks:
        text = chunk.decode() if isinstance(chunk, (bytes, bytearray)) else str(chunk)
        upper = text.upper()
        if "EXISTS" in upper or "RECENT" in upper:
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
            raise RuntimeError(imap_reason(login, self.configuration.effective_imap_password))
        selected = await self.imap.select(self.configuration.effective_imap_mailbox)
        if selected.result != "OK":
            raise RuntimeError(imap_reason(selected, self.configuration.effective_imap_password))
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
            raise RuntimeError(imap_reason(response, self.configuration.effective_imap_password))
        return _uids(list(response.lines))

    async def fetch(self, uid: int) -> Optional[Message]:
        assert self.imap is not None
        # BODY.PEEK[] does not set \Seen. RFC822.PEEK is the same payload on older servers.
        for spec in ("(BODY.PEEK[])", "(RFC822.PEEK)"):
            response = await self.imap.uid("fetch", str(uid), spec)
            if response.result != "OK":
                continue
            raw = _fetch_payload(list(response.lines))
            if raw:
                return message_from_bytes(raw, policy=default)
        return None

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
        """Block in IDLE until new mail, a command wants the socket, or the RFC timeout.

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
                if pushed == STOP_WAIT_SERVER_PUSH or _push_has_new_mail(pushed):
                    break
        finally:
            if self.imap.has_pending_idle():
                self.imap.idle_done()
            try:
                await idle
            except Exception:  # noqa: BLE001
                logger.debug("mail IDLE task ended with error", exc_info=True)
