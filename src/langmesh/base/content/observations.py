"""Typed observational-memory values independent of any persistence mechanism."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

OBSERVATIONS_FILENAME = "observations.sqlite"


class ObservationEntry(BaseModel, frozen=True):
    """One validated factual entry in observational memory."""

    id: str
    category: Literal["fact", "decision", "constraint", "failure", "artifact", "open"]
    claim: str
    detail: str
    evidence: str | None = None
    standing: Literal["verified", "reported", "inferred"]
    files: tuple[str, ...] = ()
    updated_at: datetime


class DirectiveEntry(BaseModel, frozen=True):
    """One validated behavioral directive in observational memory."""

    id: str
    kind: Literal["requirement", "preference"]
    summary: str
    detail: str | None = None
    occasion: str | None = None
    files: tuple[str, ...] = ()
    updated_at: datetime


class ObservationSnapshot(BaseModel, frozen=True):
    """One coherent revision of both observational-memory ledgers."""

    revision: int = 0
    observations: tuple[ObservationEntry, ...] = ()
    directives: tuple[DirectiveEntry, ...] = ()


class RegistryCounts(BaseModel, frozen=True):
    """Entry totals for each observational-memory ledger."""

    observations: int = 0
    directives: int = 0


class RegistryTimestamps(BaseModel, frozen=True):
    """The inclusive timestamp extent of a registry."""

    earliest: datetime | None = None
    latest: datetime | None = None


class RegistryMetadata(BaseModel, frozen=True):
    """Bounded registry health and revision metadata."""

    path: str = ""
    exists: bool = False
    revision: int = 0
    counts: RegistryCounts = RegistryCounts()
    updated_at: RegistryTimestamps = RegistryTimestamps()
    status: Literal["missing", "ok", "broken"] = "missing"
    problem: str = ""


__all__ = [
    "DirectiveEntry",
    "ObservationEntry",
    "ObservationSnapshot",
    "OBSERVATIONS_FILENAME",
    "RegistryCounts",
    "RegistryMetadata",
    "RegistryTimestamps",
]
