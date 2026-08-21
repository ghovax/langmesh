"""Resolve a location record into its model-facing URI and an executor to run tools against it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .executor import LocalExecutor, LocationExecutor, SshExecutor
from .location_uri import format_local, format_remote


@dataclass(frozen=True)
class LocationAddress:
    """The connection-relevant fields of a location, independent of storage."""

    kind: str  # "local" | "remote"
    base_directory: str
    host_alias: str = ""


def location_uri_for(address: LocationAddress) -> str:
    """The URI the agent passes as ``location``, derived only from caller-supplied values."""
    if address.kind == "local":
        return format_local(address.base_directory)
    if address.kind == "remote":
        if not address.host_alias:
            raise ValueError("A remote location requires an ssh host alias.")
        return format_remote(address.host_alias, address.base_directory)
    raise ValueError(f"Unknown location kind: {address.kind!r}")


def executor_for(address: LocationAddress, *, control_directory: Path | None = None) -> LocationExecutor:
    """The executor that runs tools against this location."""
    if address.kind == "local":
        return LocalExecutor()
    if address.kind == "remote":
        if not address.host_alias:
            raise ValueError("A remote location requires an ssh host alias.")
        if control_directory is None:
            raise ValueError("A remote location requires an SSH control directory from its host.")
        return SshExecutor(address.host_alias, control_directory=control_directory)
    raise ValueError(f"Unknown location kind: {address.kind!r}")
