#!/bin/bash
# Checks that the pipeline is actually working and notifies when it is not.
#
# Exits non-zero while something is wrong, so pm2's log shows the failure
# rather than only the notification. Nothing restarts on that exit — this is
# a cron-style one-shot, and a failing check means the pipeline is broken,
# not the watchdog.
cd "$(dirname "$0")/.."
exec .venv/bin/python3 -m paper_trading.monitoring.health_watchdog \
  --quiet-when-healthy
