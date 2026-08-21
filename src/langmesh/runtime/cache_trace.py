"""Why a request did or did not hit the provider's prompt cache.

``cache_prefix_reusable`` is tri-state: ``True`` means every segment this request shares with the
previous request in its lane is byte-identical (so a prefix cache that kept the previous
request would serve it), ``False`` means something moved or rewrote, and ``None`` means there
is no previous reading to compare against, so the outcome is *unknown*, not a miss.

The byte verdict is a prediction made before the call; :func:`reconcile` corrects it with the
provider's actual ``cache_read`` once the response lands. A divergence the provider served
straight through is not a miss, and real reuse beside a byte model that never lined up is
unknown rather than a confirmed miss.

Situation matrix, one row each:

- First-ever call in a lane: no baseline, outcome unknown; a restored session retains its baseline.
- User interrupt or steering while idle: next request appends the message; prefix stays intact.
- Steering during a stream: queued, drained at the model boundary, appended; stays intact.
- Tool interrupt: each tool result appends a message; request grows, prefix stays intact.
- Network drop: the retry resends identical bytes, so the first attempt still matches; a dropped response is not a reading.
- Background job returning late: delivered as steering on the next request, appended; stays intact.
- Compaction: rewrites the conversation head, so the first request after a fold diverges; miss.
- Usage-less or interrupted response: the request itself is still the baseline for the next request.

The invariant this locks in: every request advances the baseline at the point it is sent, so
the next request always compares against the bytes that were actually sent, never against a
request that only exists because a response happened to report usage.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Generator, MutableMapping, Optional, Sequence, TypeVar

from langmesh.base.primitives.limits import count_tokens

#: The kinds of thing a request is made of, in the order every provider reads them.
INSTRUCTIONS = "instructions"
TOOLS = "tools"
SETTINGS = "settings"
ITEM = "item"

_CACHE_LANE = ContextVar("cache_lane", default="conversation")


@contextmanager
def cache_lane(name: str) -> Generator[None, None, None]:
    """Run an auxiliary model call on its own reusable cache branch."""
    normalized = name.strip()
    if not normalized or normalized == "conversation":
        raise ValueError("an auxiliary cache lane needs a distinct non-empty name")
    parent = active_cache_lane()
    if parent != "conversation":
        normalized = f"{parent}/{normalized}"
    token = _CACHE_LANE.set(normalized)
    try:
        yield
    finally:
        _CACHE_LANE.reset(token)


def active_cache_lane() -> str:
    """The cache branch for the current model call."""
    return _CACHE_LANE.get()


def provider_cache_key(*stable_prefix: str) -> str:
    """Route byte-identical static prefixes to the same provider cache across sessions."""
    digest = hashlib.blake2b(digest_size=16)
    for part in stable_prefix:
        encoded = part.encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


@dataclass(frozen=True)
class Piece:
    """One addressable part of a request, before it is measured."""

    kind: str
    text: str
    position: int = -1
    role: str = ""


@dataclass(frozen=True)
class Segment:
    """A measured piece: what it is, and what it hashed and counted to."""

    kind: str
    position: int
    role: str
    digest: str
    tokens: int

    def identity(self) -> dict[str, object]:
        """The part a consumer identifies it by, without the measurement."""
        return {"kind": self.kind, "position": self.position, "role": self.role}


@dataclass
class RequestTrace:
    """One request's segments, in wire order."""

    segments: list[Segment] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return sum(segment.tokens for segment in self.segments)


_LaneValue = TypeVar("_LaneValue")


def remember_cache_lane(
    lanes: MutableMapping[str, _LaneValue],
    lane: str,
    value: _LaneValue,
    *,
    limit: int = 16,
) -> None:
    """Remember the newest bounded lane value while preferentially retaining the conversation baseline."""
    lanes.pop(lane, None)
    lanes[lane] = value
    while len(lanes) > max(1, limit):
        oldest = next((name for name in lanes if name != "conversation"), next(iter(lanes)))
        del lanes[oldest]


def request_trace_snapshot(value: RequestTrace) -> dict[str, object]:
    """Serialize a request trace into the JSON-safe form stored beside a session."""
    return {
        "segments": [
            {
                "kind": segment.kind,
                "position": segment.position,
                "role": segment.role,
                "digest": segment.digest,
                "tokens": segment.tokens,
            }
            for segment in value.segments
        ]
    }


def restore_request_trace(value: object) -> Optional[RequestTrace]:
    """Restore one defensively validated request trace or reject the malformed snapshot."""
    if not isinstance(value, dict) or not isinstance(value.get("segments"), list):
        return None
    segments: list[Segment] = []
    try:
        for raw in value["segments"]:
            if not isinstance(raw, dict):
                return None
            digest = str(raw["digest"])
            if not digest:
                return None
            segments.append(
                Segment(
                    kind=str(raw["kind"]),
                    position=int(raw["position"]),
                    role=str(raw["role"]),
                    digest=digest,
                    tokens=max(0, int(raw["tokens"])),
                )
            )
    except (KeyError, TypeError, ValueError):
        return None
    return RequestTrace(segments)


