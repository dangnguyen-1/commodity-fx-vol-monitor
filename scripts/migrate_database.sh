#!/bin/bash
# One-time migration off the previous owner's shared Postgres and onto one
# we control ourselves. Run this ONCE, then never again — after it, .env
# points at our own database and nothing in this repo has any knowledge of
# or dependency on the upstream one.
#
# The upstream URL is deliberately NOT read from .env and never written to
# disk. It's a credential belonging to someone else's account, and the whole
# point of this migration is that it stops mattering. Pass it as a shell
# variable for the duration of this one command:
#
#   SOURCE_DATABASE_URL='postgresql://...jacks-neon-host.../commodities' \
#   TARGET_DATABASE_URL='postgresql://...our-own-host.../commodities' \
#   ./scripts/migrate_database.sh
#
# Prefix the command with a space if your shell is zsh/bash with HISTCONTROL
# set to ignorespace, so the URL doesn't land in ~/.zsh_history either.
#
# TARGET can be anything we own — our own Neon project, Postgres on our own
# VM, or a fresh local database. The script doesn't care, as long as it's
# empty. It must be EMPTY: this restores a full dump (schema + data +
# indexes + sequences), which is the only way to guarantee the row-for-row
# fidelity we verify at the end. Restoring on top of the stale July snapshot
# in an existing database would collide with the unique indexes instead.
set -e
cd "$(dirname "$0")/.."

if [ -z "$SOURCE_DATABASE_URL" ] || [ -z "$TARGET_DATABASE_URL" ]; then
  echo "Both SOURCE_DATABASE_URL and TARGET_DATABASE_URL must be set."
  echo "See the comment at the top of this script."
  exit 1
fi

if [ "$SOURCE_DATABASE_URL" = "$TARGET_DATABASE_URL" ]; then
  echo "SOURCE and TARGET are the same database. That's not a migration."
  exit 1
fi

# Everything we print about a connection goes through this — a Postgres URL
# carries the role password inline, and these get pasted into chat logs and
# terminal scrollback.
mask() { echo "$1" | sed -E 's#://[^@/]*@#://***:***@#'; }

# The five tables data_collector/database/schema.sql defines. Used for both
# the pre-flight readout and the post-restore verification.
TABLES="market_data fundamental_trade_data news_articles news_sentiment news_sentiment_status"

counts() {
  # Row count per table for the given URL, as "table<tab>count" lines, so
  # source and target can be diffed directly.
  local url="$1"
  for t in $TABLES; do
    local n
    n=$(psql "$url" -tAc "SELECT count(*) FROM $t" 2>/dev/null || echo "MISSING")
    printf '%s\t%s\n' "$t" "$n"
  done
}

echo "=== [1/5] Pre-flight: what we're copying FROM ==="
echo "Source: $(mask "$SOURCE_DATABASE_URL")"
if ! psql "$SOURCE_DATABASE_URL" -tAc "SELECT 1" >/dev/null 2>&1; then
  echo "Can't connect to the source. If it's Neon, the URL needs ?sslmode=require."
  exit 1
fi
counts "$SOURCE_DATABASE_URL" | column -t
echo
echo "Newest market_data tick: $(psql "$SOURCE_DATABASE_URL" -tAc "SELECT to_char(to_timestamp(max(timestamp)) AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI') FROM market_data")"
echo "Newest news article:     $(psql "$SOURCE_DATABASE_URL" -tAc "SELECT to_char(max(published) AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI') FROM news_articles")"
echo "On-disk size:            $(psql "$SOURCE_DATABASE_URL" -tAc "SELECT pg_size_pretty(pg_database_size(current_database()))")"
echo
echo "Sanity check those dates. If they're still around 2026-07-20, this is a"
echo "stale snapshot rather than the live shared database, and copying it"
echo "gains us nothing — stop and sort that out first."
echo
read -p "Press Enter to continue, Ctrl-C to abort... " _

echo "=== [2/5] Pre-flight: confirming the target is empty ==="
echo "Target: $(mask "$TARGET_DATABASE_URL")"
if ! psql "$TARGET_DATABASE_URL" -tAc "SELECT 1" >/dev/null 2>&1; then
  echo "Can't connect to the target. Create the database first, then re-run."
  exit 1
fi
EXISTING=$(psql "$TARGET_DATABASE_URL" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
if [ "$EXISTING" -ne 0 ]; then
  echo
  echo "The target already has $EXISTING table(s) in the public schema."
  echo "This script only restores into an empty database — see the note at the"
  echo "top about why. Either point it at a genuinely fresh one, or drop and"
  echo "recreate this one yourself if it's a scratch database you don't need."
  exit 1
fi
echo "Empty. Good."

echo "=== [3/5] Dumping the source ==="
mkdir -p backups
DUMP="backups/upstream_seed_$(date -u +%Y%m%dT%H%M%SZ).dump"
# Custom format: compressed, and pg_restore can be pointed at it again later
# if this needs re-running. This is the ONLY artifact of the old database we
# keep, and it's gitignored — it's data, not a live credential.
pg_dump -Fc --no-owner --no-privileges -f "$DUMP" "$SOURCE_DATABASE_URL"
echo "Wrote $DUMP ($(du -h "$DUMP" | cut -f1))"

echo "=== [4/5] Restoring into our own database ==="
# --no-owner/--no-privileges: the dump's role names are the upstream account's
# and won't exist on our side. Everything lands owned by whoever we connect as.
pg_restore --no-owner --no-privileges -d "$TARGET_DATABASE_URL" "$DUMP"

echo "=== [5/5] Verifying row counts match ==="
if diff <(counts "$SOURCE_DATABASE_URL") <(counts "$TARGET_DATABASE_URL") > /tmp/migrate_diff.$$; then
  counts "$TARGET_DATABASE_URL" | column -t
  echo
  echo "Every table matches the source exactly."
  rm -f /tmp/migrate_diff.$$
else
  echo "MISMATCH between source and target (source < target >):"
  cat /tmp/migrate_diff.$$
  rm -f /tmp/migrate_diff.$$
  echo
  echo "The dump at $DUMP is intact — investigate before re-running."
  exit 1
fi

echo
echo "=================================================================="
echo "Migration complete. To actually become independent, finish these:"
echo
echo "  1. Set DATABASE_URL in .env to the TARGET url above."
echo "     Nothing in this repo should ever reference the source again."
echo "  2. Start our own collectors:  pm2 start ecosystem.config.js"
echo "     Confirm new rows are arriving (curl localhost:8000/health)."
echo "  3. Only once step 2 is confirmed writing, tell Jack he can stop"
echo "     his pm2 stack. Until then he's still the one feeding the data."
echo
echo "  4. Forget the source URL. It was never written to disk by this"
echo "     script; clear it from your shell with:  unset SOURCE_DATABASE_URL"
echo "=================================================================="
