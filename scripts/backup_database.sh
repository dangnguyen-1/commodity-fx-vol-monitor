#!/bin/bash
# Nightly pg_dump of the collectors' Postgres, with rotation.
#
# Why this exists at all: the market data is not reproducible. TradingView's
# live feed only retains a week or two of 1-minute history, so anything older
# than that exists solely in this database, re-running the collectors would
# not bring it back. Losing it also resets the Step 9 clock (8 calendar weeks
# and 200 closed trades, no rule changes mid-period), which is wall-clock
# time that cannot be recovered by any amount of compute.
#
# What this protects against, honestly: accidental drops, a bad migration, a
# corrupted table, anything logical. It does NOT protect against losing the
# disk, because the dumps live on that same disk. For that, the dumps have to
# leave the box: set BACKUP_REMOTE to an scp-style destination
# (user@host:/path) and they'll be copied there after each successful dump,
# or pull them down periodically from your own machine with
#   rsync -av commodities@<vm-ip>:~/commodities/backups/ ./backups/
set -e
cd "$(dirname "$0")/.."

set -a
. ./.env
set +a

if [ -z "$DATABASE_URL" ]; then
  echo "DATABASE_URL is not set in .env, nothing to back up."
  exit 1
fi

KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
mkdir -p backups
DUMP="backups/commodities_$(date -u +%Y%m%dT%H%M%SZ).dump"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] dumping to $DUMP"
pg_dump -Fc --no-owner --no-privileges -f "$DUMP" "$DATABASE_URL"

# Verify before rotating. A dump that pg_restore can't read its own table of
# contents from is not a backup, and deleting good older copies on the
# strength of a broken new one is how backup schemes quietly fail.
if ! pg_restore -l "$DUMP" > /dev/null 2>&1; then
  echo "ERROR: $DUMP is not a readable archive. Keeping all older backups."
  exit 1
fi

TABLES=$(pg_restore -l "$DUMP" | grep -c "TABLE DATA" || true)
echo "verified: $TABLES table(s) of data, $(du -h "$DUMP" | cut -f1)"

if [ -n "$BACKUP_REMOTE" ]; then
  # Best-effort: a failure to reach the remote must not fail the whole run,
  # since the local dump is already safely written at this point.
  echo "copying to $BACKUP_REMOTE"
  scp -q "$DUMP" "$BACKUP_REMOTE" || echo "WARNING: off-box copy failed; local dump is intact."
fi

# Rotate only after a verified dump exists, so a run of failures can never
# leave us with nothing.
DELETED=$(find backups -name 'commodities_*.dump' -type f -mtime +"$KEEP_DAYS" -print -delete | wc -l)
echo "rotated out $DELETED backup(s) older than $KEEP_DAYS days"
# Size only our own dumps, not everything in backups/, the one-off
# migration seed lives here too, and counting it made a single 20M backup
# report as "1 backup(s), 40M total".
KEPT=$(find backups -name 'commodities_*.dump' -type f | wc -l)
KEPT_SIZE=$(find backups -name 'commodities_*.dump' -type f -exec du -ch {} + 2>/dev/null | tail -1 | cut -f1)
echo "retained: $KEPT backup(s), ${KEPT_SIZE:-0} total"
