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
  "${XDG_DATA_HOME}/langmesh" \
  "${XDG_STATE_HOME}/langmesh"
chmod 700 "${XDG_RUNTIME_DIR}" "${XDG_RUNTIME_DIR}/langmesh"

load_mail_env() {
  local file="" line
  if [[ -n "${LANGMESH_MAIL_ENV:-}" && -f "${LANGMESH_MAIL_ENV}" ]]; then
    file="${LANGMESH_MAIL_ENV}"
  elif [[ -f /run/secrets/mail.env ]]; then
    file=/run/secrets/mail.env
  elif [[ -f /srv/langmesh/mail.env ]]; then
    file=/srv/langmesh/mail.env
  fi
  if [[ -z "${file}" ]]; then
    return 0
  fi
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%$'\r'}"
    case "${line}" in
      ''|'#'*) continue ;;
    esac
    [[ "${line}" == *=* ]] || continue
    export "${line}"
  done <"${file}"
}
load_mail_env

/srv/langmesh/.venv/bin/python - <<'PY'
import os
from langmeshd.commons import configuration_file
from langmeshd.commons.configuration_io import load_configuration

load_configuration(seed=True)
document = configuration_file.load() or {}
sandbox = document.setdefault("sandbox", {})
sandbox["enforce"] = os.environ.get("LANGMESH_SANDBOX_ENFORCE", sandbox.get("enforce") or "preferred")
sandbox["network"] = True
email = document.setdefault("email", {})
email["enabled"] = True
email.setdefault("address", os.environ.get("LANGMESH_MAIL_ADDRESS", ""))
allow = os.environ.get("LANGMESH_MAIL_ALLOW_FROM", "").strip()
if allow:
    email["allow_from"] = [item.strip() for item in allow.split(",") if item.strip()]
email.setdefault("agent", os.environ.get("LANGMESH_MAIL_AGENT", "reviewer") or "reviewer")
email.setdefault("permission_mode", "automatic")
imap = email.setdefault("imap", {})
imap.setdefault("host", os.environ.get("LANGMESH_MAIL_IMAP_HOST", ""))
smtp = email.setdefault("smtp", {})
smtp.setdefault("host", os.environ.get("LANGMESH_MAIL_SMTP_HOST", ""))
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
  /srv/langmesh/.venv/bin/langmeshd &
  daemon_pid=$!
  for _ in $(seq 1 300); do
    if [[ -S "${XDG_RUNTIME_DIR}/langmesh/langmeshd.sock" ]]; then
      return 0
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
