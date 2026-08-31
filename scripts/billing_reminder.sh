#!/bin/bash
# Monthly nudge to check the Hetzner invoice.
#
# A failed card takes the whole system down: collectors, dashboard, the run
# and its data. Unlike every other failure mode here, the watchdog cannot see
# it coming, because a suspended account looks like an unreachable host and
# by then it is already too late.
#
# Fires on the 29th via pm2's cron_restart. It sends a reminder rather than
# checking a balance, because Hetzner exposes no billing API for this and a
# fake check would be worse than an honest one.
set -u
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
. ./.env
set +a

URL="${ALERT_WEBHOOK_URL:-}"
MESSAGE="*commodities pipeline*
[REMINDER] Hetzner billing check.

The VM carries the collectors, the dashboard and the live paper-trading run.
A lapsed payment stops all of it and ends the run's data with it.

  console.hetzner.com -> your project -> Billing

Also worth a glance while you are there: OpenAI credit, at roughly 500 news
classifications a day."

if [ -z "$URL" ]; then
  echo "ALERT_WEBHOOK_URL is not set; printing instead."
  echo "$MESSAGE"
  exit 0
fi

payload=$(python3 - "$MESSAGE" <<'PY'
import json, sys
message = sys.argv[1]
# Slack reads "text", Discord reads "content". Sending both means one
# setting works for either.
print(json.dumps({"text": message, "content": message}))
PY
)

# Discord sits behind Cloudflare, which rejects urllib/curl defaults with a
# 403 unless a real User-Agent is sent.
if curl -sS -X POST -H 'Content-Type: application/json' \
     -A 'commodities-billing-reminder/1.0' \
     -d "$payload" "$URL" >/dev/null; then
  echo "billing reminder sent"
else
  echo "billing reminder FAILED to send"
  exit 1
fi