def request_traces_snapshot(values: dict[str, RequestTrace]) -> dict[str, object]:
    """Serialize the bounded cache-lane baselines in their recency order."""
    return {lane: request_trace_snapshot(value) for lane, value in values.items()}


def restore_request_traces(value: object, *, limit: int = 16) -> dict[str, RequestTrace]:
    """Restore at most the newest validated cache-lane baselines."""
    restored: dict[str, RequestTrace] = {}
    if not isinstance(value, dict):
        return restored
    for raw_lane, raw_trace in list(value.items())[-max(1, limit) :]:
        lane = str(raw_lane).strip()
        candidate = restore_request_trace(raw_trace)
        if lane and candidate is not None:
            remember_cache_lane(restored, lane, candidate, limit=limit)
    return restored


def trace(pieces: Sequence[Piece]) -> RequestTrace:
    """Measure a request's pieces into segments."""
    return RequestTrace(
        [
            Segment(
                kind=piece.kind,
                position=piece.position,
                role=piece.role,
                digest=hashlib.blake2b(piece.text.encode(), digest_size=8).hexdigest(),
                tokens=0 if piece.kind == SETTINGS else count_tokens(piece.text),
            )
            for piece in pieces
        ]
    )


def cache_prefix_label(value: Optional[bool]) -> str:
    """A human reading of the tri-state: the prefix was reusable, it moved, or we cannot tell."""
    if value is None:
        return "unknown"
    return "intact" if value else "moved"


def diagnose(current: RequestTrace, previous: Optional[RequestTrace]) -> dict[str, object]:
    """What this request kept from the last one, as fields to record beside the cache figure.

    ``cache_prefix_reusable`` is ``None`` when there is no previous request to compare against: the
    outcome is unknown, which reads very differently from a confirmed miss.
    """
    if previous is None:
        return {
            "cache_prefix_reusable": None,
            "reusable_prefix_tokens": 0,
            "segments": len(current.segments),
            "shared_segments": 0,
            "divergence": None,
        }
    shared = 0
    for mine, theirs in zip(previous.segments, current.segments, strict=False):
        if mine.digest != theirs.digest:
            break
        shared += 1
    common = {
        "reusable_prefix_tokens": sum(segment.tokens for segment in current.segments[:shared]),
        "segments": len(current.segments),
        "shared_segments": shared,
    }
    if shared == len(previous.segments):
        return {"cache_prefix_reusable": True, "divergence": None, **common}
    here = current.segments[shared] if shared < len(current.segments) else None
    there = previous.segments[shared]
    return {
        "cache_prefix_reusable": False,
        "divergence": {
            "index": shared,
            "current": here.identity() if here else None,
            "previous": there.identity(),
            # Same place and identity, different digest: the piece did not move, its contents changed.
            "rewritten": bool(here and here.identity() == there.identity()),
        },
        **common,
    }


def reconcile(diagnosis: dict, cache_read: int) -> dict:
    """Correct the pre-request verdict with what the provider actually served from cache.

    ``diagnose`` reads only the two request shapes, so its ``cache_prefix_reusable`` may judge a
    request a miss the provider's cache then served. This passes the byte verdict against the
    response's ``cache_read`` and adjusts the judgment in place, leaving the ``divergence``
    detail for anyone who wants to know exactly where the request changed.
    """
    verdict = diagnosis.get("cache_prefix_reusable")
    if verdict is None or verdict is True:
        return diagnosis  # no baseline, or the byte prediction already held
    reachable = int(diagnosis.get("reusable_prefix_tokens", 0) or 0)
    if cache_read <= 0:
        return diagnosis  # nothing was reused, so the confirmed break stands
    if reachable <= 0:
        # Real reuse beside a byte model that found no shared segment: the two requests never
        # lined up (a borrowed cross-lane baseline), so the outcome is unknown, not a miss.
        diagnosis["cache_prefix_reusable"] = None
        return diagnosis
    # reachable and cache_read are tokenizer-shaped, so the comparison allows a hair of variance.
    if cache_read >= reachable * 0.98:
        diagnosis["cache_prefix_reusable"] = True  # the divergence cost nothing the cache had held
    return diagnosis


__all__ = [
    "INSTRUCTIONS",
    "ITEM",
    "SETTINGS",
    "TOOLS",
    "Piece",
    "RequestTrace",
    "Segment",
    "active_cache_lane",
    "cache_lane",
    "diagnose",
    "cache_prefix_label",
    "reconcile",
    "trace",
    "provider_cache_key",
    "remember_cache_lane",
    "request_trace_snapshot",
    "request_traces_snapshot",
    "restore_request_trace",
    "restore_request_traces",
]
