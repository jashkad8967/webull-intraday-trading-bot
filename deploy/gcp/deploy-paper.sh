#!/usr/bin/env bash
set -euo pipefail

DEPLOY_ROOT="/opt/webull-bot-paper"
ARCHIVE="${1:-}"
REVISION="${2:-}"

if [[ ! "${REVISION}" =~ ^[0-9a-f]{7,64}$ ]]; then
  echo "Invalid Git revision." >&2
  exit 1
fi

case "${ARCHIVE}" in
  /tmp/webull-bot-paper-*.tar.gz) ;;
  *)
    echo "Archive must be an uploaded /tmp/webull-bot-paper-*.tar.gz file." >&2
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
  || [[ ! -f "${RELEASE_DIR}/deploy/compose.paper.yaml" ]]; then
  echo "Release is missing Docker deployment files." >&2
  exit 1
fi

PREVIOUS_REVISION=""
if [[ -L "${DEPLOY_ROOT}/current" ]]; then
  PREVIOUS_REVISION="$(basename "$(readlink -f "${DEPLOY_ROOT}/current")")"
fi

cd "${RELEASE_DIR}"
export BOT_IMAGE_TAG="${REVISION}"
docker compose -f deploy/compose.paper.yaml -p webull-bot-paper build

if ! docker compose -f deploy/compose.paper.yaml -p webull-bot-paper up -d --remove-orphans; then
  echo "New container failed to start; attempting rollback." >&2
  if [[ -n "${PREVIOUS_REVISION}" ]] \
    && [[ -d "${DEPLOY_ROOT}/releases/${PREVIOUS_REVISION}" ]]; then
    cd "${DEPLOY_ROOT}/releases/${PREVIOUS_REVISION}"
    export BOT_IMAGE_TAG="${PREVIOUS_REVISION}"
    docker compose -f deploy/compose.paper.yaml -p webull-bot-paper up -d --remove-orphans
  fi
  exit 1
fi

sleep 10
if [[ "$(docker inspect --format '{{.State.Running}}' webull-trading-bot-paper 2>/dev/null || true)" != "true" ]]; then
  echo "New container exited during its startup check; attempting rollback." >&2
  if [[ -n "${PREVIOUS_REVISION}" ]] \
    && [[ -d "${DEPLOY_ROOT}/releases/${PREVIOUS_REVISION}" ]]; then
    cd "${DEPLOY_ROOT}/releases/${PREVIOUS_REVISION}"
    export BOT_IMAGE_TAG="${PREVIOUS_REVISION}"
    docker compose -f deploy/compose.paper.yaml -p webull-bot-paper up -d --remove-orphans
  fi
  exit 1
fi

ln -sfn "${RELEASE_DIR}" "${DEPLOY_ROOT}/current.next"
mv -Tf "${DEPLOY_ROOT}/current.next" "${DEPLOY_ROOT}/current"

install -m 0750 \
  "${RELEASE_DIR}/deploy/gcp/deploy-paper.sh" \
  "${DEPLOY_ROOT}/bin/deploy.next"
mv -f "${DEPLOY_ROOT}/bin/deploy.next" "${DEPLOY_ROOT}/bin/deploy"

rm -f -- "${ARCHIVE}"
docker image prune -f >/dev/null

echo "Deployed ${REVISION}; webull-trading-bot-paper is running."
