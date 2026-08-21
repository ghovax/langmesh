"""Discover SSH hosts from the daemon user's own OpenSSH configuration."""

from __future__ import annotations

import glob
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SSH_CONFIGURATION_PATH = Path("~/.ssh/config").expanduser()
_PATTERN_CHARACTERS = set("*?!")


@dataclass(frozen=True)
class SshHost:
    """A connectable host and the coordinates resolved by OpenSSH."""

    alias: str
    hostname: str
    user: str = ""
    port: int = 22
    identity_files: tuple[str, ...] = field(default_factory=tuple)


def _configuration_paths(configuration_path: Path, seen: set[Path] | None = None) -> list[Path]:
    """Return the configuration file and every recursively included file."""
    visited = seen if seen is not None else set()
    resolved = configuration_path.expanduser()
    if resolved in visited or not resolved.is_file():
        return []
    visited.add(resolved)
    paths = [resolved]
    try:
        lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return paths
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        keyword, _, remainder = stripped.partition(" ")
        if keyword.lower() != "include" or not remainder.strip():
            continue
        for token in remainder.split():
            candidate = Path(token).expanduser()
            bases = [resolved.parent] if not candidate.is_absolute() else [Path("/")]
            if not candidate.is_absolute():
                bases.append(DEFAULT_SSH_CONFIGURATION_PATH.parent)
            for base in bases:
                for match in sorted(glob.glob(str((base / token).expanduser()))):
                    paths.extend(_configuration_paths(Path(match), visited))
    return paths


def _literal_aliases(configuration_path: Path) -> list[str]:
    aliases: list[str] = []
    seen: set[str] = set()
    for path in _configuration_paths(configuration_path):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            keyword, _, remainder = stripped.partition(" ")
            if keyword.lower() != "host":
                continue
            for token in remainder.split():
                if token and not (_PATTERN_CHARACTERS & set(token)) and token not in seen:
                    seen.add(token)
                    aliases.append(token)
    return aliases


def resolve_host(alias: str, *, timeout: float = 5.0) -> SshHost | None:
    """Resolve one alias through OpenSSH without making a connection."""
    try:
        completed = subprocess.run(
            ["ssh", "-G", alias],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    hostname = alias
    user = ""
    port = 22
    identity_files: list[str] = []
    for line in completed.stdout.splitlines():
        key, _, value = line.strip().partition(" ")
        key = key.lower()
        value = value.strip()
        if key == "hostname" and value:
            hostname = value
        elif key == "user" and value:
            user = value
        elif key == "port" and value.isdigit():
            port = int(value)
        elif key == "identityfile" and value:
            identity_files.append(os.path.expanduser(value))
    return SshHost(
        alias=alias, hostname=hostname, user=user, port=port, identity_files=tuple(identity_files)
    )


def list_ssh_hosts(
    configuration_path: Path = DEFAULT_SSH_CONFIGURATION_PATH,
) -> list[SshHost]:
    """Return every literal and connectable host from an OpenSSH configuration."""
    return [
        host
        for alias in _literal_aliases(configuration_path)
        if (host := resolve_host(alias)) is not None
    ]


def host_is_defined(alias: str) -> bool:
    """Return whether an alias is declared and resolvable in the daemon user's configuration."""
    return any(host.alias == alias for host in list_ssh_hosts())


__all__ = ["SshHost", "host_is_defined", "list_ssh_hosts", "resolve_host"]
