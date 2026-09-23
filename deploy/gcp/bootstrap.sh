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

# Disk guard, independent of deploys.
#
# Live outage 2026-09-23: this host's 8.7G root filled to 99% and
# wedged Docker - the daemon reported "active" while `docker ps` hung
# and `docker ps -a` returned nothing, so the trading bot was DOWN
# mid-session with four open option positions and no stop, no
# profit-lock and no EOD close. The box then became unreachable
# entirely and needed a console reset.
#
# deploy.sh now prunes, but that only helps on days something is
# deployed - which is exactly the wrong thing to depend on, because
# disk fills fastest during a quiet stretch nobody is watching. This
# timer runs daily whether or not anyone ships anything, so the disk
# cannot creep to full between deploys.
install -m 0755 /dev/null /usr/local/bin/webull-disk-cleanup
cat >/usr/local/bin/webull-disk-cleanup <<'CLEANUP'
#!/bin/sh
# Remove images not used by a RUNNING container, plus build cache and
# journals. `docker image prune -a` is safe here because the compose
# stack is always up: anything in use is referenced by a live
# container and is skipped.
set -eu
docker image prune -af --filter "until=48h" >/dev/null 2>&1 || true
docker builder prune -af >/dev/null 2>&1 || true
journalctl --vacuum-size=100M >/dev/null 2>&1 || true
# Release trees older than the newest three.
if [ -d /opt/webull-bot/releases ]; then
  ls -1t /opt/webull-bot/releases 2>/dev/null | tail -n +4 | while read -r old; do
    [ -n "${old}" ] || continue
    case "$(readlink -f /opt/webull-bot/current)" in
      */"${old}") continue ;;
    esac
    rm -rf -- "/opt/webull-bot/releases/${old}"
  done
fi
CLEANUP

cat >/etc/systemd/system/webull-disk-cleanup.service <<'UNIT'
[Unit]
Description=Reclaim disk so the trading host cannot fill up

[Service]
Type=oneshot
ExecStart=/usr/local/bin/webull-disk-cleanup
UNIT

cat >/etc/systemd/system/webull-disk-cleanup.timer <<'UNIT'
[Unit]
Description=Daily disk reclaim for the trading host

[Timer]
# 03:30 local - well clear of the trading session.
OnCalendar=*-*-* 03:30:00
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now webull-disk-cleanup.timer

echo
echo "GCE VM bootstrap complete."
echo "1. Edit ${DEPLOY_ROOT}/shared/.env and enter your secrets."
echo "2. Log out and reconnect so ${DEPLOY_USER} receives Docker group access."
echo "3. Configure the GitHub deployment secrets described in README.md."
