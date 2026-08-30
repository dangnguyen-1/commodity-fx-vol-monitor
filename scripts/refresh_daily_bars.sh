#!/bin/bash
# Refreshes the pre-aggregated 1D (daily close) bars in market_data that
# the dashboard's /market-data?timeframe=1D reads — a separate table/job
# from tv-stream's raw 1-minute ticks. Without this running, the "daily
# close" the dashboard shows freezes at whenever this last ran, even
# though the raw 1-minute feed keeps flowing. Idempotent (ON CONFLICT
# upsert), safe to re-run daily — see historical_daily_backfill.js.
set -e
cd "$(dirname "$0")/.."
exec node data_collector/market_data/collectors/historical_daily_backfill.js
