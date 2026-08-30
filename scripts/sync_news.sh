#!/bin/bash
# Syncs newly-collected/classified news from the Postgres pipeline
# (news_articles/news_sentiment, written by news-stream + news-sentiment-
# stream) into the paper-trading SQLite database, which is what the API's
# /news/latest endpoint actually reads. Incremental via news_ingestion_state
# checkpointing, so a short cron cadence is cheap — news is time-sensitive,
# unlike Comtrade's monthly cadence, so this runs every few minutes rather
# than daily (see ecosystem.config.js's cron_restart).
set -e
cd "$(dirname "$0")/.."
exec .venv/bin/python3 -m paper_trading.news_data.sync_news --lookback-hours 168
