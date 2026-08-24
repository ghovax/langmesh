#!/usr/bin/env bash
# Install langmeshd and the IMAP/SMTP client as systemd services on this Linux host.
# Point LANGMESH_MAIL_ENV at a filled mail.env so the file is copied intact:
#   sudo env LANGMESH_MAIL_ENV="$PWD/mail.env" packaging/mail/install.sh
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
prefix="${LANGMESH_PREFIX:-/srv/langmesh}"
unit_dir="${LANGMESH_SYSTEMD_DIR:-/etc/systemd/system}"

log() { printf '%s\n' "$*" >&2; }

need_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    log "install.sh must run as root so it can write systemd units under ${unit_dir}."
    exit 1
  fi
}

install_uv() {
  if command -v uv >/dev/null 2>&1; then
    return
  fi
  log "installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
}

sync_checkout() {
  mkdir -p "${prefix}"
  if [[ "${root}" != "${prefix}" ]]; then
    log "syncing checkout into ${prefix}"
    # Never replace xdg or mail.env: those are the job queue, history, and secrets.
    local exclude=(--exclude '.venv' --exclude '.git' --exclude 'xdg' --exclude 'mail.env' --exclude 'web/node_modules' --exclude 'web/.next')
    if command -v rsync >/dev/null 2>&1; then
      rsync -a --delete "${exclude[@]}" "${root}/" "${prefix}/"
    else
      tar -C "${root}" "${exclude[@]}" -cf - . | tar -C "${prefix}" -xf -
    fi
  fi
  mkdir -p \
    "${prefix}/xdg/config/langmesh" \
    "${prefix}/xdg/data/langmesh" \
    "${prefix}/xdg/state/langmesh" \
    "${prefix}/xdg/cache/langmesh" \
    "${prefix}/xdg/runtime/langmesh"
  chmod 700 "${prefix}/xdg/runtime" "${prefix}/xdg/runtime/langmesh"
  (
    cd "${prefix}"
    uv sync --no-dev
  )
}

write_env_file() {
  local env_file="${prefix}/mail.env"
  local source=""
  umask 077
  if [[ -n "${LANGMESH_MAIL_ENV:-}" && -f "${LANGMESH_MAIL_ENV}" ]]; then
    source="${LANGMESH_MAIL_ENV}"
  elif [[ -f "${root}/mail.env" ]]; then
    source="${root}/mail.env"
  elif [[ -f "${PWD}/mail.env" ]]; then
    source="${PWD}/mail.env"
  fi
  if [[ -n "${source}" ]]; then
    if [[ "${source}" != "${env_file}" ]]; then
      cp "${source}" "${env_file}"
    fi
    chmod 600 "${env_file}"
    log "installed ${env_file} from ${source}"
    return
  fi
  if [[ -n "${LANGMESH_MAIL_ADDRESS:-}${LANGMESH_MAIL_PASSWORD:-}${LANGMESH_MAIL_ALLOW_FROM:-}${LANGMESH_MAIL_IMAP_PASSWORD:-}${LANGMESH_MAIL_SMTP_PASSWORD:-}" ]]; then
    env | grep -E '^(LANGMESH_MAIL_|LANGMESH_API_KEY=|LANGMESH_SANDBOX_|[A-Z][A-Z0-9_]*_API_KEY=)' >"${env_file}" || true
    chmod 600 "${env_file}"
    log "wrote ${env_file} from the environment (mode 0600)"
    return
  fi
  if [[ -f "${env_file}" ]]; then
    log "keeping existing ${env_file}"
    return
  fi
  cat >"${env_file}" <<EOF
LANGMESH_MAIL_ADDRESS=${LANGMESH_MAIL_ADDRESS:-}
LANGMESH_MAIL_ALLOW_FROM=${LANGMESH_MAIL_ALLOW_FROM:-}
LANGMESH_MAIL_AGENT=${LANGMESH_MAIL_AGENT:-reviewer}
LANGMESH_MAIL_PASSWORD=${LANGMESH_MAIL_PASSWORD:-}
OPENCODE_API_KEY=${OPENCODE_API_KEY:-}
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
OPENAI_API_KEY=${OPENAI_API_KEY:-}
OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}
EOF
  chmod 600 "${env_file}"
  log "wrote ${env_file} (mode 0600)"
}

write_configuration() {
  export LANGMESH_PREFIX="${prefix}"
  export XDG_CONFIG_HOME="${prefix}/xdg/config"
  export XDG_DATA_HOME="${prefix}/xdg/data"
  export XDG_STATE_HOME="${prefix}/xdg/state"
  export XDG_CACHE_HOME="${prefix}/xdg/cache"
  export XDG_RUNTIME_DIR="${prefix}/xdg/runtime"
  "${prefix}/.venv/bin/python" - <<'PY'
import os
from pathlib import Path

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
email.setdefault(
    "working_directory",
    os.environ.get("LANGMESH_MAIL_WORKING_DIRECTORY")
    or os.environ.get("LANGMESH_PREFIX")
    or "/srv/langmesh",
)
email.setdefault("permission_mode", "automatic")
imap = email.setdefault("imap", {})
imap.setdefault("host", os.environ.get("LANGMESH_MAIL_IMAP_HOST", ""))
imap.setdefault("username", os.environ.get("LANGMESH_MAIL_IMAP_USER", ""))
smtp = email.setdefault("smtp", {})
smtp.setdefault("host", os.environ.get("LANGMESH_MAIL_SMTP_HOST", ""))
smtp.setdefault("username", os.environ.get("LANGMESH_MAIL_SMTP_USER", ""))
invalid = configuration_file.rejects(document)
if invalid:
    raise SystemExit(f"invalid configuration: {invalid}")
configuration_file.save(document)
path = Path(os.environ["XDG_CONFIG_HOME"]) / "langmesh" / "configuration.yaml"
os.chmod(path, 0o600)
print(f"wrote {path}")
PY
}

install_units() {
  local src="${root}/packaging/mail"
  if [[ "${root}" != "${prefix}" ]]; then
    src="${prefix}/packaging/mail"
  fi
  sed "s|/srv/langmesh|${prefix}|g" "${src}/langmeshd.service" >"${unit_dir}/langmeshd.service"
  sed "s|/srv/langmesh|${prefix}|g" "${src}/langmesh-mail.service" >"${unit_dir}/langmesh-mail.service"
  systemctl daemon-reload
  systemctl enable --now langmeshd.service
  systemctl enable --now langmesh-mail.service
  systemctl --no-pager --full status langmeshd.service langmesh-mail.service || true
}

need_root
install_uv
sync_checkout
write_env_file
write_configuration
install_units
log "langmeshd and langmesh-mail are enabled. Send mail to LANGMESH_MAIL_ADDRESS from an allowlisted From."
