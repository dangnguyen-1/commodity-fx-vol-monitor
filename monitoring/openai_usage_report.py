"""Summarises OpenAI usage over a window, for the weekly billing reminder.

Its own module rather than inlined into billing_reminder.sh, because a
Python heredoc nested inside a shell heredoc breaks the moment either side
is edited.

Usage:
    .venv/bin/python3 -m monitoring.openai_usage_report --days 7
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import psycopg2
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")

    try:
        connection = psycopg2.connect(
            os.environ.get("DATABASE_URL", ""), connect_timeout=15
        )
    except Exception as error:  # noqa: BLE001
        print(f"  (could not read usage: {error})")
        return

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(input_tokens), 0),
                       COALESCE(SUM(output_tokens), 0),
                       COALESCE(SUM(estimated_cost_usd), 0)
                FROM openai_usage
                WHERE created_at_utc > now() - (%s || ' days')::interval
                """,
                (str(args.days),),
            )
            calls, tokens_in, tokens_out, cost = cursor.fetchone()

            cursor.execute(
                """
                SELECT DISTINCT model FROM openai_usage
                WHERE created_at_utc > now() - (%s || ' days')::interval
                """,
                (str(args.days),),
            )
            models = ", ".join(r[0] for r in cursor.fetchall()) or "none"

            cursor.execute(
                """
                SELECT COUNT(*) FROM openai_usage
                WHERE created_at_utc
                      >= date_trunc('day', now() AT TIME ZONE 'UTC')
                """
            )
            today = int(cursor.fetchone()[0])
    finally:
        connection.close()

    cost = float(cost or 0)
    print(f"  last {args.days} days:   {int(calls):,} calls on {models}")
    print(f"  tokens:        {int(tokens_in):,} in / {int(tokens_out):,} out")

    # Cost is only printed when prices are configured. Otherwise the volume
    # figures above are the useful part and a "$0.00" line would read as
    # free rather than as unmeasured.
    if cost > 0:
        per_30 = cost / max(args.days, 1) * 30
        print(f"  cost:          ${cost:.2f}  ->  ${per_30:.2f} per 30 days")

    cap = int(os.environ.get("OPENAI_MAX_CALLS_PER_DAY", "1000") or 0)
    if cap:
        print(f"  budget today:  {today:,} of {cap:,} calls")
        print(f"  hard ceiling:  classification stops at {cap:,} calls per day")


if __name__ == "__main__":
    main()
