"""Run a command against a location: locally, or over a durable multiplexed SSH connection.

The plugin's whole job is a durable connection to the other machine. An SSH location keeps
one multiplexed master (ControlMaster) per alias, so every command is forwarded over the
same open connection — no repeated handshakes, no per-call setup. Nothing else: file
listing, search, and transfer machinery have no place here, because bash itself is
forwarded to the remote and does that work there.
"""

from __future__ import annotations

import abc
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from langmesh.base.confinement.paths import ssh_control_directory, ssh_control_identifier


# Baseline command and connect ceilings, scaled at each subprocess boundary by the active timeout knob.
DEFAULT_TIMEOUT = 120.0
DEFAULT_CONNECT_TIMEOUT = 16.0
# Keep the multiplexed master alive briefly after the last use, so bursts of calls reuse one connection.
CONTROL_PERSIST_SECONDS = 120


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _login_script(command: str, cwd: str, env: dict[str, str] | None) -> str:
    """One shell script: change into the base directory, export any extra environment, run the command."""
    prefix = ""
    if env:
        prefix = "".join(
            f"export {name}={shlex.quote(str(value))}; " for name, value in env.items()
        )
    return f"cd {shlex.quote(cwd)} && {prefix}{command}"


class LocationExecutor(abc.ABC):
    """One place a command can run, local or over SSH."""

    is_local = True

    @abc.abstractmethod
    def run(
        self,
        command: str,
        cwd: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Run one shell command and return its output."""

    @abc.abstractmethod
    def connect(self, *, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> CommandResult:
        """Establish the durable connection and confirm the host is reachable."""

    @abc.abstractmethod
    def disconnect(self) -> None:
        """Tear the durable connection down."""

    @abc.abstractmethod
    def terminal_argv(self, base_directory: str) -> list[str]:
        """The argv for an interactive login shell on this location."""


class LocalExecutor(LocationExecutor):
    """Runs a command on the machine this process is on."""

    is_local = True

    def run(
        self,
        command: str,
        cwd: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        completed = subprocess.run(
            ["bash", "-lc", _login_script(command, cwd, env)],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return CommandResult(
            completed.returncode,
            completed.stdout.decode("utf-8", errors="replace"),
            completed.stderr.decode("utf-8", errors="replace"),
        )

    def connect(self, *, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> CommandResult:
        return CommandResult(0, "", "")

    def disconnect(self) -> None:
        return None

    def terminal_argv(self, base_directory: str) -> list[str]:
        return ["bash", "-l"]


class SshExecutor(LocationExecutor):
    """Executes on a remote host over one durable multiplexed SSH connection, named by its `~/.ssh/config` alias."""

    is_local = False

    def __init__(self, alias: str, control_directory: Path | None = None):
        self.alias = alias
        # Runtime state, so the control socket has the lifetime the OS gives the runtime directory.
        self._control_directory = control_directory or ssh_control_directory()
        self._control_directory.mkdir(parents=True, exist_ok=True)

    def _mux_options(self) -> list[str]:
        # Our own digest of the alias rather than ssh's, whose full hash overran the unix socket path limit.
        control_path = str(self._control_directory / ssh_control_identifier(self.alias))
        return [
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPath={control_path}",
            "-o",
            f"ControlPersist={CONTROL_PERSIST_SECONDS}",
        ]

    def _ssh(
        self,
        remote_command: str,
        *,
        timeout: float,
        extra_options: list[str] | None = None,
        stdin: bytes | None = None,
    ) -> subprocess.CompletedProcess:
        argv = ["ssh", *self._mux_options(), *(extra_options or []), self.alias, remote_command]
        return subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    def ssh_argv(self, command: str, cwd: str, env: dict[str, str] | None = None) -> list[str]:
        """The exact ssh argv a `run` would execute, so the local bash machinery can forward a remote command."""
        remote = f"bash -lc {shlex.quote(_login_script(command, cwd, env))}"
        return ["ssh", *self._mux_options(), self.alias, remote]

    def run(
        self,
        command: str,
        cwd: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        remote = f"bash -lc {shlex.quote(_login_script(command, cwd, env))}"
        completed = self._ssh(remote, timeout=timeout)
        return CommandResult(
            completed.returncode,
            completed.stdout.decode("utf-8", errors="replace"),
            completed.stderr.decode("utf-8", errors="replace"),
        )

    def connect(self, *, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> CommandResult:
        """Establish or reuse the master and confirm the host is reachable, letting interactive auth surface."""
        completed = self._ssh(
            "true", timeout=timeout, extra_options=["-o", f"ConnectTimeout={int(timeout)}"]
        )
        return CommandResult(
            completed.returncode,
            completed.stdout.decode("utf-8", errors="replace"),
            completed.stderr.decode("utf-8", errors="replace"),
        )

    def disconnect(self) -> None:
        """Tear down the multiplexed master, if any."""
        subprocess.run(
            ["ssh", *self._mux_options(), "-O", "exit", self.alias],
            capture_output=True,
            timeout=DEFAULT_CONNECT_TIMEOUT,
            check=False,
        )

    def terminal_argv(self, base_directory: str) -> list[str]:
        """The ssh argv for an interactive login shell on the remote, sharing this host's multiplexed connection."""
        remote_command = (
            f"cd {shlex.quote(base_directory)} 2>/dev/null; exec ${{SHELL:-/bin/bash}} -l"
        )
        return ["ssh", "-tt", *self._mux_options(), self.alias, remote_command]


__all__ = ["CommandResult", "LocationExecutor", "LocalExecutor", "SshExecutor"]
