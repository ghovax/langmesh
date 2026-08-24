#!/usr/bin/env bash
# Install langmeshd and the IMAP/SMTP client as systemd services on this Linux host.
# Policy is packaging/mail/configuration.yaml. Secrets are files under ./secrets
# (or packaging/mail/secrets), copied onto $XDG_DATA_HOME/langmesh/secrets.
#   sudo packaging/mail/install.sh
#   sudo packaging/mail/install.sh --prefix /opt/langmesh
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
prefix=/srv/langmesh
unit_dir=/etc/systemd/system

log() { printf '%s\n' "$*" >&2; }

usage() {
  log "usage: install.sh [--prefix DIR]"
  log "  --prefix DIR  install directory (default /srv/langmesh)"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix)
      if [[ $# -lt 2 || -z "${2}" || "${2}" == -* ]]; then
        log "--prefix needs a directory"
        exit 2
      fi
      prefix="$2"
      shift 2
      ;;
    --prefix=*)
      prefix="${1#--prefix=}"
      if [[ -z "${prefix}" ]]; then
        log "--prefix needs a directory"
        exit 2
      fi
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      log "unknown argument: $1"
      usage
      exit 2
      ;;
  esac
done

if [[ "${prefix}" != /* ]]; then
  prefix="${PWD}/${prefix}"
fi

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
    local exclude=(--exclude '.venv' --exclude '.git' --exclude 'xdg' --exclude 'mail.env' --exclude 'secrets' --exclude 'web/node_modules' --exclude 'web/.next')
    if command -v rsync >/dev/null 2>&1; then
      rsync -a --delete "${exclude[@]}" "${root}/" "${prefix}/"
    else
      tar -C "${root}" "${exclude[@]}" -cf - . | tar -C "${prefix}" -xf -
    fi
  fi
  mkdir -p \
    "${prefix}/xdg/config/langmesh" \
    "${prefix}/xdg/data/langmesh/secrets" \
    "${prefix}/xdg/state/langmesh" \
    "${prefix}/xdg/cache/langmesh" \
    "${prefix}/xdg/runtime/langmesh"
  chmod 700 "${prefix}/xdg/runtime" "${prefix}/xdg/runtime/langmesh" \
    "${prefix}/xdg/data/langmesh/secrets"
  (
    cd "${prefix}"
    uv sync --no-dev
  )
}

install_secrets() {
  local dest="${prefix}/xdg/data/langmesh/secrets"
  local source=""
  umask 077
  mkdir -p "${dest}"
  chmod 700 "${dest}"
  if [[ -d "${root}/secrets" ]]; then
    source="${root}/secrets"
  elif [[ -d "${PWD}/secrets" ]]; then
    source="${PWD}/secrets"
  elif [[ -d "${root}/packaging/mail/secrets" ]]; then
    source="${root}/packaging/mail/secrets"
  fi
  if [[ -n "${source}" ]]; then
    local file
    for file in "${source}"/*; do
      [[ -f "${file}" ]] || continue
      case "$(basename "${file}")" in
        README|README.md) continue ;;
      esac
      install -m 600 "${file}" "${dest}/$(basename "${file}")"
    done
    log "installed secrets from ${source}"
  fi
}

install_policy() {
  local dest="${prefix}/xdg/config/langmesh/configuration.yaml"
  local source=""
  if [[ -f "${root}/packaging/mail/configuration.yaml" && ! -f "${dest}" ]]; then
    source="${root}/packaging/mail/configuration.yaml"
  elif [[ -f "${prefix}/packaging/mail/configuration.yaml" && ! -f "${dest}" ]]; then
    source="${prefix}/packaging/mail/configuration.yaml"
  fi
  if [[ -n "${source}" ]]; then
    install -m 600 "${source}" "${dest}"
    log "installed ${dest} from ${source}"
  fi
}

write_configuration() {
  export XDG_CONFIG_HOME="${prefix}/xdg/config"
  export XDG_DATA_HOME="${prefix}/xdg/data"
  export XDG_STATE_HOME="${prefix}/xdg/state"
  export XDG_CACHE_HOME="${prefix}/xdg/cache"
  export XDG_RUNTIME_DIR="${prefix}/xdg/runtime"
  "${prefix}/.venv/bin/python" - "${prefix}" <<'PY'
import os
import sys
from pathlib import Path

from langmeshd.commons import configuration_file
from langmeshd.commons.configuration_io import load_configuration
from langmeshd.commons.secret_import import import_into_files

prefix = sys.argv[1]
load_configuration(seed=True)
import_into_files()
document = configuration_file.load() or {}
sandbox = document.setdefault("sandbox", {})
sandbox["enforce"] = sandbox.get("enforce") or "preferred"
sandbox["network"] = True
email = document.setdefault("email", {})
email.setdefault("agent", "reviewer")
email.setdefault("working_directory", prefix)
email.setdefault("permission_mode", "automatic")
if email.get("address"):
    email["enabled"] = True
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
install_policy
install_secrets
write_configuration
install_units
log "langmeshd and langmesh-mail are enabled. Send mail to local+machine@domain from an allowlisted From."
