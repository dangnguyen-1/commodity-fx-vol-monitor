#!/bin/bash
# Recurring nudges for the two costs that can stop this system.
#
#   ./scripts/billing_reminder.sh hetzner   monthly, the 29th
#   ./scripts/billing_reminder.sh openai    weekly, Sunday
#
# Both send a reminder rather than reading a balance. Hetzner exposes no
# billing API, and OpenAI's balance endpoint is not available on current
# accounts, so a "check" would be a fabrication. The watchdog does track
# OpenAI spend from logged token usage; this is the human-facing backstop.
#
# Scheduled from the user crontab rather than pm2, because cron honours
# CRON_TZ and pm2 does not. These fire at 21:00 America/New_York, which is a
# different UTC hour in summer and winter; anchoring to the local time is the
# point.
set -u
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
. ./.env
set +a

KIND="${1:-hetzner}"

case "$KIND" in
  hetzner)
    MESSAGE="*commodities pipeline*
[REMINDER] Hetzner billing check.

The VM carries the collectors, the dashboard and the live paper-trading run.
A lapsed payment stops all of it and ends the run's data with it.

  console.hetzner.com -> your project -> Billing"
    ;;
  openai)
    # Reports measured usage rather than asking someone to go and look. The
    # previous version only linked to the billing page, which is how 1,023
    # calls came to be logged at $0.00 without anyone noticing that the
    # per-million prices had never been set.
    USAGE=$(.venv/bin/python3 -m monitoring.openai_usage_report --days 7 2>&1)
    MESSAGE="*commodities pipeline*
[WEEKLY] OpenAI usage.

$USAGE

News classification is the only variable cost here. If credit runs out it
stops silently and the Risk & News tab goes stale.

  platform.openai.com -> Settings -> Billing"
    ;;
  *)
    echo "Unknown reminder: $KIND (expected 'hetzner' or 'openai')"
    exit 2
    ;;
esac

URL="${ALERT_WEBHOOK_URL:-}"
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

# Discord sits behind Cloudflare, which rejects default agents with a 403.
if curl -sS -X POST -H 'Content-Type: application/json' \
     -A 'commodities-reminder/1.0' \
     -d "$payload" "$URL" >/dev/null; then
  echo "$KIND reminder sent"
else
  echo "$KIND reminder FAILED to send"
  exit 1
fi
