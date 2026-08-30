#!/bin/bash
# Runs the Step 6 strategy monitor (paper_trading/dashboard/app.py).
# `streamlit run` doesn't resolve package-relative imports from the
# invoking directory the way `python -m` does elsewhere in this repo —
# without PYTHONPATH set explicitly here, it fails with
# ModuleNotFoundError: No module named 'paper_trading'.
#
# --server.headless true is required under pm2 (or any non-interactive
# supervisor): without it, Streamlit's first-ever run on a machine blocks
# forever on an interactive "Welcome, enter your email" prompt that
# never gets answered, since nothing is attached to stdin.
set -e
cd "$(dirname "$0")/.."
export PYTHONPATH=.
exec .venv/bin/streamlit run paper_trading/dashboard/app.py --server.headless true
