#!/bin/bash
# Wrapper so pm2 can run this as a plain script, same reasoning as
# run_news_stream.sh.
set -e
cd "$(dirname "$0")/.."
exec .venv/bin/python3 -m uvicorn api.app:app --host 127.0.0.1 --port 8000
