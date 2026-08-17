#!/usr/bin/env bash
# Start the web UI in development, pointed straight at the running daemon.
#
#   ./scripts/web-development.sh          # then open http://localhost:3000
#
# The daemon serves its own API on an ephemeral loopback port and authenticates with a
# capability token it publishes beside the port. No bridge or proxy is involved: the dev
# page reads the port and token here and talks to the daemon directly (its CORS allows
# localhost). Run this outside `nix develop`; the handoff below enters the devshell only
# after finding the daemon.
set -euo pipefail

repository="$(cd "$(dirname "$0")/.." && pwd)"

if [[ -x "$repository/.venv/bin/python" ]]; then
  python=("$repository/.venv/bin/python")
else
  python=(uv run --project "$repository" python)
fi

# Start the daemon if it is not answering.
"${python[@]}" -c "from langmesh.cli.client import ensure_daemon; ensure_daemon()"

# The daemon publishes its handshake files in the runtime directory: the same derivation the
# core uses, reproduced here so a shell reads them without a Python round-trip.
if [[ -n "${XDG_RUNTIME_DIR:-}" && "${XDG_RUNTIME_DIR}" == /* ]]; then
  runtime_directory="${XDG_RUNTIME_DIR}/langmesh"
else
  runtime_directory="${TMPDIR:-/tmp}/langmesh-$(id -u)"
fi

daemon_port="$(cat "$runtime_directory/port" 2>/dev/null || true)"
daemon_token="$(cat "$runtime_directory/token" 2>/dev/null || true)"

if [[ -z "$daemon_port" ]]; then
  echo "Could not read the daemon's published port from $runtime_directory/port." >&2
  exit 1
fi

echo "langmeshd on http://127.0.0.1:$daemon_port — starting the UI at http://localhost:${DEV_PORT:-3000}" >&2

cd "$repository/web"
export NEXT_PUBLIC_API_BASE="http://127.0.0.1:$daemon_port"
export NEXT_PUBLIC_TOKEN="$daemon_token"

if command -v bun >/dev/null 2>&1; then
  bun run dev "$@"
  exit $?
fi
nix develop . -c bun run dev "$@"
