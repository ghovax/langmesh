#!/usr/bin/env bash
# Create a small VPS when a cloud token is already in the environment, then install LangMesh mail on it.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"

log() { printf '%s\n' "$*" >&2; }

env_file() {
  if [[ -n "${LANGMESH_MAIL_ENV:-}" && -f "${LANGMESH_MAIL_ENV}" ]]; then
    printf '%s\n' "${LANGMESH_MAIL_ENV}"
    return 0
  fi
  if [[ -f "${root}/mail.env" ]]; then
    printf '%s\n' "${root}/mail.env"
    return 0
  fi
  if [[ -f "${root}/packaging/mail/mail.env" ]]; then
    printf '%s\n' "${root}/packaging/mail/mail.env"
    return 0
  fi
  return 1
}

load_env() {
  grep -vE '^(#|$)' "$1" | xargs -d '\n'
}

remote_install() {
  local host="$1"
  local file
  log "installing LangMesh mail on ${host}"
  ssh -o StrictHostKeyChecking=accept-new "${host}" "sudo mkdir -p /srv/langmesh"
  tar -C "${root}" --exclude '.venv' --exclude '.git' --exclude 'web/node_modules' --exclude 'web/.next' -cf - . \
    | ssh "${host}" "sudo tar -C /srv/langmesh -xf -"
  if file="$(env_file)"; then
    scp -o StrictHostKeyChecking=accept-new "${file}" "${host}:/tmp/langmesh-mail.env"
    ssh "${host}" "sudo mv /tmp/langmesh-mail.env /srv/langmesh/mail.env && sudo chmod 600 /srv/langmesh/mail.env"
    ssh "${host}" "sudo env $(load_env "${file}") bash /srv/langmesh/packaging/mail/install.sh"
    return
  fi
  log "No mail.env found. Copy packaging/mail/mail.env.example to mail.env, fill it, and rerun"
  log "with LANGMESH_MAIL_ENV pointing at that file, or place mail.env at the checkout root."
  exit 2
}

provision_fly() {
  if ! command -v flyctl >/dev/null 2>&1 && ! command -v fly >/dev/null 2>&1; then
    log "installing flyctl"
    curl -L https://fly.io/install.sh | sh
    export PATH="${HOME}/.fly/bin:${PATH}"
  fi
  local fly file
  fly="$(command -v flyctl || command -v fly)"
  local app="${LANGMESH_FLY_APP:-langmesh-mail}"
  local region="${LANGMESH_FLY_REGION:-iad}"
  "${fly}" apps create "${app}" --generate-name=false || true
  "${fly}" volumes create langmesh_xdg --app "${app}" --size 3 --region "${region}" --yes || true
  if file="$(env_file)"; then
    "${fly}" secrets import --app "${app}" < "${file}"
  fi
  (cd "${root}" && "${fly}" deploy . --app "${app}" \
    --config packaging/mail/fly.toml \
    --dockerfile packaging/mail/Dockerfile \
    --region "${region}")
}

if [[ -n "${LANGMESH_VPS_HOST:-}" ]]; then
  remote_install "${LANGMESH_VPS_HOST}"
  exit 0
fi

if [[ -n "${FLY_API_TOKEN:-}" ]]; then
  provision_fly
  exit 0
fi

if [[ -n "${HCLOUD_TOKEN:-}" ]] && command -v hcloud >/dev/null 2>&1; then
  name="${LANGMESH_VPS_NAME:-langmesh-mail}"
  image="${LANGMESH_HCLOUD_IMAGE:-ubuntu-24.04}"
  type="${LANGMESH_HCLOUD_TYPE:-cpx11}"
  loc="${LANGMESH_HCLOUD_LOCATION:-fsn1}"
  ssh_key="${LANGMESH_HCLOUD_SSH_KEY:-}"
  args=(server create --name "${name}" --type "${type}" --image "${image}" --location "${loc}")
  if [[ -n "${ssh_key}" ]]; then
    args+=(--ssh-key "${ssh_key}")
  fi
  hcloud "${args[@]}"
  ip="$(hcloud server ip "${name}")"
  remote_install "root@${ip}"
  exit 0
fi

if [[ -n "${DIGITALOCEAN_ACCESS_TOKEN:-}" ]] && command -v doctl >/dev/null 2>&1; then
  name="${LANGMESH_VPS_NAME:-langmesh-mail}"
  doctl compute droplet create "${name}" --size s-1vcpu-1gb --image ubuntu-24-04-x64 \
    --region "${LANGMESH_DO_REGION:-nyc1}" --wait
  ip="$(doctl compute droplet get "${name}" --format PublicIPv4 --no-header)"
  remote_install "root@${ip}"
  exit 0
fi

log "No cloud token or LANGMESH_VPS_HOST was set, so no VM was created."
log "Bring any small Linux VPS, copy this checkout there, fill mail.env from"
log "packaging/mail/mail.env.example, and run:"
log "  sudo env \$(grep -vE '^(#|\$)' mail.env | xargs -d '\\n') packaging/mail/install.sh"
exit 2
