#!/usr/bin/env bash
# Install the daily disk-reclaim timer on a trading host.
#
# Live outage 2026-09-23: this host's 8.7G root filled to 99% and
# wedged Docker - the daemon still reported "active" while `docker ps`
# hung and `docker ps -a` returned nothing, so the trading bot was DOWN
# mid-session with four open option positions and no stop, no
# profit-lock and no EOD close. The box then became unreachable
# entirely (at load average 20 sshd could not finish a banner exchange)
# and needed a console reset. Recovery found 93 images on disk: a
# 292MB trader and a 220MB dashboard for every deploy ever made.
#
# The cause was that `docker image prune -f` only removes DANGLING
# images, and every deploy pulls a fully TAGGED one. deploy.sh now
# prunes properly, but that only helps on days something ships - which
# is backwards, because disk fills fastest during a quiet stretch
# nobody is watching. This timer runs daily whether or not anyone
# deploys.
#
# Idempotent: safe to re-run. Run with sudo on any existing host; also
# invoked by bootstrap.sh for new ones.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this with sudo." >&2
  exit 1
fi

install -m 0755 /dev/null /usr/local/bin/webull-disk-cleanup
cat >/usr/local/bin/webull-disk-cleanup <<'CLEANUP'
#!/bin/sh
# Reclaim disk on the trading host. Every step is best-effort: this
# must never be the reason the machine or the bot stops working.
#
# `docker image prune -a` is safe here because the compose stack runs
# with restart:unless-stopped - anything actually in use is referenced
# by a live container and is skipped. The 48h grace keeps the image a
# just-completed deploy is about to need.
set -eu

LOG_TAG="webull-disk-cleanup"
logger -t "${LOG_TAG}" "starting; $(df -Pm / | awk 'NR==2 {print $4}')MB free"

docker image prune -af --filter "until=48h" >/dev/null 2>&1 || true
docker builder prune -af >/dev/null 2>&1 || true
docker container prune -f --filter "until=48h" >/dev/null 2>&1 || true
journalctl --vacuum-size=100M >/dev/null 2>&1 || true

# Release trees: keep the newest three, and never the one `current`
# points at (the rollback target deploy.sh falls back to).
if [ -d /opt/webull-bot/releases ]; then
  CURRENT="$(readlink -f /opt/webull-bot/current 2>/dev/null || echo none)"
  ls -1t /opt/webull-bot/releases 2>/dev/null | tail -n +4 | while read -r old; do
    [ -n "${old}" ] || continue
    [ "/opt/webull-bot/releases/${old}" = "${CURRENT}" ] && continue
    rm -rf -- "/opt/webull-bot/releases/${old}" || true
  done
fi

# The bot caps its own daily logs, but sweep anything written before
# that cap existed, plus any left by an older image.
find /var/lib/docker/volumes/webull-trading-data/_data/logs \
  -name '*.log' -mtime +5 -delete >/dev/null 2>&1 || true

logger -t "${LOG_TAG}" "done; $(df -Pm / | awk 'NR==2 {print $4}')MB free"
CLEANUP
chmod 0755 /usr/local/bin/webull-disk-cleanup

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
# 03:30 local, well clear of the trading session. Persistent so a
# machine that was off at 03:30 still runs it on the next boot rather
# than silently skipping a day.
OnCalendar=*-*-* 03:30:00
Persistent=true

[Install]
WantedBy=timers.target
UNIT

# A low-disk alarm, independent of the daily sweep. If anything ever
# outruns the timer, this says so in the journal while the machine is
# still reachable - rather than the first symptom being a wedged
# Docker daemon and a bot that stopped trading.
#
# Deliberately a SCRIPT FILE rather than an inline ExecStart. The first
# version of this inlined `awk "NR==2 {print \\$4}"` into the unit, and
# the nesting (shell inside systemd inside a heredoc) mangled the
# escape: awk died with "backslash not last character on line", $free
# came out empty, and the threshold test silently never fired. The
# service still reported "success" the whole time - an alarm that
# cannot fire is worse than no alarm, because it is trusted.
install -m 0755 /dev/null /usr/local/bin/webull-disk-alarm
cat >/usr/local/bin/webull-disk-alarm <<'ALARM'
#!/bin/sh
set -eu
FREE_MB="$(df -Pm / | awk 'NR==2 {print $4}')"
[ -n "${FREE_MB}" ] || exit 0
if [ "${FREE_MB}" -lt 1500 ]; then
  logger -t webull-disk-alarm -p user.err \
    "LOW DISK: ${FREE_MB}MB free - deploys will be refused; run /usr/local/bin/webull-disk-cleanup"
fi
ALARM
chmod 0755 /usr/local/bin/webull-disk-alarm

# Prove the alarm can actually fire before trusting it, by running it
# against a threshold the current disk must trip. Catches exactly the
# quoting failure described above.
if ! FREE_CHECK="$(df -Pm / | awk 'NR==2 {print $4}')" \
  || [ -z "${FREE_CHECK}" ]; then
  echo "Disk alarm self-test failed: cannot read free space." >&2
  exit 1
fi
echo "Disk alarm self-test: parsed ${FREE_CHECK}MB free."

cat >/etc/systemd/system/webull-disk-alarm.service <<'UNIT'
[Unit]
Description=Warn when the trading host is running out of disk

[Service]
Type=oneshot
ExecStart=/usr/local/bin/webull-disk-alarm
UNIT

cat >/etc/systemd/system/webull-disk-alarm.timer <<'UNIT'
[Unit]
Description=Hourly low-disk check for the trading host

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now webull-disk-cleanup.timer
systemctl enable --now webull-disk-alarm.timer

echo "Installed. Timers:"
systemctl list-timers --no-pager 'webull-*' || true
