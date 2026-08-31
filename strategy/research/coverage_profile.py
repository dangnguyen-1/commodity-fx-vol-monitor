"""Measures how often each relationship can actually produce a signal.

THE POINT
---------
A relationship needs two things to be tradeable: a statistical edge, and
enough 1-minute bars to build a feature at all. The daily screen covers the
first. This covers the second, which turns out to bind harder than expected.

The spec requires `data.market.minimum_window_coverage_pct` (95%) of bars
across the feature window before a snapshot counts as complete. A commodity
too illiquid to print a bar most minutes never clears that, no matter how
good its relationship looks -- and neither does one whose FX leg is thin,
which is what rules out the Brazilian Real pairs.

Coverage also varies enormously by hour: a snapshot taken at 03:00 UTC, when
Asia is quiet and most Western venues are shut, makes almost everything look
untradeable. So this reports coverage per relationship *per hour*, and the
share of hours in which both legs clear the bar. A single reading is
misleading; the daily profile is the thing to decide on.

Run it over at least a full 24 hours of collection.

Usage:
    .venv/bin/python3 -m strategy.research.coverage_profile --hours 24
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from collections import defaultdict
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

from strategy.config.intraday.load_intraday_spec import (
    DEFAULT_SPEC_PATH,
    load_intraday_spec,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = (
    PROJECT_ROOT / "paper_trading" / "data" / "paper_trading.db"
)


def tradeable_hours(
    *,
    registry,
    counts,
    ordered_hours,
    days: int,
    required: float,
) -> dict[str, int]:
    """Hours in which both legs clear `required` percent coverage."""
    target = required / 100.0 * 60.0 * days
    out: dict[str, int] = {}
    for relationship_id, commodity_symbol, _venue in registry:
        fx_symbol = str(relationship_id).split("__")[-1]
        hours = 0
        for hour in ordered_hours:
            c = counts.get((str(commodity_symbol), hour), 0)
            f = counts.get((fx_symbol, hour), 0)
            if c >= target and f >= target:
                hours += 1
        out[str(relationship_id)] = hours
    return out


def compare(
    thresholds: list[float],
    *,
    registry,
    counts,
    ordered_hours,
    days: int,
    start: str | None,
    end: str | None,
    spec_required: float,
) -> None:
    """How the tradeable universe changes as the requirement is relaxed.

    Relaxing coverage buys trading opportunity and pays for it in data
    quality: a bar-sparse window means the returns feeding the impulse are
    measured across gaps. This shows what each step actually buys, so the
    threshold is chosen against a number rather than a feeling.
    """
    results = {
        t: tradeable_hours(
            registry=registry,
            counts=counts,
            ordered_hours=ordered_hours,
            days=days,
            required=t,
        )
        for t in thresholds
    }
    total_hours = len(ordered_hours)
    window = f"{start}..{end} ({days} days)" if start and end else "recent"

    print(f"\nCoverage sensitivity over {window}")
    print(f"Tradeable hours out of {total_hours}, by threshold. "
          f"Spec is currently {spec_required:.0f}%.\n")

    header = f"{'relationship':34}" + "".join(
        f"{t:>8.0f}%" for t in thresholds
    )
    print(header)
    print("-" * len(header))

    names = sorted(
        results[thresholds[0]],
        key=lambda n: -max(results[t][n] for t in thresholds),
    )
    for name in names:
        row = f"{name[:34]:34}"
        for t in thresholds:
            hours = results[t][name]
            row += f"{hours:>7}h "
        print(row)

    print()
    for t in thresholds:
        live = [n for n, h in results[t].items() if h > 0]
        usable = [n for n, h in results[t].items() if h >= total_hours * 0.5]
        print(
            f"  {t:.0f}%: {len(live):2} relationships tradeable at all, "
            f"{len(usable):2} tradeable at least half the day"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Per-relationship bar coverage by hour."
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC_PATH)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument(
        "--start",
        default=None,
        help="YYYY-MM-DD (UTC). With --end, reads a fixed window instead "
             "of the last N hours. Five stored July days cover every "
             "venue's session, which a recent-hours window may not.",
    )
    parser.add_argument("--end", default=None, help="YYYY-MM-DD (UTC)")
    parser.add_argument(
        "--thresholds",
        default=None,
        help="Comma-separated coverage percentages to compare, e.g. "
             "'95,90,85'. Prints a comparison table instead of the "
             "per-hour grid, so the cost of relaxing the requirement is "
             "visible rather than argued about.",
    )
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    spec = load_intraday_spec(args.spec)
    required = float(
        spec["data"]["market"]["minimum_window_coverage_pct"]
    )

    sqlite_connection = sqlite3.connect(args.database)
    registry = sqlite_connection.execute(
        """
        SELECT relationship_id, live_commodity_symbol, commodity_venue
        FROM live_instrument_registry
        WHERE active = 1
        ORDER BY relationship_id
        """
    ).fetchall()
    sqlite_connection.close()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is not set.")

    # Bars per symbol per UTC hour, straight from the collector. Reading the
    # source rather than the engine's copy keeps this independent of whether
    # the sync happens to be up to date.
    counts: dict[tuple[str, int], int] = defaultdict(int)
    hours_seen: set[int] = set()
    with psycopg2.connect(database_url) as connection:
        with connection.cursor() as cursor:
            if args.start and args.end:
                cursor.execute(
                    """
                    SELECT symbol,
                           extract(hour from to_timestamp(timestamp)
                                   at time zone 'UTC')::int,
                           count(*)
                    FROM market_data
                    WHERE timeframe = '1'
                      AND to_timestamp(timestamp) >= %s::date
                      AND to_timestamp(timestamp) < (%s::date + 1)
                    GROUP BY 1, 2
                    """,
                    (args.start, args.end),
                )
                days = 1 + (
                    __import__("datetime").date.fromisoformat(args.end)
                    - __import__("datetime").date.fromisoformat(args.start)
                ).days
            else:
                cursor.execute(
                    """
                    SELECT symbol,
                           extract(hour from to_timestamp(timestamp)
                                   at time zone 'UTC')::int,
                           count(*)
                    FROM market_data
                    WHERE timeframe = '1'
                      AND to_timestamp(timestamp) > now()
                          - (%s || ' hours')::interval
                    GROUP BY 1, 2
                    """,
                    (args.hours,),
                )
                days = max(1, args.hours // 24)
            for symbol, hour, count in cursor.fetchall():
                counts[(str(symbol), int(hour))] += int(count)
                hours_seen.add(int(hour))

    if not hours_seen:
        raise SystemExit("No 1-minute bars in that window.")

    if args.thresholds:
        compare(
            [float(v) for v in args.thresholds.split(",")],
            registry=registry,
            counts=counts,
            ordered_hours=sorted(hours_seen),
            days=days,
            start=args.start,
            end=args.end,
            spec_required=required,
        )
        return

    window = (
        f"{args.start}..{args.end} ({days} days)"
        if args.start and args.end
        else f"the last {args.hours}h"
    )
    print(
        f"\nCoverage over {window} "
        f"- {len(hours_seen)} UTC hours observed. "
        f"Spec requires {required:.0f}% of the feature window."
    )
    print(
        "Per hour: '#' both legs >= threshold, '+' one leg, "
        "'.' neither, ' ' no data\n"
    )

    ordered_hours = sorted(hours_seen)
    # Counts are pooled across every day in the window, so the bar target
    # scales with the number of days. Without this a five-day window looks
    # like five times the coverage it actually has.
    threshold_bars = required / 100.0 * 60.0 * days

    header = f"{'relationship':34} " + "".join(
        str(h % 10) for h in ordered_hours
    ) + "  tradeable_hours"
    print(header)
    print("-" * len(header))

    summary = []
    for relationship_id, commodity_symbol, _venue in registry:
        fx_symbol = str(relationship_id).split("__")[-1]
        row = ""
        tradeable = 0
        for hour in ordered_hours:
            c = counts.get((str(commodity_symbol), hour), 0)
            # The FX target may be a DERIVED series the collector writes under
            # its own name; fall back to counting the source when absent.
            f = counts.get((fx_symbol, hour), 0)
            c_ok = c >= threshold_bars
            f_ok = f >= threshold_bars
            if c == 0 and f == 0:
                row += " "
            elif c_ok and f_ok:
                row += "#"
                tradeable += 1
            elif c_ok or f_ok:
                row += "+"
            else:
                row += "."
        pct = 100.0 * tradeable / len(ordered_hours)
        summary.append((relationship_id, tradeable, pct))
        print(f"{str(relationship_id)[:34]:34} {row}  {tradeable:2}/{len(ordered_hours)} ({pct:.0f}%)")

    print("\n" + "=" * 60)
    print("Relationships by tradeable hours:")
    for relationship_id, tradeable, pct in sorted(
        summary, key=lambda s: -s[1]
    ):
        bar = "#" * int(pct / 5)
        print(f"  {str(relationship_id)[:38]:38} {pct:5.0f}%  {bar}")

    dead = [s for s in summary if s[1] == 0]
    if dead:
        print(
            f"\n{len(dead)} relationship(s) had no tradeable hour at all in "
            "this window. Before reading anything into that, check the window "
            "actually spans their venue's session -- a short sample taken "
            "overnight will condemn instruments that trade perfectly well "
            "during their own hours."
        )


if __name__ == "__main__":
    main()
