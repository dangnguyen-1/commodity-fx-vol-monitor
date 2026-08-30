#!/bin/bash
# Periodic Comtrade refresh: fetch whatever's new since the last run (the
# backfill script checkpoints completed periods, so this is cheap after
# the first run — not a full 2010-present refetch each time), then load
# into fundamental_trade_data. Comtrade data is monthly, not real-time,
# so this only needs to run periodically (see ecosystem.config.js's
# cron_restart), not continuously like the market/news streams.
set -e
cd "$(dirname "$0")/.."
.venv/bin/python3 -m data_collector.fundamental_data.collectors.historical_backfill
.venv/bin/python3 -m data_collector.fundamental_data.collectors.load_trade_data_to_db
