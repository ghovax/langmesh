"""Starting the daemon, and checking whether it is up.

`serve` and `mail` both need the daemon; this is how they start and probe it.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Optional

import httpx

from langmeshd.commons.timing import DAEMON_PROBE_INTERVAL_SECONDS, DAEMON_STARTUP_SECONDS
from langmeshd.daemon.paths import daemon_socket_path, daemon_token_path


class DaemonError(RuntimeError):
    """The daemon could not be started or reached."""


def _read_token() -> str:
    try:
        return daemon_token_path().read_text().strip()
    except OSError:
        return ""


def daemon_is_up() -> bool:
    """Whether something is actually listening, since a killed daemon leaves its socket behind."""
    path = daemon_socket_path()
    if not path.exists() or not _read_token():
        return False
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(path)), timeout=2.0) as client:
            return client.get("http://daemon/health").status_code == 200
    except (httpx.HTTPError, OSError):
        return False


def _socket_listening() -> bool:
    """Whether anything accepts the unix socket, even before /health succeeds."""
    path = daemon_socket_path()
    if not path.exists():
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(path))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def _wait_until_up(seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if daemon_is_up():
            return True
        time.sleep(DAEMON_PROBE_INTERVAL_SECONDS)
    return False


def _daemon_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "langmeshd"]
    return [sys.executable, "-m", "langmeshd", "langmeshd"]


def ensure_daemon() -> None:
    """Start the daemon if it is not up, and wait for the readiness line it writes itself.

    A systemd unit or Docker entrypoint may already be starting langmeshd. If the
    socket is accepting, wait for /health rather than spawning a twin that would
    only stand down on the singleton lock.
    """
    if daemon_is_up():
        return
    if _socket_listening():
        if _wait_until_up(DAEMON_STARTUP_SECONDS):
            return
        raise DaemonError(
            "langmeshd is listening but /health did not succeed. Check the daemon log under the state directory."
        )
    try:
        daemon = subprocess.Popen(
            _daemon_command(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            text=True,
            # Detached, so the daemon outlives the command that started it.
            start_new_session=True,
        )
    except OSError as error:
        raise DaemonError(f"Could not start langmeshd: {error}") from error

    announcement = _await_announcement(daemon)
    if announcement is None:
        raise DaemonError(
            "langmeshd exited before it was ready. Check the daemon log under the state directory."
            if daemon.poll() is not None
            else "langmeshd did not become ready in time. Check the daemon log under the state directory."
        )


def _await_announcement(daemon: subprocess.Popen) -> Optional[dict]:
    """The daemon's readiness announcement, or `None`, read on a worker thread so the timeout is real."""
    if daemon.stdout is None:
        return None
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(daemon.stdout.readline)
        try:
            line = pending.result(timeout=DAEMON_STARTUP_SECONDS)
        except FuturesTimeout:
            return None
    if not line:
        return None
    import json

    try:
        announcement = json.loads(line)
    except ValueError:
        return None
    return announcement if announcement.get("ready") else None
