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

# Preflight disk check. Pulling two images and extracting a release
# tree needs room; starting that on an almost-full disk is what took
# this host down on 2026-09-23 (99% full -> Docker daemon wedged with
# `docker ps` hanging -> bot DOWN mid-session -> load average 20 and
# sshd unable to complete a banner exchange, needing a console reset).
#
# Failing here is strictly better than that: the currently running
# container keeps trading untouched, and the error says exactly what
# is wrong while the machine is still reachable.
AVAILABLE_MB="$(df -Pm "${DEPLOY_ROOT}" | awk 'NR==2 {print $4}')"
if [[ "${AVAILABLE_MB}" -lt 1500 ]]; then
  echo "Refusing to deploy: only ${AVAILABLE_MB}MB free on ${DEPLOY_ROOT}." >&2
  echo "The running container is untouched. Reclaim space first:" >&2
  echo "  docker images   # then: docker rmi <old revision tags>" >&2
  echo "  sudo du -xhd1 /opt/webull-bot/releases | sort -rh | head" >&2
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
# BOT_IMAGE_REPO is exported by the deploy caller. When it is unset the
# compose default is a plain local tag and the fallback below builds
# locally - so a hand-run deploy on a machine with no registry access
# still works.
if [[ -n "${BOT_IMAGE_REPO:-}" ]]; then
  export BOT_IMAGE_REPO
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

# Reclaim disk from superseded deploys.
#
# Live outage 2026-09-23: this host filled to 99% (124M free on an
# 8.7G root) and wedged Docker - the daemon still reported "active"
# but `docker ps` hung and `docker ps -a` returned nothing, so the
# trading bot was DOWN mid-session with four open option positions
# and no stop, no profit-lock and no EOD close. The box then went
# unreachable entirely: at load average 20 sshd could not finish a
# banner exchange, and recovery needed a console reset.
#
# `docker image prune -f` alone caused it. That only removes DANGLING
# images - untagged, unreferenced layers. Every deploy pulls
# ghcr.io/.../trader:<sha>, which is fully
# TAGGED and therefore never dangling, so each one stayed on disk
# forever. On a 2-core/1GB/8.7G instance a few weeks of deploys is
# all it takes.
#
# Release trees leak the same way: each deploy extracts a full source
# checkout into releases/<sha> and nothing ever removed the old ones.
#
# Both are pruned by RETENTION rather than age, and both deliberately
# keep the current revision AND the previous one, because the failure
# path above rolls back to PREVIOUS_REVISION - pruning it would turn
# a failed deploy into an outage instead of a rollback.
prune_images() {
  local repo="$1"
  [[ -n "${repo}" ]] || return 0
  docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' \
    | awk -v repo="${repo}" '$1 ~ "^"repo":" {print $1}' \
    | grep -v ":${REVISION}$" \
    | { [[ -n "${PREVIOUS_REVISION}" ]] \
        && grep -v ":${PREVIOUS_REVISION}$" || cat; } \
    | xargs -r -n1 docker rmi -f >/dev/null 2>&1 || true
}
prune_images "${BOT_IMAGE_REPO:-}"
# The dashboard image is gone (merged into the trader container), so
# sweep every tag left over from before that merge - none of them is
# the current or previous revision of anything that still runs.
if [[ -n "${BOT_IMAGE_REPO:-}" ]]; then
  docker images --format '{{.Repository}}:{{.Tag}}' \
    | grep -E "(^|/)(dashboard|webull-trading-dashboard):" \
    | xargs -r -n1 docker rmi -f >/dev/null 2>&1 || true
fi
docker image prune -f >/dev/null 2>&1 || true

# Keep the newest few release trees (current and previous are always
# among them; ls -t orders by mtime, newest first).
if [[ -d "${DEPLOY_ROOT}/releases" ]]; then
  ls -1t "${DEPLOY_ROOT}/releases" 2>/dev/null \
    | tail -n +4 \
    | while read -r old; do
        [[ "${old}" == "${REVISION}" ]] && continue
        [[ "${old}" == "${PREVIOUS_REVISION}" ]] && continue
        rm -rf -- "${DEPLOY_ROOT:?}/releases/${old}"
      done
fi

df -h "${DEPLOY_ROOT}" | tail -1

echo "Deployed ${REVISION}; webull-trading-bot is running."
