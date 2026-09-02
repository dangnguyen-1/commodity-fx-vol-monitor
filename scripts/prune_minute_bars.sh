#!/bin/bash
# Drops one-minute bars past the retention window (default 7 days).
#
# Daily bars are the research record and are never touched. One-minute bars
# only feed the dashboard's live price and the watchdog's freshness check,
# both of which read back days, so keeping them indefinitely would add
# roughly 38 million rows a year for nothing.
set -e
cd "$(dirname "$0")/.."
exec .venv/bin/python3 -m data_collector.database.prune_minute_bars
