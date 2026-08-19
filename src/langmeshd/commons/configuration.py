"""The app's own configuration sections: features the daemon hosts but the library never models.

The library's Configuration carries only what the runtime itself reads. Dictation and Composio
are daemon-hosted features, so their sections live here, read and written straight from the
shared configuration file.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, Field

from langmesh.base.confinement import environment_variables
from langmesh.protocol.dtos import SettingsUpdateRequest
from langmeshd.commons import timing


class AppSettingsUpdateRequest(SettingsUpdateRequest):
    """The library's settings request, plus the app-owned Composio key."""

    composio_api_key: str | None = None


class DictationUpdateRequest(BaseModel):
    """Opt-in/out of transcribing the composer's dictation on this machine."""

    enabled: bool


class DictationTimingConfiguration(BaseModel):
    """How long dictation waits before giving up, separated because these are what a slow machine must move."""

    minimum_transcription_timeout_seconds: float = Field(default=30.0)
    transcription_timeout_realtime_multiplier: float = Field(default=0.5)
    maximum_attempts: int = Field(default=2, ge=1)
    worker_shutdown_seconds: float = Field(default=2.0)


class DaemonConfiguration(BaseModel):
    """The daemon's own lifecycle timings, configurable in the file and never part of the library.

    The library must not carry these: they are properties of the daemon process, not of a
    session or the runtime.
    """

    # How long to wait for the daemon to come up after starting it.
    startup_seconds: float = Field(default=timing.DAEMON_STARTUP_SECONDS)
    # How often a probe retries a not-yet-listening daemon, and how long each connect may wait.
    probe_interval_seconds: float = Field(default=timing.DAEMON_PROBE_INTERVAL_SECONDS)
    probe_connect_seconds: float = Field(default=timing.DAEMON_PROBE_CONNECT_SECONDS)
    # How long an idle hosted session sleeps before it is let go (five hours).
    session_idle_sleep_seconds: float = Field(default=timing.SESSION_IDLE_SLEEP_SECONDS)


class DictationConfiguration(BaseModel):
    """Opt-in speech-to-text, transcribed locally. Off by default: the first use downloads about a gigabyte."""

    enabled: bool = Field(default=False)
    model: str = Field(default="mlx-community/parakeet-tdt-0.6b-v3")
    timing: DictationTimingConfiguration = Field(default_factory=DictationTimingConfiguration)


class ComposioConfiguration(BaseModel):
    """Composio's hosted MCP endpoint, exposed as an ordinary streamable_http server."""

    enabled: bool = Field(default=False)
    url: str = Field(default="https://connect.composio.dev/mcp")
    api_key: str = Field(default="", json_schema_extra={"secret": True})
    server_name: str = Field(default="composio")
    timeout_seconds: float = Field(default=60)

    @property
    def effective_api_key(self) -> str:
        return os.environ.get(environment_variables.COMPOSIO_API_KEY) or self.api_key
