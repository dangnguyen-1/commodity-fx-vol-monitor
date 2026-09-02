#!/bin/bash
# Checks that the pipeline is actually working and notifies when it is not.
#
# Exits non-zero while something is wrong, so the log shows the failure
# rather than only the notification. Nothing restarts on that exit: this is
# a one-shot, and a failing check means the pipeline is broken, not the
# watchdog.
#
# Scheduled from the user crontab rather than pm2. pm2's cron_restart
# silently stopped firing for the market-bar sync after a day of working,
# and a watchdog that depends on the scheduler which just failed cannot
# report that it has stopped. Its own absence would be invisible.
cd "$(dirname "$0")/.."

# Loaded here rather than inherited, so the script behaves the same under
# cron, which starts with almost no environment.
set -a
# shellcheck disable=SC1091
. ./.env
set +a

exec .venv/bin/python3 -m monitoring.health_watchdog \
  --quiet-when-healthy
