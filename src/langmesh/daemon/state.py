"""The daemon's process-wide singletons, and the one place a session's verb is called."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any


logger = logging.getLogger(__name__)


@dataclass(eq=False)
class SessionSubscription:
    """One bounded tail. Overflow closes it explicitly so the client can rebuild from snapshot + replay."""

    queue: asyncio.Queue
    overflowed: bool = False


@dataclass(frozen=True)
class SessionSnapshot:
    """One atomic cut between durable history and the live tail."""

    events: tuple[dict, ...]
    turn_id: str
    sequence: int
    version: int
    running: bool


@dataclass
class _TurnTail:
    """One open turn's live suffix: its events, and whether it is still running or durably persisted."""

    turn_id: str
    events: list[dict] = field(default_factory=list)
    durable: bool = False
    running: bool = True


class SessionEventBus:
    """Sequence, replay and bounded fan-out for the latency-critical live session stream."""

    _OVERFLOW = {"kind": "resync"}

    def __init__(self, *, subscriber_capacity: int = 1024) -> None:
        self._subscriber_capacity = subscriber_capacity
        self._subscribers: dict[str, set[SessionSubscription]] = {}
        self._sequences: dict[str, int] = {}
        # One open turn's live suffix per session; retired once it is durable and not running.
        self._tails: dict[str, _TurnTail] = {}
        self._activity: set[str] = set()
        self._block_cursors: dict[str, dict[tuple[str, str], int]] = {}
        # Unlike the wire sequence, this also changes for replay/durability transitions.
        self._versions: dict[str, int] = {}

    def subscribe(self, session_id: str) -> SessionSubscription:
        subscription = SessionSubscription(asyncio.Queue(maxsize=self._subscriber_capacity))
        self._subscribers.setdefault(session_id, set()).add(subscription)
        return subscription

    def snapshot(self, session_id: str) -> SessionSnapshot:
        """Atomically describe the live suffix through one sequence watermark."""
        tail = self._tails.get(session_id)
        return SessionSnapshot(
            events=tuple(
                {**event, **({"chunks": list(event["chunks"])} if "chunks" in event else {})}
                for event in (tail.events if tail is not None else ())
            ),
            turn_id=tail.turn_id if tail is not None else "",
            sequence=self._sequences.get(session_id, 0),
            version=self._versions.get(session_id, 0),
            running=session_id in self._activity,
        )

    def unsubscribe(self, session_id: str, subscription: SessionSubscription) -> None:
        subscriptions = self._subscribers.get(session_id)
        if not subscriptions:
            return
        subscriptions.discard(subscription)
        if not subscriptions:
            self._subscribers.pop(session_id, None)

    def _next(self, session_id: str) -> int:
        sequence = self._sequences.get(session_id, 0) + 1
        self._sequences[session_id] = sequence
        return sequence

    def _changed(self, session_id: str) -> None:
        self._versions[session_id] = self._versions.get(session_id, 0) + 1

    def _retire(self, session_id: str) -> None:
        """Drop a closed turn's suffix once durable history owns it, so a new subscriber starts from history."""
        self._tails.pop(session_id, None)
        self._block_cursors.pop(session_id, None)
        self._changed(session_id)

    def _remember(self, session_id: str, event: dict) -> None:
        """Record a live event into the open turn's suffix, coalescing adjacent deltas."""
        tail = self._tails.get(session_id)
        if tail is None or not tail.running or event.get("kind") == "turn":
            return
        if event.get("kind") == "delta" and tail.events:
            previous = tail.events[-1]
            if (
                previous.get("kind") == "delta"
                and previous.get("channel") == event.get("channel")
                and previous.get("block_id") == event.get("block_id")
            ):
                # Internal replay records never enter a subscriber queue; snapshots copy them. Mutating this private list keeps a long response O(n), rather than repeatedly copying its entire prefix and turning token streaming into O(n²) work.
                previous["chunks"].extend(event.get("chunks", ()))
                previous["seq"] = event["seq"]
                return
        tail.events.append({**event, **({"chunks": list(event["chunks"])} if "chunks" in event else {})})

    def _fan_out(self, session_id: str, event: dict) -> None:
        for subscription in tuple(self._subscribers.get(session_id, ())):
            if subscription.overflowed:
                continue
            try:
                subscription.queue.put_nowait(event)
            except asyncio.QueueFull:
                # A slow consumer cannot grow the daemon or receive a subtly incomplete transcript.
                subscription.overflowed = True
                while not subscription.queue.empty():
                    subscription.queue.get_nowait()
                subscription.queue.put_nowait(self._OVERFLOW)

    def publish_part(self, session_id: str, part: dict) -> None:
        event = {"kind": "live", "seq": self._next(session_id), "part": part}
        self._remember(session_id, event)
        self._fan_out(session_id, event)

    def publish_delta(self, session_id: str, channel: str, block_id: str, text: str) -> None:
        if not text:
            return
        event = {
            "kind": "delta",
            "seq": self._next(session_id),
            "channel": channel,
            "block_id": block_id,
            "turn_id": (tail.turn_id if (tail := self._tails.get(session_id)) is not None else ""),
            "cursor": self._block_cursors.setdefault(session_id, {}).get((channel, block_id), 0),
            "chunks": [text],
        }
        self._block_cursors[session_id][(channel, block_id)] = event["cursor"] + 1
        self._remember(session_id, event)
        self._fan_out(session_id, event)

    def begin_turn(self, session_id: str, turn_id: str) -> None:
        """Open one actual serialized turn, replacing the completed turn's replay."""
        self._tails[session_id] = _TurnTail(turn_id)
        self._block_cursors[session_id] = {}
        self._changed(session_id)

    def end_turn(self, session_id: str, turn_id: str) -> None:
        """Close one actual turn while retaining its compact replay until durability catches up."""
        tail = self._tails.get(session_id)
        if tail is None or tail.turn_id != turn_id:
            return
        tail.running = False
        self._changed(session_id)
        if tail.durable:
            self._retire(session_id)

    def commit_turn(self, session_id: str, turn_id: str) -> None:
        """Retire replay only after the exact turn is terminally durable."""
        tail = self._tails.get(session_id)
        if tail is None or tail.turn_id != turn_id:
            return
        tail.durable = True
        self._changed(session_id)
        if not tail.running:
            self._retire(session_id)

    def publish_activity(self, session_id: str, running: bool) -> None:
        """Publish aggregate busy/idle state independently of the serialized turn replay."""
        if running:
            self._activity.add(session_id)
        else:
            self._activity.discard(session_id)
        self._changed(session_id)
        self._fan_out(
            session_id,
            {"kind": "turn", "seq": self._next(session_id), "running": running},
        )

    def complete(self, session_id: str) -> None:
        """Close every watcher's stream — the session has ended."""
        self._tails.pop(session_id, None)
        self._activity.discard(session_id)
        self._block_cursors.pop(session_id, None)
        self._sequences.pop(session_id, None)
        self._versions.pop(session_id, None)
        for subscription in tuple(self._subscribers.get(session_id, ())):
            if subscription.overflowed:
                continue
            try:
                subscription.queue.put_nowait(None)
            except asyncio.QueueFull:
                subscription.overflowed = True
                while not subscription.queue.empty():
                    subscription.queue.get_nowait()
                subscription.queue.put_nowait(self._OVERFLOW)

    def complete_all(self) -> None:
        """Close every watcher of every session, since the daemon cannot finish shutting down while a stream is open."""
        for session_id in list(self._subscribers):
            self.complete(session_id)


