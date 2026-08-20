"""Why a request did or did not hit the provider's prompt cache.

``prefix_intact`` is tri-state: ``True`` means every segment this request shares with the
previous request in its lane is byte-identical (so a prefix cache that keeps the previous
request would serve it), ``False`` means something moved or rewrote, and ``None`` means there
is no previous reading to compare against, so the outcome is *unknown*, not a miss.

The paired state matrix for the situations the harness actually produces:

- First call in a lane (no baseline): outcome unknown; ``prefix_intact=None``.
- User interrupt or steering while the model is not streaming: the next request simply appends
  the new message; the shared prefix stays intact -> intact.
- Steering arriving during a stream: queued and drained at the model boundary, so it lands on
  the next request, appended -> intact.
- Tool interrupt: each tool result appends a ``tool`` message; the request grows, the prefix
  stays -> intact.
- Network drop on a request: the outgoing request is atomic; the retry resends the identical
  bytes, so the baseline from the first attempt still matches -> intact. A dropped response is
  not itself a reading, so the comparison stays on the request boundary, not the response.
- Background job returning late: delivered as steering on the next request, appended -> intact.
- Compaction: the fold rewrites the head of the conversation, so the previous prefixes no
  longer match and the first request after a fold diagnoses a divergence -> miss.
- Session/daemon restart: the in-memory baseline is gone; the first request is unknown again.
- Auxiliary lanes (permission review, compaction summary): fall back to the conversation lane's
  baseline so their diagnosis is against the main session's request, not nothing.
- A response that carries no usage reading: the request itself is still the baseline, so the
  follow-up request is diagnosed against it even though no token figure was reported.

The invariant this locks in: every request advances the baseline at the point it is sent, so
the next request always compares against the bytes that were actually sent, never against a
request that only exists because a response happened to report usage.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Generator, Optional, Sequence

from langmesh.base.primitives.limits import count_tokens

#: The kinds of thing a request is made of, in the order every provider reads them.
INSTRUCTIONS = "instructions"
TOOLS = "tools"
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


def provider_cache_key(session_id: str) -> str:
    """The stable provider key shared by the main conversation and every branch from it."""
    return session_id


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


def trace(pieces: Sequence[Piece]) -> RequestTrace:
    """Measure a request's pieces into segments."""
    return RequestTrace(
        [
            Segment(
                kind=piece.kind,
                position=piece.position,
                role=piece.role,
                digest=hashlib.blake2b(piece.text.encode(), digest_size=8).hexdigest(),
                tokens=count_tokens(piece.text),
            )
            for piece in pieces
        ]
    )


def prefix_intact_label(value: Optional[bool]) -> str:
    """A human reading of the tri-state: the prefix was reusable, it moved, or we cannot tell."""
    if value is None:
        return "unknown"
    return "intact" if value else "moved"


def diagnose(current: RequestTrace, previous: Optional[RequestTrace]) -> dict[str, object]:
    """What this request kept from the last one, as fields to record beside the cache figure.

    ``prefix_intact`` is ``None`` when there is no previous request to compare against: the
    outcome is unknown, which reads very differently from a confirmed miss.
    """
    if previous is None:
        return {
            "prefix_intact": None,
            "reachable_tokens": 0,
            "segments": len(current.segments),
            "shared_segments": 0,
            "divergence": None,
        }
    shared = 0
    for mine, theirs in zip(previous.segments, current.segments):
        if mine.digest != theirs.digest:
            break
        shared += 1
    common = {
        "reachable_tokens": sum(segment.tokens for segment in current.segments[:shared]),
        "segments": len(current.segments),
        "shared_segments": shared,
    }
    if shared == len(previous.segments):
        return {"prefix_intact": True, "divergence": None, **common}
    here = current.segments[shared] if shared < len(current.segments) else None
    there = previous.segments[shared]
    return {
        "prefix_intact": False,
        "divergence": {
            "index": shared,
            "current": here.identity() if here else None,
            "previous": there.identity(),
            # Same place and identity, different digest: the piece did not move, its contents changed.
            "rewritten": bool(here and here.identity() == there.identity()),
        },
        **common,
    }


__all__ = [
    "INSTRUCTIONS",
    "ITEM",
    "TOOLS",
    "Piece",
    "RequestTrace",
    "Segment",
    "active_cache_lane",
    "cache_lane",
    "diagnose",
    "prefix_intact_label",
    "trace",
    "provider_cache_key",
]
