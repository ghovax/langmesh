"""Where the daemon's own handshake and lock files live.

These are daemon concepts, not core ones: the core library knows the runtime
directory, and the daemon decides what it publishes inside it.
"""

from __future__ import annotations

from pathlib import Path

from langmesh.base.confinement.paths import (
    SOCKET_PATH_MAXIMUM_BYTES,
    runtime_directory,
    state_directory,
)


class SocketPathTooLong(OSError):
    """A unix socket path exceeds what `bind` accepts, raised as its own error so it names the path and the limit."""


def _within_socket_limit(path: Path) -> Path:
    """The path if it can actually be bound, checked at construction so the caller that chose the directory hears it."""
    encoded = len(str(path).encode())
    if encoded > SOCKET_PATH_MAXIMUM_BYTES:
        raise SocketPathTooLong(
            f"{path} is {encoded} bytes, and a unix socket path may be at most {SOCKET_PATH_MAXIMUM_BYTES}. The runtime directory is too deep — set XDG_RUNTIME_DIR to something shorter."
        )
    return path


def daemon_socket_path() -> Path:
    return _within_socket_limit(runtime_directory() / "langmeshd.sock")


def daemon_token_path() -> Path:
    """The capability token the daemon mints at startup, written 0600 so permissions are the access control."""
    return runtime_directory() / "token"


def daemon_port_path() -> Path:
    """The loopback port for clients that cannot open a unix socket, written beside the token."""
    return runtime_directory() / "port"


def daemon_pid_path() -> Path:
    """The pidfile, how a stop signal reaches a daemon that has stopped answering."""
    return runtime_directory() / "langmeshd.pid"


def daemon_lock_path() -> Path:
    """The singleton lock, held for the daemon's whole life so a second one stands down."""
    return runtime_directory() / "langmeshd.lock"


def daemon_log_path() -> Path:
    """The daemon's own log, beside the state directory's other logs."""
    return state_directory() / "langmeshd.log"
