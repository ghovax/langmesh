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

# Start the daemon if it is not answering, then read the endpoint it published.
"${python[@]}" -c "from langmesh.cli.client import ensure_daemon; ensure_daemon()"

daemon_port="$("${python[@]}" -c "from langmesh.base.confinement.paths import daemon_port_path; print(daemon_port_path().read_text().strip())")"
daemon_token="$("${python[@]}" -c "from langmesh.base.confinement.paths import daemon_token_path; print(daemon_token_path().read_text().strip())")"

if [[ -z "$daemon_port" ]]; then
  echo "Could not read the daemon's published port." >&2
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
