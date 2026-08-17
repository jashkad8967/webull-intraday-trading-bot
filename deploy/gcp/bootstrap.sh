#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run with sudo: sudo bash deploy/gcp/bootstrap.sh" >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
DEPLOY_ROOT="/opt/webull-bot"
DEPLOY_USER="${SUDO_USER:-}"

if [[ -z "${DEPLOY_USER}" ]] || ! id "${DEPLOY_USER}" >/dev/null 2>&1; then
  echo "Run this with 'sudo bash deploy/gcp/bootstrap.sh' as your normal" >&2
  echo "GCE login user (not directly as root), so that user can be found" >&2
  echo "and added to the docker group." >&2
  exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2
systemctl enable --now docker
usermod -aG docker "${DEPLOY_USER}"

install -d -m 0750 -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" \
  "${DEPLOY_ROOT}" \
  "${DEPLOY_ROOT}/bin" \
  "${DEPLOY_ROOT}/releases" \
  "${DEPLOY_ROOT}/shared"

install -m 0750 -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" \
  "${SCRIPT_DIR}/deploy.sh" \
  "${DEPLOY_ROOT}/bin/deploy"

if [[ ! -f "${DEPLOY_ROOT}/shared/.env" ]]; then
  install -m 0600 -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" \
    /dev/null "${DEPLOY_ROOT}/shared/.env"
  cat >"${DEPLOY_ROOT}/shared/.env" <<'EOF'
MODE=LIVE
WEBULL_APP_KEY=
WEBULL_APP_SECRET=
ACCOUNT_ID=
LIVE_TRADING_ENABLED=true
GROQ_API_KEY=
EOF
  chown "${DEPLOY_USER}:${DEPLOY_USER}" "${DEPLOY_ROOT}/shared/.env"
fi

docker volume create webull-trading-data >/dev/null

echo
echo "GCE VM bootstrap complete."
echo "1. Edit ${DEPLOY_ROOT}/shared/.env and enter your secrets."
echo "2. Log out and reconnect so ${DEPLOY_USER} receives Docker group access."
echo "3. Configure the GitHub deployment secrets described in README.md."
