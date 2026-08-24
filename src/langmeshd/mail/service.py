"""The mail client loop: durable jobs, IDLE for discovery, resume after pause, reboot, or a killed process.

IMAP UNSEEN finds new mail. SQLite is what must finish. IDLE keeps running while a turn is
in flight, so a follow-up can be steered; FETCH never runs from inside IDLE. Each turn is
JSON with subject and message; quoted history is stripped. The session mails progress and
the reply through `submit_email`. Sockets that survived a suspend or a container pause are
dropped and rebuilt.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from email.message import Message
from typing import Any, Optional

from langmesh.base.primitives.errors import log_fields
from langmeshd.commons.configuration import EmailConfiguration
from langmeshd.mail import body
from langmeshd.mail import client as daemon_client
from langmeshd.mail import payload
from langmeshd.mail.clock import ResumeClock, StaleConnection
from langmeshd.mail.imap import Inbox
from langmeshd.mail.smtp import outbound_message_id, probe_smtp, reply_message, send_reply
from langmeshd.mail.threads import (
    COMPLETED,
    DISCOVERED,
    POSTED,
    SEEN,
    SKIPPED,
    SUBMITTED,
    MailItem,
    ThreadStore,
)
from langmeshd.mail.transcript import (
    turn_failed,
    turn_for_client_message,
)

logger = logging.getLogger(__name__)


def _thread_key(mailbox: str, message: Message) -> str:
    root = body.thread_root_id(message)
    identity = root or body.durable_identity(message)
    return f"email:{mailbox}:{identity}"


def _reply_subject(item: MailItem) -> str:
    return item.subject or "LangMesh"


def _references(message: Message) -> str:
    existing = " ".join(body.referenced_ids(message))
    own = body.message_id_of(message)
    return " ".join(part for part in (existing, own) if part).strip()


class MailService:
    """One mailbox in front of one daemon, with every in-flight mail checkpointed."""

    def __init__(
        self, configuration: EmailConfiguration, store: ThreadStore, clock: ResumeClock
    ) -> None:
        self.configuration = configuration
        self.store = store
        self.clock = clock
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._imap_commands = asyncio.Lock()
        self._work: set[asyncio.Task[None]] = set()
        self._stale = asyncio.Event()
        self._inflight: set[str] = set()

    def _spawn_item(self, http, item: MailItem, inbox: Inbox | None) -> None:
        if item.id in self._inflight:
            return
        self._inflight.add(item.id)

        async def run() -> None:
            try:
                await self._finish_one(http, item, inbox)
            finally:
                self._inflight.discard(item.id)

        self._spawn(run())

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._work.add(task)
        task.add_done_callback(self._work.discard)

    async def cancel_work(self) -> None:
        for task in list(self._work):
            task.cancel()
        if self._work:
            await asyncio.gather(*self._work, return_exceptions=True)
        self._inflight.clear()

    async def _imap(self, inbox: Inbox, awaitable: Any) -> Any:
        """Run an IMAP command only while we are not in IDLE."""
        inbox.request_wake()
        async with self._imap_commands:
            while inbox.idling():
                self._guard()
                await asyncio.sleep(0.05)
            return await self.clock.await_fresh(awaitable)

    def _lock_for(self, thread_key: str) -> asyncio.Lock:
        lock = self._session_locks.get(thread_key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[thread_key] = lock
        return lock

    def _guard(self) -> None:
        if self.clock.jumped():
            raise StaleConnection("the host slept or the clock jumped")

    async def _rpc(self, http, method: str, params: dict) -> dict:
        self._guard()
        return await self.clock.await_fresh(daemon_client.rpc(http, method, params))

    async def _session_status(self, http, session_id: str) -> str:
        """live, ended, or missing. Connection failures propagate so we reconnect instead of minting a twin."""
        try:
            result = await self._rpc(http, "session.get", {"id": session_id})
        except daemon_client.MailDaemonError as error:
            if error.code == "no_such_session":
                return "missing"
            raise
        session = result.get("session")
        record = session if isinstance(session, dict) else {}
        if str(record.get("lifecycle") or "") == "ended":
            return "ended"
        return "live"

    async def _session_working(self, http, session_id: str) -> bool:
        result = await self._rpc(http, "session.get", {"id": session_id})
        session = result.get("session")
        record = session if isinstance(session, dict) else {}
        return str(record.get("activity") or "") == "working"

    async def _ensure_session(self, http, thread_key: str, title: str) -> tuple[str, bool]:
        mapped = self.store.session_id(thread_key)
        if mapped:
            status = await self._session_status(http, mapped)
            if status == "live":
                return mapped, False
            if status not in {"missing", "ended"}:
                raise daemon_client.MailDaemonError(f"session {mapped} is not usable ({status}).")
        created = await self._rpc(
            http,
            "session.create",
            {
                "agent": self.configuration.effective_agent,
                "working_directory": self.configuration.effective_working_directory,
                "permission_mode": self.configuration.permission_mode,
                "title": title,
            },
        )
        session_id = str(created.get("id") or "")
        if not session_id:
            raise daemon_client.MailDaemonError("session.create returned no id.")
        return session_id, True

    async def _history(self, http, session_id: str) -> list[dict[str, Any]]:
        result = await self._rpc(http, "session.history", {"id": session_id})
        turns = result.get("turns")
        return [turn for turn in turns if isinstance(turn, dict)] if isinstance(turns, list) else []

    async def _wait_until_idle(self, http, item: MailItem) -> None:
        """Attach until the session is idle. Re-attach if the stream dies. Does not harvest prose."""
        deadline = asyncio.get_running_loop().time() + self.configuration.turn_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            self._guard()
            working = await self._session_working(http, item.session_id)
            turns = await self._history(http, item.session_id)
            matched = turn_for_client_message(turns, item.client_message_id)
            if matched is not None and turn_failed(matched) and not working:
                retried = await self._rpc(http, "session.retry", {"id": item.session_id})
                if retried.get("retried") is not True:
                    logger.warning(
                        "mail turn retry refused",
                        extra=log_fields(session=item.session_id, turn=item.turn_id),
                    )
                    await asyncio.sleep(1)
                continue
            if not working:
                return
            remaining = min(30.0, deadline - asyncio.get_running_loop().time())
            if remaining <= 0:
                break
            try:
                await self._attach_until_idle(http, item.session_id, remaining)
            except StaleConnection:
                raise
            except Exception:  # noqa: BLE001 — a dead attach is resumed, not a lost turn
                logger.warning(
                    "mail attach dropped; will resume",
                    extra=log_fields(session=item.session_id),
                    exc_info=True,
                )
                await asyncio.sleep(1)
        raise daemon_client.MailDaemonError(
            "The session did not finish before the mail wait elapsed."
        )

    async def _attach_until_idle(self, http, session_id: str, timeout: float) -> None:
        response = await self.clock.await_fresh(daemon_client.attach(http, session_id))
        incoming: asyncio.Queue[dict | None] = asyncio.Queue()

        async def pump() -> None:
            try:
                async for frame in daemon_client.iter_attach_frames(response):
                    await incoming.put(frame)
            finally:
                await incoming.put(None)
                await response.aclose()

        pump_task = asyncio.create_task(pump())
        seen_running = False
        try:
            end = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = end - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return
                try:
                    frame = await asyncio.wait_for(incoming.get(), timeout=min(2.0, remaining))
                except asyncio.TimeoutError:
                    self._guard()
                    continue
                if frame is None:
                    return
                if frame.get("kind") == "ready":
                    continue
                state = daemon_client.consume_frame(frame, [])
                if state is True:
                    seen_running = True
                if state is False and seen_running:
                    return
        finally:
            if not pump_task.done():
                pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump_task

    async def _submit(self, http, item: MailItem) -> MailItem:
        session_id, first = await self._ensure_session(http, item.thread_key, _reply_subject(item))
        self.store.bind(
            item.thread_key,
            session_id,
            message_id=item.message_id,
            references=item.references,
            reply_address=item.sender,
            subject=item.subject,
        )
        if first and not item.first:
            text = payload.turn_text(subject=item.subject, message=item.body, first=True)
            item = self.store.update(item.id, first=True, text_to_send=text, session_id=session_id)
        else:
            item = self.store.update(item.id, session_id=session_id)
        outcome = await self._rpc(
            http,
            "session.send",
            {
                "id": session_id,
                "parts": [{"kind": "text", "text": item.text_to_send}],
                "metadata": {"messageId": item.client_message_id},
            },
        )
        if outcome.get("accepted") is False:
            if outcome.get("compaction_required"):
                notice = "The session cannot take mail until its last compaction is retried from the app."
            elif outcome.get("awaiting_input"):
                notice = (
                    "The session is waiting on a decision in the app, so this mail was not taken."
                )
            else:
                notice = "The session did not accept this mail."
            # Stay discovered: a refusal is not a finished job, and must not set \Seen.
            raise daemon_client.MailDaemonError(notice)
        turn_id = str(outcome.get("turn_id") or item.turn_id)
        return self.store.update(
            item.id,
            state=SUBMITTED,
            session_id=session_id,
            turn_id=turn_id,
            injected=bool(outcome.get("injected")),
        )

    async def _resume_submitted(self, http, item: MailItem) -> MailItem:
        """Send again if the daemon lost this turn. The same messageId is idempotent."""
        if not item.session_id:
            return await self._submit(http, item)
        status = await self._session_status(http, item.session_id)
        if status in {"missing", "ended"}:
            return await self._submit(http, item)
        if await self._session_working(http, item.session_id):
            return item
        turns = await self._history(http, item.session_id)
        matched = turn_for_client_message(turns, item.client_message_id)
        if matched is not None and turn_failed(matched):
            retried = await self._rpc(http, "session.retry", {"id": item.session_id})
            if retried.get("retried") is True:
                return item
            return await self._submit(http, item)
        if matched is None:
            return await self._submit(http, item)
        return item

    async def _post(self, item: MailItem) -> MailItem:
        outbound_id = item.outbound_message_id or outbound_message_id()
        if not item.outbound_message_id:
            item = self.store.update(item.id, outbound_message_id=outbound_id)
        outbound = reply_message(
            configuration=self.configuration,
            to_address=item.sender,
            subject=_reply_subject(item),
            body=item.reply_text,
            in_reply_to=item.message_id or item.in_reply_to,
            references=item.references,
            message_id=outbound_id,
        )
        await self.clock.await_fresh(send_reply(self.configuration, outbound))
        return self.store.update(item.id, state=POSTED, outbound_message_id=outbound_id)

    async def _mark_seen(self, inbox: Inbox | None, item: MailItem) -> MailItem:
        if inbox is None or not item.uid or item.uidvalidity != inbox.uidvalidity:
            return item
        try:
            await self._imap(inbox, inbox.mark_seen(item.uid))
        except StaleConnection:
            raise
        except Exception:  # noqa: BLE001 — Seen is a flag; the job is already posted
            logger.warning("mail could not set Seen", extra=log_fields(uid=item.uid), exc_info=True)
            return item
        return self.store.update(item.id, state=SEEN)

    async def finish(self, http, item: MailItem, inbox: Inbox | None) -> None:
        current = self.store.get(item.id) or item
        if current.state in {SEEN, SKIPPED}:
            return
        async with self._lock_for(item.thread_key or item.id):
            current = self.store.get(current.id) or current
            if current.state == DISCOVERED:
                current = await self._submit(http, current)
        # Released so a later mail on this thread can steer a live turn.
        if current.state == SUBMITTED:
            await self._wait_until_idle(http, current)
            current = self.store.get(current.id) or current
        if current.state == SUBMITTED:
            async with self._lock_for(item.thread_key or item.id):
                current = self.store.get(current.id) or current
                if current.state == SUBMITTED:
                    current = await self._resume_submitted(http, current)
            if current.state == SUBMITTED:
                await self._wait_until_idle(http, current)
                current = self.store.get(current.id) or current
        if current.state == COMPLETED:
            current = await self._post(current)
        if current.state == POSTED:
            current = await self._mark_seen(inbox, current)
        logger.info(
            "mail item advanced",
            extra=log_fields(item=current.id, state=current.state, session=current.session_id),
        )

    async def ingest(self, uidvalidity: int, uid: int, message: Message) -> MailItem | None:
        mailbox = self.configuration.effective_imap_mailbox
        message_id = body.durable_identity(message)
        if self.store.already_finished(mailbox, uidvalidity, uid, message_id):
            existing = self.store.item_by_message_id(message_id) or self.store.item_by_uid(
                mailbox, uidvalidity, uid
            )
            if existing is not None and (
                existing.uid != uid or existing.uidvalidity != uidvalidity
            ):
                return self.store.update(existing.id, uid=uid, uidvalidity=uidvalidity)
            return existing
        sender = body.sender_address(message)
        mailbox_address = self.configuration.effective_address.lower()
        if sender == mailbox_address or (
            sender
            and mailbox_address
            and body.canonical_mailbox(sender) == body.canonical_mailbox(mailbox_address)
        ):
            self.store.mark_skipped(
                mailbox=mailbox,
                uidvalidity=uidvalidity,
                uid=uid,
                message_id=message_id,
                reason="own",
            )
            return None
        if body.is_automatic(message):
            self.store.mark_skipped(
                mailbox=mailbox,
                uidvalidity=uidvalidity,
                uid=uid,
                message_id=message_id,
                reason="automatic",
            )
            return None
        if not body.sender_allowed(sender, self.configuration.effective_allow_from):
            self.store.mark_skipped(
                mailbox=mailbox,
                uidvalidity=uidvalidity,
                uid=uid,
                message_id=message_id,
                reason="not-allowlisted",
            )
            return None
        prose = body.turn_body(message)
        if not prose:
            self.store.mark_skipped(
                mailbox=mailbox,
                uidvalidity=uidvalidity,
                uid=uid,
                message_id=message_id,
                reason="empty",
            )
            return None
        computed = _thread_key(mailbox, message)
        in_reply = body.in_reply_to_ids(message)
        known = set(in_reply)
        identifiers = in_reply + [
            identifier for identifier in body.referenced_ids(message) if identifier not in known
        ]
        thread_key = self.store.resolve_thread_key(identifiers, computed)
        first = not bool(self.store.session_id(thread_key))
        subject = body.subject_of(message) or "LangMesh"
        return self.store.put_discovered(
            mailbox=mailbox,
            uidvalidity=uidvalidity,
            uid=uid,
            message_id=message_id,
            thread_key=thread_key,
            sender=body.reply_address(message) or sender,
            subject=subject,
            references=_references(message),
            in_reply_to=body.message_id_of(message) or message_id,
            body=prose,
            text_to_send=payload.turn_text(subject=subject, message=prose, first=first),
            first=first,
        )

    async def drain(self, http, inbox: Inbox) -> None:
        """Discover UNSEEN mail and start jobs. Does not wait for turns, so IDLE can keep running."""
        await self.resume(http, inbox)
        for uid in await self._imap(inbox, inbox.unseen()):
            self._guard()
            existing = self.store.item_by_uid(
                inbox.configuration.effective_imap_mailbox, inbox.uidvalidity, uid
            )
            if existing is not None and existing.state in {POSTED, SEEN, SKIPPED}:
                if existing.state == POSTED:
                    self._spawn_item(http, existing, inbox)
                elif existing.state == SKIPPED:
                    with contextlib.suppress(Exception):
                        await self._imap(inbox, inbox.mark_seen(uid))
                continue
            message = await self._imap(inbox, inbox.fetch(uid))
            if message is None:
                continue
            try:
                item = await self.ingest(inbox.uidvalidity, uid, message)
                if item is None or item.state == SKIPPED:
                    with contextlib.suppress(Exception):
                        await self._imap(inbox, inbox.mark_seen(uid))
                    continue
                if item.state in {DISCOVERED, SUBMITTED, COMPLETED, POSTED}:
                    self._spawn_item(http, item, inbox)
            except StaleConnection:
                raise
            except Exception as error:  # noqa: BLE001 — one bad mail must not stop IDLE
                logger.warning(
                    "mail message failed",
                    extra=log_fields(error, uid=uid),
                    exc_info=True,
                )

    async def _finish_one(self, http, item: MailItem, inbox: Inbox | None) -> None:
        try:
            await self.finish(http, item, inbox)
        except StaleConnection:
            self._stale.set()
            if inbox is not None:
                inbox.request_wake()
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "mail job failed",
                extra=log_fields(error, item=item.id, state=item.state),
                exc_info=True,
            )

    async def resume(self, http, inbox: Inbox | None) -> None:
        for item in self.store.incomplete():
            self._guard()
            self._spawn_item(http, item, inbox)


def load_email_configuration() -> EmailConfiguration:
    from langmeshd.commons.configuration_file import load as load_document

    document = load_document() or {}
    return EmailConfiguration.model_validate(document.get("email") or {})


def _agent_credential_problem(configuration: EmailConfiguration) -> str:
    """Why this agent cannot take a turn, or empty when the profile and its key are present.

    IMAP can IDLE without a model key; the turn then 401s until turn_timeout_seconds.
    Fail here so a missing OPENCODE_API_KEY shows up in the mail log, like a missing
    mailbox password, instead of after someone has already emailed.
    """
    name = configuration.effective_agent
    try:
        from langmeshd.commons.agent_files import load_agent_configuration
        from langmeshd.commons.configuration_locations import agent_directories

        agent = load_agent_configuration(
            name, agent_directories(configuration.effective_working_directory)
        )
    except FileNotFoundError:
        return (
            f"email.agent {name!r} is not an available profile "
            "(set LANGMESH_MAIL_AGENT, or install the bundled reviewer)."
        )
    provider = (agent.provider or "").strip()
    if not provider:
        return ""
    identifier = agent.model_identifier
    if identifier:
        from langmesh.base.content.models import find_model

        find_model(identifier)
    from langmesh.base.identity.providers import (
        get_provider_definition,
        provider_env_vars,
        resolve_api_key,
    )

    definition = get_provider_definition(provider) or get_provider_definition(provider.lower())
    if definition is not None and definition.native:
        return ""
    if (os.environ.get("LANGMESH_API_KEY") or "").strip():
        return ""
    configured: dict[str, str] = {}
    try:
        from langmeshd.commons.configuration_io import load_configuration

        configured = load_configuration(seed=False).configured_provider_keys()
    except Exception:  # noqa: BLE001 — an unreadable file still allows env-only keys
        configured = {}
    key = resolve_api_key(provider, configured)
    anonymous = (definition.anonymous_api_key if definition is not None else "") or ""
    if key.strip() and key.strip() != anonymous:
        return ""
    names = list(provider_env_vars(provider))
    if provider.lower().startswith("opencode") and "OPENCODE_API_KEY" not in names:
        names.insert(0, "OPENCODE_API_KEY")
    if any((os.environ.get(name) or "").strip() for name in names):
        return ""
    shown = ", ".join(names) if names else "a provider API key"
    return f"the {name} profile needs {shown} (or LANGMESH_API_KEY / providers.{provider}.api_key)."


def validate_ready(configuration: EmailConfiguration) -> str:
    """Why this configuration cannot run, or an empty string when it can."""
    if not configuration.effective_enabled:
        return "email.enabled is false (set email.enabled or LANGMESH_MAIL_ADDRESS)."
    if not configuration.effective_address:
        return "email.address (or LANGMESH_MAIL_ADDRESS) is required."
    if not configuration.effective_allow_from:
        return "email.allow_from (or LANGMESH_MAIL_ALLOW_FROM) is required."
    if not configuration.effective_agent:
        return "email.agent (or LANGMESH_MAIL_AGENT) is required."
    if not configuration.effective_imap_host:
        return "email.imap.host (or LANGMESH_MAIL_IMAP_HOST) is required."
    if not configuration.effective_imap_username:
        return "email.imap.username (or LANGMESH_MAIL_IMAP_USER) is required."
    if not configuration.effective_imap_password:
        return "email.imap.password (or LANGMESH_MAIL_IMAP_PASSWORD) is required."
    if not configuration.effective_smtp_host:
        return "email.smtp.host (or LANGMESH_MAIL_SMTP_HOST) is required."
    return _agent_credential_problem(configuration)


async def run(configuration: Optional[EmailConfiguration] = None) -> int:
    """Connect to the daemon, resume unfinished mail, then IDLE until cancelled.

    A missing mailbox config is waited out rather than exiting, so a systemd unit
    that started before mail.env was filled comes up on its own.
    """
    store = ThreadStore()
    clock = ResumeClock()
    delay = 1.0
    supplied = configuration
    try:
        while True:
            current = supplied or load_email_configuration()
            problem = validate_ready(current)
            if problem:
                logger.error("mail not configured: %s; waiting", problem)
                if supplied is not None:
                    return 1
                await asyncio.sleep(5)
                continue
            service = MailService(current, store, clock)
            try:
                async with daemon_client.connect() as http:
                    if not await daemon_client.health(http):
                        raise StaleConnection("the daemon is not accepting connections")
                    await service.resume(http, None)
                    inbox = Inbox(current, clock=clock)
                    try:
                        await clock.await_fresh(inbox.connect())
                        clock.note()
                        await clock.await_fresh(probe_smtp(current))
                        logger.info(
                            "mail ready",
                            extra=log_fields(
                                address=current.effective_address,
                                allow_from=",".join(current.effective_allow_from),
                                agent=current.effective_agent,
                                imap=current.effective_imap_host,
                                smtp=current.effective_smtp_host,
                            ),
                        )
                        await service.drain(http, inbox)
                        while True:
                            if service._stale.is_set():
                                raise StaleConnection("the host slept or the clock jumped")
                            await inbox.idle_until_exists()
                            if service._stale.is_set() or clock.jumped():
                                raise StaleConnection("the host slept or the clock jumped")
                            await service._imap(inbox, inbox.noop())
                            await service.drain(http, inbox)
                    finally:
                        await service.cancel_work()
                        await inbox.close()
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except StaleConnection:
                logger.warning("mail dropped stale sockets after suspend or pause")
                await asyncio.sleep(1)
                delay = 1.0
            except Exception:  # noqa: BLE001 — reconnect rather than exit on a dropped socket
                logger.exception("mail session failed; reconnecting")
                await asyncio.sleep(delay)
                delay = min(30.0, delay * 2)
    finally:
        store.close()
    return 0
