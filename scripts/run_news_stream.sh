#!/bin/bash
# Wrapper so pm2 can run this as a plain script, `python3 -m package.module`
# needs the cwd on the path and the -m flag, which pm2's `script` field
# can't express directly.
set -e
cd "$(dirname "$0")/.."
exec .venv/bin/python3 -m data_collector.news_data.collectors.news_stream
