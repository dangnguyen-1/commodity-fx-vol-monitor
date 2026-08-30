#!/bin/bash
# LLM classification of whatever news_stream.py has collected — a
# separate persistent loop from news_stream.py itself (that one only
# collects raw articles; this is the one that actually produces the
# classified rows the dashboard's /news/latest reads).
set -e
cd "$(dirname "$0")/.."
exec .venv/bin/python3 -m data_collector.news_data.collectors.news_sentiment_stream