# The supervision singletons: what a session's existence depends on, as distinct from what the browser surface needs.

registry: Any = None  # SessionRegistry
host: Any = None  # SessionHost
lifecycle: Any = None  # SessionLifecycle

event_bus = SessionEventBus()

# Re-exported from the workspace layer, because they are read here constantly.


def __getattr__(name: str) -> Any:
    """Read a workspace singleton through this module, resolved lazily because they are set after this one is imported."""
    from langmesh.commons import state as commons_state

    if hasattr(commons_state, name):
        return getattr(commons_state, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# The running and awaiting sets live on the workspace module and reach this one through the lookup above.
_title_tasks: set = set()
# Long-lived tasks the daemon owns, held so teardown can cancel them.
_watchers: list = []
_mcp_start_task = None
_remote_start_task = None
# The HTTP client the push sender borrows, closed with everything else on shutdown.
_push_client = None

# The daemon's own addresses and the token that guards them, written where a client can find them.
daemon_socket: str = ""
daemon_token: str = ""


async def reset_live_session_runtimes() -> None:
    """Tell the sessions being hosted to rebuild their runtime; a record with no executor has none to drop."""
    if host is None:
        return
    await asyncio.gather(
        *(dispatch_to_session(session_id, "session/reset", {}) for session_id in host.hosted()),
        return_exceptions=True,
    )


async def refresh_workspace_locations(workspace_id: str) -> None:
    """Tell every live session in a workspace that its locations have changed."""
    if registry is None or not workspace_id:
        return
    from langmesh.commons.services.locations import _resolve_session_locations

    live = [
        record
        for record in registry.live()
        if record.workspace_id == workspace_id and host is not None and host.hosts(record.id)
    ]
    if not live:
        return

    async def push(record) -> None:
        locations = await asyncio.to_thread(_resolve_session_locations, record.id)
        await relay_to_session(record, "session/locations", {"locations": locations})

    await asyncio.gather(*(push(record) for record in live), return_exceptions=True)


async def wake_then_relay(record, method: str, params: dict) -> dict:
    """Forward a command to a session, building its executor first if this daemon is not hosting it yet."""
    if host is not None and not host.hosts(record.id):
        await _wake(record)
    return await dispatch_to_session(record.id, method, params)


_wake_locks: dict[str, asyncio.Lock] = {}


async def _wake(record) -> None:
    """Give a session an executor again, from the record that already holds everything the build needs."""
    lock = _wake_locks.setdefault(record.id, asyncio.Lock())
    async with lock:
        # Re-checked inside the lock, since the waiter may be the second of two and the first has already done this.
        if host is not None and host.hosts(record.id):
            return
        if lifecycle is None:
            raise RuntimeError(
                f"Session {record.id} has no executor and there is nothing to build one."
            )
        if not await lifecycle.start(record):
            raise RuntimeError(f"Session {record.id} could not be started; see the daemon log.")


async def dispatch_to_session(session_id: str, method: str, params: dict) -> dict:
    """Call one of a hosted session's verbs, which is what used to cross a socket."""
    if host is None:
        raise RuntimeError("This daemon is hosting nothing.")
    return await host.dispatch(session_id, method, params)


async def relay_to_session(record, method: str, params: dict) -> dict:
    """Address a session by its record, which is how every caller outside this module reaches one."""
    return await dispatch_to_session(record.id, method, params)
