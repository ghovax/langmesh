"""The daemon's own lifecycle timings, which the library must not carry."""

from __future__ import annotations

# How long to wait for the daemon to come up after starting it.
DAEMON_STARTUP_SECONDS = 45.0
# How often a probe retries a not-yet-listening daemon, and how long each connect may wait.
DAEMON_PROBE_INTERVAL_SECONDS = 0.05
DAEMON_PROBE_CONNECT_SECONDS = 0.5
# How long an idle hosted session sleeps before it is let go (five hours).
SESSION_IDLE_SLEEP_SECONDS = 18_000.0
