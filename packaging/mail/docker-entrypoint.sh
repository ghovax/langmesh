#!/usr/bin/env bash
# Start the daemon and the mail client, restarting either if it dies. XDG must be a volume.
set -euo pipefail
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-/srv/langmesh/xdg/config}"
export XDG_DATA_HOME="${XDG_DATA_HOME:-/srv/langmesh/xdg/data}"
export XDG_STATE_HOME="${XDG_STATE_HOME:-/srv/langmesh/xdg/state}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-/srv/langmesh/xdg/cache}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/srv/langmesh/xdg/runtime}"
mkdir -p "${XDG_RUNTIME_DIR}/langmesh" \
  "${XDG_CONFIG_HOME}/langmesh" \
  "${XDG_DATA_HOME}/langmesh/secrets" \
  "${XDG_STATE_HOME}/langmesh"
chmod 700 "${XDG_RUNTIME_DIR}" "${XDG_RUNTIME_DIR}/langmesh" \
  "${XDG_DATA_HOME}/langmesh/secrets"

policy="${XDG_CONFIG_HOME}/langmesh/configuration.yaml"
if [[ ! -f "${policy}" && -f /srv/langmesh/packaging/mail/configuration.yaml ]]; then
  install -m 600 /srv/langmesh/packaging/mail/configuration.yaml "${policy}"
fi

/srv/langmesh/.venv/bin/python - <<'PY'
from langmeshd.commons import configuration_file
from langmeshd.commons.configuration_io import load_configuration
from langmeshd.commons.secret_import import import_into_files

load_configuration(seed=True)
import_into_files()
document = configuration_file.load() or {}
sandbox = document.setdefault("sandbox", {})
sandbox["enforce"] = sandbox.get("enforce") or "preferred"
sandbox["network"] = True
email = document.setdefault("email", {})
email.setdefault("agent", "reviewer")
email.setdefault("working_directory", "/srv/langmesh")
email.setdefault("permission_mode", "automatic")
if email.get("address"):
    email["enabled"] = True
invalid = configuration_file.rejects(document)
if invalid:
    raise SystemExit(invalid)
configuration_file.save(document)
PY

daemon_pid=""
mail_pid=""

stop() {
  if [[ -n "${mail_pid}" ]]; then kill "${mail_pid}" 2>/dev/null || true; fi
  if [[ -n "${daemon_pid}" ]]; then kill "${daemon_pid}" 2>/dev/null || true; fi
  wait || true
}
trap stop EXIT INT TERM

start_daemon() {
  rm -f "${XDG_RUNTIME_DIR}/langmesh/langmeshd.sock"
  /srv/langmesh/.venv/bin/python -m langmeshd langmeshd &
  daemon_pid=$!
  for _ in $(seq 1 300); do
    if /srv/langmesh/.venv/bin/python -c \
      'from langmeshd.cli.client import daemon_is_up; raise SystemExit(0 if daemon_is_up() else 1)'
    then
      return 0
    fi
    if ! kill -0 "${daemon_pid}" 2>/dev/null; then
      return 1
    fi
    sleep 0.2
  done
  return 1
}

start_mail() {
  /srv/langmesh/.venv/bin/langmesh mail &
  mail_pid=$!
}

start_daemon
start_mail
while true; do
  if ! kill -0 "${daemon_pid}" 2>/dev/null; then
    start_daemon
  fi
  if ! kill -0 "${mail_pid}" 2>/dev/null; then
    if [[ -S "${XDG_RUNTIME_DIR}/langmesh/langmeshd.sock" ]]; then
      start_mail
    fi
  fi
  sleep 1
done
