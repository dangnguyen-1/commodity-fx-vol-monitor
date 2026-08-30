#!/bin/bash
# Downloads free historical 1-minute (m1) bars from Dukascopy for the
# subset of the intraday strategy's 30 relationships that Dukascopy
# actually covers — 11 relationships across 14 unique instruments (10
# commodities + 4 currencies). Dukascopy has no Brazilian Real pair, so
# every BRL-linked relationship (Coffee, Soybeans, Sugar) is excluded
# even though those commodities themselves are available.
#
# This exists purely to backtest the intraday candidate's own formulas
# against real multi-year minute data, since the live TradingView feed
# only retains ~1-2 weeks of 1-minute history — nowhere near enough for
# a real backtest. Output is gitignored; regenerate by rerunning this.
#
# Chunked by year with pauses between requests: a single 2018-present
# request in one shot triggers a 429 (rate limit) from Dukascopy almost
# immediately, and the CLI has no built-in cross-request throttling —
# only per-instrument retries. Continues past a failed year/instrument
# instead of aborting the whole run (no `set -e`), since one bad chunk
# shouldn't cost the rest of the download.
cd "$(dirname "$0")/.."

FIRST_YEAR=2018
LAST_YEAR=2026
OUT_DIR="intraday_backtest/data"
mkdir -p "$OUT_DIR"

INSTRUMENTS=(
  brentcmdusd   # Brent Oil
  lightcmdusd   # WTI / Crude Oil
  gascmdusd     # Natural Gas
  xauusd        # Gold
  xagusd        # Silver
  coppercmdusd  # Copper
  xptcmdusd     # Platinum
  xpdcmdusd     # Palladium
  cocoacmdusd   # Cocoa
  cottoncmdusx  # Cotton
  usdcad        # CAD leg (Brent/WTI/NatGas relationships)
  audusd        # AUD leg (Gold/Copper relationships)
  gbpusd        # GBP leg (Cocoa relationship)
  eurusd        # USD-proxy leg (Gold/Silver/Platinum/Palladium/Cotton "USD" relationships)
)

for instrument in "${INSTRUMENTS[@]}"; do
  for year in $(seq "$FIRST_YEAR" "$LAST_YEAR"); do
    to_date="$year-12-31"
    if [ "$year" = "$LAST_YEAR" ]; then
      to_date="now"
    fi

    out_file="$OUT_DIR/${instrument}-m1-bid-${year}.csv"
    if [ -s "$out_file" ]; then
      echo "=== $instrument $year: already have it, skipping ==="
      continue
    fi

    echo "=== $instrument $year ==="
    npx dukascopy-node \
      -i "$instrument" \
      -from "$year-01-01" \
      -to "$to_date" \
      -t m1 \
      -f csv \
      -v \
      -dir "$OUT_DIR" \
      -fn "${instrument}-m1-bid-${year}" \
      -r 5 \
      -rp 8000 \
      -bs 3 \
      -bp 4000 \
      || echo "!!! $instrument $year FAILED, continuing !!!"

    sleep 6
  done
done

echo "ALL_DONE"
