#!/usr/bin/env bash
# Create a small VPS when a cloud token is already in the environment, then install LangMesh mail on it.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"

log() { printf '%s\n' "$*" >&2; }

policy_file() {
  if [[ -n "${LANGMESH_CONFIG:-}" && -f "${LANGMESH_CONFIG}" ]]; then
    printf '%s\n' "${LANGMESH_CONFIG}"
    return 0
  fi
  if [[ -f "${root}/packaging/mail/configuration.yaml" ]]; then
    printf '%s\n' "${root}/packaging/mail/configuration.yaml"
    return 0
  fi
  return 1
}

secrets_dir() {
  if [[ -n "${LANGMESH_SECRETS:-}" && -d "${LANGMESH_SECRETS}" ]]; then
    printf '%s\n' "${LANGMESH_SECRETS}"
    return 0
  fi
  if [[ -d "${root}/secrets" ]]; then
    printf '%s\n' "${root}/secrets"
    return 0
  fi
  return 1
}

leftover_mail_env() {
  if [[ -n "${LANGMESH_MAIL_ENV:-}" && -f "${LANGMESH_MAIL_ENV}" ]]; then
    printf '%s\n' "${LANGMESH_MAIL_ENV}"
    return 0
  fi
  if [[ -f "${root}/mail.env" ]]; then
    printf '%s\n' "${root}/mail.env"
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
  local config secrets envf
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
  extra=()
  if envf="$(leftover_mail_env)"; then
    scp -o StrictHostKeyChecking=accept-new "${envf}" "${host}:/tmp/langmesh-mail.env"
    ssh "${host}" "sudo mv /tmp/langmesh-mail.env /srv/langmesh/mail.env && sudo chmod 600 /srv/langmesh/mail.env"
    extra+=(LANGMESH_MAIL_ENV=/srv/langmesh/mail.env)
  fi
  ssh "${host}" "sudo env ${extra[*]+"${extra[*]}"} bash /srv/langmesh/packaging/mail/install.sh"
}

provision_fly() {
  if ! command -v flyctl >/dev/null 2>&1 && ! command -v fly >/dev/null 2>&1; then
    log "installing flyctl"
    curl -L https://fly.io/install.sh | sh
    export PATH="${HOME}/.fly/bin:${PATH}"
  fi
  local fly envf
  fly="$(command -v flyctl || command -v fly)"
  local app="${LANGMESH_FLY_APP:-langmesh-mail}"
  local region="${LANGMESH_FLY_REGION:-iad}"
  "${fly}" apps create "${app}" --generate-name=false || true
  "${fly}" volumes create langmesh_xdg --app "${app}" --size 3 --region "${region}" --yes || true
  if envf="$(leftover_mail_env)"; then
    grep -vE '^(#|$)' "${envf}" | grep -vE '^[^=]+=[[:space:]]*$' | "${fly}" secrets import --app "${app}"
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
  if [[ -z "${ssh_key}" ]]; then
    log "Set LANGMESH_HCLOUD_SSH_KEY to an hcloud SSH key name so the server can be installed over SSH."
    exit 2
  fi
  args=(server create --name "${name}" --type "${type}" --image "${image}" --location "${loc}" --ssh-key "${ssh_key}")
  hcloud "${args[@]}"
  ip="$(hcloud server ip "${name}")"
  remote_install "root@${ip}"
  exit 0
fi

if [[ -n "${DIGITALOCEAN_ACCESS_TOKEN:-}" ]] && command -v doctl >/dev/null 2>&1; then
  name="${LANGMESH_VPS_NAME:-langmesh-mail}"
  ssh_key="${LANGMESH_DO_SSH_KEY:-}"
  if [[ -z "${ssh_key}" ]]; then
    log "Set LANGMESH_DO_SSH_KEY to a DigitalOcean SSH key fingerprint or id so the droplet can be installed over SSH."
    exit 2
  fi
  doctl compute droplet create "${name}" --size s-1vcpu-1gb --image ubuntu-24-04-x64 \
    --region "${LANGMESH_DO_REGION:-nyc1}" --ssh-keys "${ssh_key}" --wait
  ip="$(doctl compute droplet get "${name}" --format PublicIPv4 --no-header)"
  remote_install "root@${ip}"
  exit 0
fi

log "No cloud token or LANGMESH_VPS_HOST was set, so no VM was created."
log "Bring any small Linux VPS, copy this checkout there, fill packaging/mail/configuration.yaml"
log "and a secrets directory, and run:"
log "  sudo env LANGMESH_CONFIG=\"\$PWD/packaging/mail/configuration.yaml\" LANGMESH_SECRETS=\"\$PWD/secrets\" packaging/mail/install.sh"
exit 2
