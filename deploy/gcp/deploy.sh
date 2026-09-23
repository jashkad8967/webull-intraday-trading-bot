#!/usr/bin/env bash
set -euo pipefail

DEPLOY_ROOT="/opt/webull-bot"
ARCHIVE="${1:-}"
REVISION="${2:-}"

if [[ ! "${REVISION}" =~ ^[0-9a-f]{7,64}$ ]]; then
  echo "Invalid Git revision." >&2
  exit 1
fi

case "${ARCHIVE}" in
  /tmp/webull-bot-*.tar.gz) ;;
  *)
    echo "Archive must be an uploaded /tmp/webull-bot-*.tar.gz file." >&2
    exit 1
    ;;
esac

if [[ ! -f "${ARCHIVE}" ]]; then
  echo "Release archive not found: ${ARCHIVE}" >&2
  exit 1
fi

if [[ ! -s "${DEPLOY_ROOT}/shared/.env" ]]; then
  echo "Configure ${DEPLOY_ROOT}/shared/.env before deploying." >&2
  exit 1
fi

RELEASE_DIR="${DEPLOY_ROOT}/releases/${REVISION}"
mkdir -p "${RELEASE_DIR}"
tar -xzf "${ARCHIVE}" -C "${RELEASE_DIR}"

if [[ ! -f "${RELEASE_DIR}/Dockerfile" ]] \
  || [[ ! -f "${RELEASE_DIR}/deploy/compose.yaml" ]]; then
  echo "Release is missing Docker deployment files." >&2
  exit 1
fi

PREVIOUS_REVISION=""
if [[ -L "${DEPLOY_ROOT}/current" ]]; then
  PREVIOUS_REVISION="$(basename "$(readlink -f "${DEPLOY_ROOT}/current")")"
fi

cd "${RELEASE_DIR}"
export BOT_IMAGE_TAG="${REVISION}"

# Images are BUILT BY CI and pulled from the registry, never built
# here. Live incident 2026-09-22: `docker compose build` ran on this
# host - 2 cores, 1GB RAM, also running the trading bot and the
# dashboard. It drove load average above 12, starved the trading loop
# so consecutive SCAN cycles were 5-7 MINUTES apart (held positions
# went unmonitored and a filled exit went unnoticed), and still timed
# out: two deploys failed with DeadlineExceeded mid-session, leaving
# merged fixes unshipped.
#
# BOT_IMAGE_REPO/DASHBOARD_IMAGE_REPO are exported by the deploy
# caller. When they are unset the compose defaults are plain local
# tags, and the fallback below builds locally - so a hand-run deploy
# on a machine with no registry access still works.
if [[ -n "${BOT_IMAGE_REPO:-}" ]]; then
  export BOT_IMAGE_REPO DASHBOARD_IMAGE_REPO
  if ! docker compose -f deploy/compose.yaml -p webull-bot pull; then
    echo "Registry pull failed; falling back to a local build." >&2
    docker compose -f deploy/compose.yaml -p webull-bot build
  fi
else
  docker compose -f deploy/compose.yaml -p webull-bot build
fi

if ! docker compose -f deploy/compose.yaml -p webull-bot up -d --remove-orphans; then
  echo "New container failed to start; attempting rollback." >&2
  if [[ -n "${PREVIOUS_REVISION}" ]] \
    && [[ -d "${DEPLOY_ROOT}/releases/${PREVIOUS_REVISION}" ]]; then
    cd "${DEPLOY_ROOT}/releases/${PREVIOUS_REVISION}"
    export BOT_IMAGE_TAG="${PREVIOUS_REVISION}"
    docker compose -f deploy/compose.yaml -p webull-bot up -d --remove-orphans
  fi
  exit 1
fi

sleep 10
if [[ "$(docker inspect --format '{{.State.Running}}' webull-trading-bot 2>/dev/null || true)" != "true" ]]; then
  echo "New container exited during its startup check; attempting rollback." >&2
  if [[ -n "${PREVIOUS_REVISION}" ]] \
    && [[ -d "${DEPLOY_ROOT}/releases/${PREVIOUS_REVISION}" ]]; then
    cd "${DEPLOY_ROOT}/releases/${PREVIOUS_REVISION}"
    export BOT_IMAGE_TAG="${PREVIOUS_REVISION}"
    docker compose -f deploy/compose.yaml -p webull-bot up -d --remove-orphans
  fi
  exit 1
fi

ln -sfn "${RELEASE_DIR}" "${DEPLOY_ROOT}/current.next"
mv -Tf "${DEPLOY_ROOT}/current.next" "${DEPLOY_ROOT}/current"

install -m 0750 \
  "${RELEASE_DIR}/deploy/gcp/deploy.sh" \
  "${DEPLOY_ROOT}/bin/deploy.next"
mv -f "${DEPLOY_ROOT}/bin/deploy.next" "${DEPLOY_ROOT}/bin/deploy"

rm -f -- "${ARCHIVE}"
docker image prune -f >/dev/null

echo "Deployed ${REVISION}; webull-trading-bot is running."
