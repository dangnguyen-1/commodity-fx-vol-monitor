#!/bin/bash
# Wrapper so pm2 can run this as a plain script — same reasoning as
# run_news_stream.sh.
set -e
cd "$(dirname "$0")/.."
exec .venv/bin/python3 -m uvicorn paper_trading.api.app:app --host 0.0.0.0 --port 8000
