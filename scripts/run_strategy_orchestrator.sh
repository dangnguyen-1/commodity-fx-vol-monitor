#!/bin/bash
# The strategy's own decision loop: feature snapshots and signal decisions
# every five minutes, paper execution every minute. Runs continuously.
#
# Everything else under pm2 collects data; this is the only process that
# actually evaluates the strategy, and the only one that closes positions —
# including forcing them flat at their session deadline. It runs in
# live_paper mode, which is still paper trading; the spec's
# `live_capital_approved: false` is what keeps it that way.
set -e
cd "$(dirname "$0")/.."
exec .venv/bin/python3 -m paper_trading.orchestrator.run_strategy_cycle \
  --run-mode live_paper
