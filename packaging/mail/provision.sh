#!/usr/bin/env bash
# Create a small VPS when a cloud token is already in the environment, then install LangMesh mail on it.
# Target and create fields come from packaging/mail/configuration.yaml (provision.*).
# Cloud CLIs still read their own tokens: FLY_API_TOKEN, HCLOUD_TOKEN, DIGITALOCEAN_ACCESS_TOKEN.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
policy="${root}/packaging/mail/configuration.yaml"

log() { printf '%s\n' "$*" >&2; }

provision_get() {
  local dotted="$1"
  (cd "${root}" && uv run python - "${policy}" "${dotted}" <<'PY'
import sys
from pathlib import Path

import yaml

document = yaml.safe_load(Path(sys.argv[1]).read_text()) or {}
node = document.get("provision") or {}
for part in sys.argv[2].split("."):
    if not isinstance(node, dict):
        print("")
        raise SystemExit(0)
    node = node.get(part)
    if node is None or node is False:
        print("")
        raise SystemExit(0)
if isinstance(node, (dict, list)):
    print("")
else:
    print(node)
PY
  )
}

policy_file() {
  if [[ -f "${policy}" ]]; then
    printf '%s\n' "${policy}"
    return 0
  fi
  return 1
}

secrets_dir() {
  if [[ -d "${root}/secrets" ]]; then
    printf '%s\n' "${root}/secrets"
    return 0
  fi
  return 1
}

remote_install() {
  local host="$1"
  log "waiting for SSH on ${host}"
  local n=0
  until ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 "${host}" true; do
    n=$((n + 1))
    if (( n >= 36 )); then
      log "could not SSH to ${host}"
      exit 1
    fi
    sleep 5
  done
  log "installing LangMesh mail on ${host}"
  ssh -o StrictHostKeyChecking=accept-new "${host}" "sudo mkdir -p /srv/langmesh"
  tar -C "${root}" --exclude '.venv' --exclude '.git' --exclude 'xdg' --exclude 'mail.env' \
    --exclude 'secrets' --exclude 'web/node_modules' --exclude 'web/.next' -cf - . \
    | ssh "${host}" "sudo tar -C /srv/langmesh -xf -"
  local config secrets
  if config="$(policy_file)"; then
    scp -o StrictHostKeyChecking=accept-new "${config}" "${host}:/tmp/langmesh-configuration.yaml"
    ssh "${host}" "sudo mkdir -p /srv/langmesh/xdg/config/langmesh && sudo mv /tmp/langmesh-configuration.yaml /srv/langmesh/xdg/config/langmesh/configuration.yaml && sudo chmod 600 /srv/langmesh/xdg/config/langmesh/configuration.yaml"
  fi
  if secrets="$(secrets_dir)"; then
    ssh "${host}" "sudo mkdir -p /srv/langmesh/xdg/data/langmesh/secrets && sudo chmod 700 /srv/langmesh/xdg/data/langmesh/secrets"
    scp -o StrictHostKeyChecking=accept-new "${secrets}"/* "${host}:/tmp/langmesh-secrets/" 2>/dev/null || true
    ssh "${host}" "sudo mkdir -p /tmp/langmesh-secrets; true"
    scp -o StrictHostKeyChecking=accept-new -r "${secrets}/." "${host}:/tmp/langmesh-secrets/"
    ssh "${host}" "sudo find /tmp/langmesh-secrets -maxdepth 1 -type f ! -name 'README' ! -name 'README.md' -exec install -m 600 {} /srv/langmesh/xdg/data/langmesh/secrets/ \\; && sudo rm -rf /tmp/langmesh-secrets"
  fi
  ssh "${host}" "sudo bash /srv/langmesh/packaging/mail/install.sh"
}

provision_fly() {
  if ! command -v flyctl >/dev/null 2>&1 && ! command -v fly >/dev/null 2>&1; then
    log "installing flyctl"
    curl -L https://fly.io/install.sh | sh
    export PATH="${HOME}/.fly/bin:${PATH}"
  fi
  local fly
  fly="$(command -v flyctl || command -v fly)"
  local app region
  app="$(provision_get fly.app)"
  app="${app:-langmesh-mail}"
  region="$(provision_get fly.region)"
  region="${region:-iad}"
  "${fly}" apps create "${app}" --generate-name=false || true
  "${fly}" volumes create langmesh_xdg --app "${app}" --size 3 --region "${region}" --yes || true
  (cd "${root}" && "${fly}" deploy . --app "${app}" \
    --config packaging/mail/fly.toml \
    --dockerfile packaging/mail/Dockerfile \
    --region "${region}")
}

if [[ ! -f "${policy}" ]]; then
  log "missing ${policy}"
  exit 2
fi

host="$(provision_get host)"
if [[ -n "${host}" ]]; then
  remote_install "${host}"
  exit 0
fi

if [[ -n "${FLY_API_TOKEN:-}" ]]; then
  provision_fly
  exit 0
fi

name="$(provision_get name)"
name="${name:-langmesh-mail}"

if [[ -n "${HCLOUD_TOKEN:-}" ]] && command -v hcloud >/dev/null 2>&1; then
  local_image="$(provision_get hetzner.image)"
  local_type="$(provision_get hetzner.type)"
  local_loc="$(provision_get hetzner.location)"
  ssh_key="$(provision_get hetzner.ssh_key)"
  local_image="${local_image:-ubuntu-24.04}"
  local_type="${local_type:-cpx11}"
  local_loc="${local_loc:-fsn1}"
  if [[ -z "${ssh_key}" ]]; then
    log "Set provision.hetzner.ssh_key in packaging/mail/configuration.yaml to an hcloud SSH key name so the server can be installed over SSH."
    exit 2
  fi
  args=(server create --name "${name}" --type "${local_type}" --image "${local_image}" --location "${local_loc}" --ssh-key "${ssh_key}")
  hcloud "${args[@]}"
  ip="$(hcloud server ip "${name}")"
  remote_install "root@${ip}"
  exit 0
fi

if [[ -n "${DIGITALOCEAN_ACCESS_TOKEN:-}" ]] && command -v doctl >/dev/null 2>&1; then
  ssh_key="$(provision_get digitalocean.ssh_key)"
  do_region="$(provision_get digitalocean.region)"
  do_region="${do_region:-nyc1}"
  if [[ -z "${ssh_key}" ]]; then
    log "Set provision.digitalocean.ssh_key in packaging/mail/configuration.yaml to a DigitalOcean SSH key fingerprint or id so the droplet can be installed over SSH."
    exit 2
  fi
  doctl compute droplet create "${name}" --size s-1vcpu-1gb --image ubuntu-24-04-x64 \
    --region "${do_region}" --ssh-keys "${ssh_key}" --wait
  ip="$(doctl compute droplet get "${name}" --format PublicIPv4 --no-header)"
  remote_install "root@${ip}"
  exit 0
fi

log "No provision.host in packaging/mail/configuration.yaml, and no cloud token was set, so no VM was created."
log "Fill provision.host (SSH into a machine you already have), or set FLY_API_TOKEN / HCLOUD_TOKEN / DIGITALOCEAN_ACCESS_TOKEN."
log "Bring any small Linux VPS, copy this checkout there, fill packaging/mail/configuration.yaml"
log "and a secrets directory, and run:"
log "  sudo packaging/mail/install.sh"
exit 2
