#!/bin/bash
# Refreshes the dashboard's on-disk caches so no visitor pays for a cold
# fetch. World Bank expires every 24 hours, which is what makes this a daily
# job; the Comtrade caches hold for 7 days and are a no-op on most runs.
#
# Deliberately run from the project root, not from dashboard/ as
# warm_cache.py's own usage line suggests. data/worldbank.py resolves
# "cache/worldbank_indicators.json" against the working directory, and the
# dashboard runs under pm2 with cwd set to the project root. Started from
# dashboard/ this would faithfully warm dashboard/cache/, which nothing
# reads, and the tab would still fetch on first load.
#
# Scheduled from the user crontab rather than pm2 cron_restart, for the
# reason recorded in ecosystem.config.js.
set -e
cd "$(dirname "$0")/.."
exec .venv/bin/python3 dashboard/scripts/warm_cache.py
