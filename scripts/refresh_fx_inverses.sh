#!/bin/bash
# Regenerates the DERIVED:xxxUSD inverse FX rows (CADUSD, MXNUSD, etc.)
# from the raw USDxxx quotes tv-stream collects, needed because
# TradingView (like most FX data) quotes these as USD-per-foreign-unit,
# but the dashboard's whole convention is foreign-unit-per-USD. This was
# previously only ever run once by hand and never scheduled, so it would
# have silently gone stale the same way the daily bars and news sync did.
# Cheap (a few INSERT..SELECT statements), so a tight cadence is fine.
set -e
cd "$(dirname "$0")/.."
exec .venv/bin/python3 -m data_collector.market_data.collectors.generate_fx_inverses
