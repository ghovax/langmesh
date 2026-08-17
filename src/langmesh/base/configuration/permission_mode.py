"""Who answers a gate: the person, or the reviewer. Not what a session may do — that is its confinement."""

from __future__ import annotations

from enum import StrEnum
from typing import Optional

class PermissionMode(StrEnum):
    """Who answers when a call asks to reach past its confinement."""

    ASK = "ask"
    AUTOMATIC = "automatic"
    #: No gate at all: every call runs as if it had whatever it asked for. The confinement
    #: still applies to what a call may touch; only the asking is skipped.
    ALLOW = "allow"

    @classmethod
    def parse(cls, value: str | PermissionMode | None) -> Optional[PermissionMode]:
        """The mode a string names, or ``None`` for a name that is not one."""
        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except ValueError:
            return None

    @classmethod
    def resolve(
        cls, value: str | PermissionMode | None, default: PermissionMode | None = None
    ) -> PermissionMode:
        """Resolve an absent mode to a default and reject every unknown value."""
        if value is None or value == "":
            return default if default is not None else cls.ASK
        parsed = cls.parse(value)
        if parsed is None:
            raise ValueError(f"unknown permission mode: {value!r}")
        return parsed

    @property
    def restrictiveness(self) -> int:
        """Position in the restrictiveness order: ``allow < automatic < ask``."""
        return {
            PermissionMode.ASK: 2,
            PermissionMode.AUTOMATIC: 1,
            PermissionMode.ALLOW: 0,
        }[self]

    @classmethod
    def more_restrictive(cls, *modes: str | PermissionMode | None) -> PermissionMode:
        """The stricter of the given modes. The child-session clamp, so a peer is never looser than its creator."""
        candidates = [cls.resolve(value) for value in modes if value is not None and value != ""]
        return max(candidates, key=lambda mode: mode.restrictiveness) if candidates else cls.ASK

    @classmethod
    def child_of(
        cls,
        parent: str | PermissionMode | None,
        *,
        requested: str | PermissionMode | None = None,
        fallback: str | PermissionMode | None = None,
    ) -> PermissionMode:
        """Resolve a session mode from its request and defaults without widening past its parent."""
        parent_mode = None if parent is None or parent == "" else cls.resolve(parent)
        chosen = cls.more_restrictive(
            (
                cls.resolve(requested)
                if requested is not None and requested != ""
                else parent_mode
                or cls.resolve(fallback)
            ),
            parent_mode,
        )
        if parent_mode is not None and parent_mode.never_asks and not chosen.never_asks:
            raise ValueError(
                f"a session running unattended can only create sessions that also run unattended, and {chosen} stops to ask"
            )
        return chosen

    @property
    def never_asks(self) -> bool:
        """Whether this mode can run with nobody watching."""
        return self in (PermissionMode.AUTOMATIC, PermissionMode.ALLOW)

    @property
    def asks(self) -> bool:
        """Whether a gate goes to a person."""
        return self is PermissionMode.ASK

    @property
    def gates(self) -> bool:
        """Whether calls are gated at all. ``allow`` lets every call run without asking."""
        return self is not PermissionMode.ALLOW
