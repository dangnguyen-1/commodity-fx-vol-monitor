"""Deletes one-minute bars older than the retention window.

Daily bars are the research record and are never pruned. One-minute bars
exist only to keep the dashboard's current price live between daily
rebuilds and to give the health watchdog a freshness signal, and both read
back days rather than months. Left unpruned they accumulate roughly 38
million rows a year.

Deletes in batches so the table is never locked for long, which matters
because tv-stream is writing to it continuously.

Usage:
    .venv/bin/python3 -m data_collector.database.prune_minute_bars
    .venv/bin/python3 -m data_collector.database.prune_minute_bars --dry-run
"""

from __future__ import annotations

import argparse
import os

import psycopg2
from dotenv import load_dotenv


# The dashboard reads back three days for its live price, so a week leaves
# generous headroom for a collector outage without keeping months.
DEFAULT_RETENTION_DAYS = 7
BATCH_ROWS = 100_000


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prune one-minute market data past the retention window."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=int(
            os.environ.get("MINUTE_BAR_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    connection = psycopg2.connect(os.environ["DATABASE_URL"])
    connection.autocommit = True

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) FROM market_data
                WHERE timeframe = '1'
                  AND datetime_utc < now() - (%s || ' days')::interval
                """,
                (str(args.days),),
            )
            stale = int(cursor.fetchone()[0])
            print(f"one-minute bars older than {args.days} days: {stale:,}")

            if args.dry_run or stale == 0:
                return

            deleted = 0
            while True:
                # ctid keeps the batch cheap: no ordering, no index needed
                # on a column the table is not indexed by.
                cursor.execute(
                    """
                    DELETE FROM market_data
                    WHERE ctid IN (
                        SELECT ctid FROM market_data
                        WHERE timeframe = '1'
                          AND datetime_utc < now() - (%s || ' days')::interval
                        LIMIT %s
                    )
                    """,
                    (str(args.days), BATCH_ROWS),
                )
                if not cursor.rowcount:
                    break
                deleted += cursor.rowcount
                print(f"  deleted {deleted:,} of {stale:,}")

            # Marks the freed space reusable by the continuing inserts.
            # Not VACUUM FULL, which would take an exclusive lock and block
            # tv-stream for the duration.
            cursor.execute("VACUUM (ANALYZE) market_data")
            print(f"done: {deleted:,} rows removed, table vacuumed")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
