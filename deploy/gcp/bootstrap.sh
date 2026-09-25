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

# Swap cushion. This instance has 955MB of RAM and shipped with NO
# swap, so anything that spikes memory has nowhere to go but the OOM
# killer and the page-thrash that precedes it.
#
# Live 2026-09-24, mid-session with two positions open: a scheduled
# unattended apt upgrade pushed available memory to 34MB. Load average
# hit 32.59, systemd-timesyncd could not get enough CPU to hold the
# clock, and it drifted 8 seconds - past Webull's request-timestamp
# window, so EVERY api call started failing with
# CLOCK_SKEW_EXCEEDED. The bot was up and healthy and simply could not
# place an order, including exits, with 35 minutes left before the
# option close.
#
# 512M rather than the conventional 1G: the deploy preflight refuses
# to run under 1500MB free disk, and this host's root is 8.7G.
if [[ ! -f /swapfile ]]; then
  fallocate -l 512M /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q "^/swapfile" /etc/fstab     || echo "/swapfile none swap sw 0 0" >>/etc/fstab
fi

docker volume create webull-trading-data >/dev/null

# Disk guard, independent of deploys. See install-disk-cleanup.sh for
# why this exists (2026-09-23: root filled to 99%, Docker wedged, bot
# DOWN mid-session holding live positions, machine needed a console
# reset). Kept in its own script so it can also be installed on an
# EXISTING host, which is exactly what that outage needed and what
# bootstrap-only code could not provide.
if [[ -f "${SCRIPT_DIR}/install-disk-cleanup.sh" ]]; then
  bash "${SCRIPT_DIR}/install-disk-cleanup.sh"
fi

echo
echo "GCE VM bootstrap complete."
echo "1. Edit ${DEPLOY_ROOT}/shared/.env and enter your secrets."
echo "2. Log out and reconnect so ${DEPLOY_USER} receives Docker group access."
echo "3. Configure the GitHub deployment secrets described in README.md."
