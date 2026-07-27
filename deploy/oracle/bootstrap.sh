#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run with sudo: sudo bash deploy/oracle/bootstrap.sh" >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
DEPLOY_ROOT="/opt/webull-bot"
DEPLOY_USER="${SUDO_USER:-ubuntu}"

if ! id "${DEPLOY_USER}" >/dev/null 2>&1; then
  echo "Deployment user does not exist: ${DEPLOY_USER}" >&2
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
WEBULL_API_ENDPOINT=api.webull.com
WEBULL_ENVIRONMENT=prod
WEBULL_REGION_ID=us
LIVE_TRADING_ENABLED=true

STOCK_SYMBOLS=ALL
MAX_SYMBOLS=0
OPTION_CONTRACTS=
OPTION_UNDERLYINGS=

AGENT_ENABLED=true
GROQ_API_KEY=
GROQ_MODEL=groq/compound-mini

TRADING_TIMEZONE=America/New_York
LOG_DIRECTORY=/var/data/logs
WASH_SALE_STATE_FILE=/var/data/conf/wash_sale_blocks.json
EOF
  chown "${DEPLOY_USER}:${DEPLOY_USER}" "${DEPLOY_ROOT}/shared/.env"
fi

docker volume create webull-trading-data >/dev/null

echo
echo "Oracle VM bootstrap complete."
echo "1. Edit ${DEPLOY_ROOT}/shared/.env and enter your secrets."
echo "2. Log out and reconnect so ${DEPLOY_USER} receives Docker group access."
echo "3. Configure the GitHub deployment secrets described in README.md."
