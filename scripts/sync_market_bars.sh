#!/bin/bash
# Normalises completed 1-minute bars out of the collector's Postgres and into
# the paper-trading SQLite database, which is what the strategy engine
# actually reads. Incremental via market_ingestion_state, so a short cadence
# is cheap.
#
# This script already existed; nothing ever ran it on a schedule. That is why
# the API's /health showed market_data_adapter frozen — it had only ever been
# invoked by hand, exactly the way news-sync and the daily bars went stale
# before they were put on real schedules. The strategy orchestrator cannot
# produce a feature snapshot without these rows, so this has to run for the
# rest of the cycle to mean anything.
set -e
cd "$(dirname "$0")/.."
exec .venv/bin/python3 -m paper_trading.market_data.sync_market_bars
