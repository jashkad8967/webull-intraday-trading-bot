#!/bin/sh
# One container, two processes: the trading bot and the dashboard that
# drives its manual buy/sell/cancel buttons.
#
# They used to be two containers, which on a 2-core/1GB/8.7G host meant
# two base images and two dependency trees on a disk that filled to 99%
# on 2026-09-23 and wedged Docker with the bot DOWN mid-session. They
# only ever talked through files (/var/data/status.json, the log
# directory, /var/commands/commands.json), so collapsing them changes
# no protocol - both now read and write the same paths on the same
# local filesystem.
#
# Deliberately two PROCESSES rather than a thread inside the bot. The
# dashboard's /api/logs endpoint reads a whole daily log file, and in
# one interpreter that read would hold the GIL against a trading loop
# with hard sub-second exit requirements. Separate processes keep the
# web server entirely off the bot's critical path.
set -eu

# The bot is the reason this container exists, so it runs as PID 1:
# docker stop's SIGTERM reaches it directly and compose's
# stop_grace_period applies to the process holding live positions.
# The dashboard is a child - if the bot exits, the container goes
# down with it, which is correct.
(
  while true; do
    # Never allowed to take the bot with it: a crash here is retried,
    # and a permanent failure leaves the bot trading without a UI
    # rather than killing the container.
    uvicorn server:app \
      --app-dir /app/ui \
      --host 0.0.0.0 \
      --port "${DASHBOARD_PORT:-8080}" \
      --log-level warning || true
    echo "dashboard exited; restarting in 5s" >&2
    sleep 5
  done
) &

exec python -m webull_bot
