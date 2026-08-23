#!/usr/bin/env bash
# Create a small VPS when a cloud token is already in the environment, then install LangMesh mail on it.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"

log() { printf '%s\n' "$*" >&2; }

remote_install() {
  local host="$1"
  log "installing LangMesh mail on ${host}"
  ssh -o StrictHostKeyChecking=accept-new "${host}" "sudo mkdir -p /srv/langmesh"
  tar -C "${root}" --exclude '.venv' --exclude '.git' --exclude 'web/node_modules' --exclude 'web/.next' -cf - . \
    | ssh "${host}" "sudo tar -C /srv/langmesh -xf -"
  ssh "${host}" "sudo env \
    LANGMESH_MAIL_ADDRESS='${LANGMESH_MAIL_ADDRESS:-}' \
    LANGMESH_MAIL_ALLOW_FROM='${LANGMESH_MAIL_ALLOW_FROM:-}' \
    LANGMESH_MAIL_AGENT='${LANGMESH_MAIL_AGENT:-reviewer}' \
    LANGMESH_MAIL_IMAP_HOST='${LANGMESH_MAIL_IMAP_HOST:-}' \
    LANGMESH_MAIL_IMAP_USER='${LANGMESH_MAIL_IMAP_USER:-}' \
    LANGMESH_MAIL_IMAP_PASSWORD='${LANGMESH_MAIL_IMAP_PASSWORD:-${LANGMESH_MAIL_PASSWORD:-}}' \
    LANGMESH_MAIL_SMTP_HOST='${LANGMESH_MAIL_SMTP_HOST:-}' \
    LANGMESH_MAIL_SMTP_USER='${LANGMESH_MAIL_SMTP_USER:-}' \
    LANGMESH_MAIL_SMTP_PASSWORD='${LANGMESH_MAIL_SMTP_PASSWORD:-${LANGMESH_MAIL_PASSWORD:-}}' \
    OPENROUTER_API_KEY='${OPENROUTER_API_KEY:-}' \
    ANTHROPIC_API_KEY='${ANTHROPIC_API_KEY:-}' \
    OPENAI_API_KEY='${OPENAI_API_KEY:-}' \
    OPENCODE_API_KEY='${OPENCODE_API_KEY:-}' \
    LANGMESH_API_KEY='${LANGMESH_API_KEY:-}' \
    bash /srv/langmesh/packaging/mail/install.sh"
}

provision_fly() {
  if ! command -v flyctl >/dev/null 2>&1 && ! command -v fly >/dev/null 2>&1; then
    log "installing flyctl"
    curl -L https://fly.io/install.sh | sh
    export PATH="${HOME}/.fly/bin:${PATH}"
  fi
  local fly
  fly="$(command -v flyctl || command -v fly)"
  local app="${LANGMESH_FLY_APP:-langmesh-mail}"
  local region="${LANGMESH_FLY_REGION:-iad}"
  "${fly}" apps create "${app}" --generate-name=false || true
  "${fly}" volumes create langmesh_xdg --app "${app}" --size 3 --region "${region}" --yes || true
  if [[ -n "${LANGMESH_MAIL_ADDRESS:-}${LANGMESH_MAIL_PASSWORD:-}${LANGMESH_MAIL_IMAP_PASSWORD:-}" ]]; then
    "${fly}" secrets set --app "${app}" \
      LANGMESH_MAIL_ADDRESS="${LANGMESH_MAIL_ADDRESS:-}" \
      LANGMESH_MAIL_ALLOW_FROM="${LANGMESH_MAIL_ALLOW_FROM:-}" \
      LANGMESH_MAIL_AGENT="${LANGMESH_MAIL_AGENT:-reviewer}" \
      LANGMESH_MAIL_IMAP_HOST="${LANGMESH_MAIL_IMAP_HOST:-}" \
      LANGMESH_MAIL_SMTP_HOST="${LANGMESH_MAIL_SMTP_HOST:-}" \
      LANGMESH_MAIL_PASSWORD="${LANGMESH_MAIL_IMAP_PASSWORD:-${LANGMESH_MAIL_PASSWORD:-}}" \
      OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}" \
      ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}" \
      OPENAI_API_KEY="${OPENAI_API_KEY:-}" \
      OPENCODE_API_KEY="${OPENCODE_API_KEY:-}"
  fi
  "${fly}" deploy --app "${app}" --config "${root}/packaging/mail/fly.toml" \
    --dockerfile "${root}/packaging/mail/Dockerfile" --region "${region}"
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
log "Bring any small Linux VPS (Oracle Always Free, Hetzner CX22, a leftover droplet),"
log "copy this checkout there, and run: sudo packaging/mail/install.sh"
log "Needed on that host: LANGMESH_MAIL_ADDRESS, LANGMESH_MAIL_ALLOW_FROM,"
log "LANGMESH_MAIL_IMAP_HOST, LANGMESH_MAIL_IMAP_PASSWORD, LANGMESH_MAIL_SMTP_HOST,"
log "LANGMESH_MAIL_AGENT (defaults to reviewer), and a provider API key."
exit 2
