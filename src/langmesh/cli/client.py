"""Starting the daemon, and checking whether it is up.

The command line's only verb is serving, which needs the daemon; this is how it starts
and probes the daemon, and the only surface this module exists for.
"""

from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Optional

import httpx

from langmesh.base.primitives.tuning import Tunable, active_tuning
from langmesh.daemon.paths import daemon_socket_path, daemon_token_path


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


def _daemon_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "langmeshd"]
    return [sys.executable, "-m", "langmesh", "langmeshd"]


def ensure_daemon() -> None:
    """Start the daemon if it is not up, and wait for the readiness line it writes itself."""
    if daemon_is_up():
        return
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
            line = pending.result(timeout=active_tuning().duration(Tunable.daemon_startup))
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
