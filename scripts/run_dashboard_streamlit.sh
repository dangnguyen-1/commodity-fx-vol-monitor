#!/bin/bash
# Runs the Step 6 strategy monitor (paper_trading/dashboard/app.py).
# `streamlit run` doesn't resolve package-relative imports from the
# invoking directory the way `python -m` does elsewhere in this repo —
# without PYTHONPATH set explicitly here, it fails with
# ModuleNotFoundError: No module named 'paper_trading'.
set -e
cd "$(dirname "$0")/.."
export PYTHONPATH=.
exec .venv/bin/streamlit run paper_trading/dashboard/app.py
