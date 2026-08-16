"""Why a request did or did not hit the provider's prompt cache."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Generator, Optional, Sequence

from langmesh.base.primitives.tuning import count_tokens

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


def diagnose(current: RequestTrace, previous: Optional[RequestTrace]) -> dict[str, object]:
    """What this request kept from the last one, as fields to record beside the cache figure."""
    if previous is None:
        return {
            "prefix_intact": False,
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
    "trace",
    "provider_cache_key",
]
