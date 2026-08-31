"""Does a divergence trade make money, at the horizons it is actually held?

BACKGROUND
----------
forward_prediction.py found that divergence has no measurable power to
predict the FX return over the following 15 minutes -- pooled t of 0.41,
R2 of 0.0001, inconsistent signs across relationships. That is the
strategy's central premise, so the result matters. But it did not settle
the question, for two reasons:

  * 15 minutes is the wrong horizon. Positions are held up to 240.
  * A linear fit measures the average relationship. The strategy only
    trades the tail -- the few percent of divergences that clear a
    threshold -- and the tail can behave differently from the bulk.

This addresses both. Forward returns are computed from raw bar prices
rather than from stored 15-minute features, so any horizon can be tested,
and the tail is examined directly.

THE TEST THAT MATTERS
---------------------
For each candidate threshold, take only the observations the strategy would
actually trade, and compute

    sign(divergence) * forward_return

which is what the position earns before costs: long when divergence says the
currency should rise, short when it says the opposite. If the premise holds
this is positive on average. The spec's maximum expected round-trip cost of
4 basis points is shown alongside, because an edge smaller than its own
transaction cost is not an edge.

Overlapping windows are avoided by thinning to one observation per forward
window.

Usage:
    .venv/bin/python3 -m strategy.research.divergence_edge \\
        --database /tmp/replay.db --min-abs-beta 0.35
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from strategy.config.intraday.load_intraday_spec import (
    DEFAULT_SPEC_PATH,
    load_intraday_spec,
)


REPLAY_RUN_ID = "commodity_fx_intraday-threshold-replay"
HORIZONS = (15, 60, 240)


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def percentile(ordered: list[float], p: float) -> float:
    if not ordered:
        return float("nan")
    index = min(
        len(ordered) - 1,
        max(0, int(round(p / 100.0 * (len(ordered) - 1)))),
    )
    return ordered[index]


MINIMUM_SAMPLE = 30


def summarize(
    values: list[float],
) -> tuple[float | None, float, float, int]:
    """Mean in basis points, its t-statistic, hit rate, and count.

    Returns a mean of None below the minimum sample rather than 0.0. A
    printed zero reads as "measured, no edge", which is the opposite of
    "not enough observations to say" -- and this table is meant to inform
    whether to commit to an eight-week run.
    """
    n = len(values)
    if n < MINIMUM_SAMPLE:
        return None, 0.0, 0.0, n
    mean = statistics.fmean(values)
    sd = statistics.stdev(values)
    t_stat = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    hits = sum(1 for v in values if v > 0) / n * 100.0
    return mean * 10_000.0, t_stat, hits, n


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Edge of divergence trades by horizon and threshold."
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC_PATH)
    parser.add_argument("--run-id", default=REPLAY_RUN_ID)
    parser.add_argument("--min-abs-beta", type=float, default=None)
    args = parser.parse_args()

    spec = load_intraday_spec(args.spec)
    cost_bps = float(
        spec["execution"][
            "reject_entry_when_expected_round_trip_cost_bps_exceeds"
        ]
    )

    connection = sqlite3.connect(args.database)
    keep: set[str] | None = None
    if args.min_abs_beta is not None:
        keep = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT relationship_id FROM live_instrument_registry
                WHERE transmission_beta IS NOT NULL
                  AND ABS(transmission_beta) >= ?
                """,
                (args.min_abs_beta,),
            )
        }

    snapshots = connection.execute(
        """
        SELECT relationship_id, feature_timestamp_utc, divergence_score
        FROM feature_snapshots
        WHERE run_id = ?
          AND market_data_complete = 1
          AND divergence_score IS NOT NULL
        ORDER BY relationship_id, feature_timestamp_utc
        """,
        (args.run_id,),
    ).fetchall()

    symbols = {
        str(row[0]).split("__")[-1]
        for row in snapshots
        if keep is None or str(row[0]) in keep
    }
    prices: dict[str, dict[datetime, float]] = {}
    for symbol in symbols:
        prices[symbol] = {
            parse_iso(str(row[0])): float(row[1])
            for row in connection.execute(
                """
                SELECT bar_timestamp_utc, close_price
                FROM market_bars_1m
                WHERE symbol = ? AND is_complete = 1
                """,
                (symbol,),
            )
        }
    connection.close()

    print(
        f"\nDivergence edge, spec v"
        f"{spec['strategy']['specification_version']}"
    )
    print(
        f"sign(divergence) * forward return, in basis points. "
        f"Round-trip cost budget: {cost_bps:.1f} bps.\n"
    )

    for horizon in HORIZONS:
        # One observation per forward window, so the samples are independent.
        stride = max(1, horizon // 5)
        records: list[tuple[float, float]] = []
        for index, (relationship_id, stamp, divergence) in enumerate(
            snapshots
        ):
            relationship_id = str(relationship_id)
            if keep is not None and relationship_id not in keep:
                continue
            if index % stride:
                continue
            symbol = relationship_id.split("__")[-1]
            series = prices.get(symbol)
            if not series:
                continue
            start = parse_iso(str(stamp))
            end = start + timedelta(minutes=horizon)
            p0 = series.get(start)
            p1 = series.get(end)
            if not p0 or not p1 or p0 <= 0 or p1 <= 0:
                continue
            records.append(
                (float(divergence), math.log(p1 / p0))
            )

        if len(records) < 60:
            print(f"  {horizon:>3}m horizon: too few observations\n")
            continue

        magnitudes = sorted(abs(r[0]) for r in records)
        print(f"  {horizon:>3}-minute holding horizon   ({len(records)} obs)")
        print(
            f"    {'threshold':>22} {'trades':>7} {'mean bps':>9} "
            f"{'t':>7} {'hit%':>6}  net of cost"
        )
        for label, p in (
            ("all divergences", 0.0),
            ("top 50%", 50.0),
            ("top 10%", 90.0),
            ("top 5%", 95.0),
            ("top 1%", 99.0),
        ):
            cut = percentile(magnitudes, p) if p else 0.0
            selected = [
                math.copysign(1.0, d) * forward
                for d, forward in records
                if abs(d) >= cut
            ]
            mean_bps, t_stat, hits, n = summarize(selected)
            if mean_bps is None:
                print(
                    f"    {label:>22} {n:7} "
                    f"{'--':>9} {'--':>7} {'--':>6}  "
                    f"too few to say"
                )
                continue
            net = mean_bps - cost_bps
            verdict = "positive" if net > 0 else ""
            print(
                f"    {label:>22} {n:7} {mean_bps:9.2f} "
                f"{t_stat:7.2f} {hits:6.1f}  {net:+7.2f} {verdict}"
            )
        print()

    print(
        "A mean of zero within noise means divergence carries no edge at "
        "that horizon.\nAn edge smaller than the cost budget is not "
        "tradeable even if it is real."
    )


if __name__ == "__main__":
    main()
