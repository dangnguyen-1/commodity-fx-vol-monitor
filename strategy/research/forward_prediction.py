"""Tests whether the signal predicts the FX move it is betting on.

TWO QUESTIONS
-------------
1. Which commodity return horizon predicts the *next* FX move? The
   contemporaneous test (horizon_contribution.py) showed the 15-minute term
   carries nearly all the explanatory power, but that measures how much of
   the *current* FX move the commodity explains. The strategy's logic is
   different: it looks for a commodity move the FX leg has not responded to
   yet and bets on catch-up. Under that reading, a long-horizon term having
   low contemporaneous correlation could be the point rather than a defect.
   Only a forward test separates the two.

2. Does divergence predict forward FX returns at all? This is the strategy's
   central premise, and as far as this repository shows it has never been
   tested. `divergence = expected - observed`, so a positive divergence says
   the currency has moved less than the commodity implies, and the trade
   bets it catches up. If that is real, divergence at time t should predict
   the FX return from t to t+15 with a positive coefficient. If the
   coefficient is zero, the strategy is trading noise; if it is negative,
   it is trading the wrong way round.

OVERLAP
-------
Snapshots are five minutes apart but the FX return spans fifteen, so
consecutive observations share two thirds of their window. That
autocorrelation inflates apparent significance without adding information,
so every statistic is reported twice: over all observations, and over a
non-overlapping subsample taking every third snapshot. Trust the second.

Usage:
    .venv/bin/python3 -m strategy.research.forward_prediction \\
        --database /tmp/replay.db --min-abs-beta 0.35
"""

from __future__ import annotations

import argparse
import math
import sqlite3
from datetime import timedelta
from pathlib import Path


REPLAY_RUN_ID = "commodity_fx_intraday-threshold-replay"
FORWARD_MINUTES = 15


def parse_iso(value: str):
    from datetime import datetime, timezone

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fit(xs: list[float], ys: list[float]) -> tuple[float, float, float, int]:
    """Slope, R^2, and a t-statistic for the slope."""
    n = len(xs)
    if n < 30:
        return 0.0, 0.0, 0.0, n
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx <= 0 or syy <= 0:
        return 0.0, 0.0, 0.0, n
    beta = sxy / sxx
    r2 = (sxy * sxy) / (sxx * syy)
    residual_var = (syy - beta * sxy) / (n - 2)
    standard_error = math.sqrt(residual_var / sxx) if residual_var > 0 else 0.0
    t_stat = beta / standard_error if standard_error > 0 else 0.0
    return beta, r2, t_stat, n


def report(
    title: str,
    pairs: list[tuple[float, float]],
    stride: int,
) -> None:
    everything = [(x, y) for x, y in pairs]
    thinned = everything[::stride]

    for label, sample in (
        ("all (overlapping)", everything),
        (f"every {stride}rd (independent)", thinned),
    ):
        xs = [p[0] for p in sample]
        ys = [p[1] for p in sample]
        beta, r2, t_stat, n = fit(xs, ys)
        print(
            f"    {label:28} n={n:5}  beta={beta:+7.4f}  "
            f"R2={r2:6.4f}  t={t_stat:+6.2f}"
        )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Forward-predictive power of the signal components."
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--run-id", default=REPLAY_RUN_ID)
    parser.add_argument("--min-abs-beta", type=float, default=None)
    args = parser.parse_args()

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

    rows = connection.execute(
        """
        SELECT relationship_id, feature_timestamp_utc,
               normalized_commodity_return_15m,
               normalized_commodity_return_60m,
               normalized_commodity_return_240m,
               commodity_impulse,
               divergence_score,
               normalized_fx_return_15m
        FROM feature_snapshots
        WHERE run_id = ?
          AND market_data_complete = 1
        ORDER BY relationship_id, feature_timestamp_utc
        """,
        (args.run_id,),
    ).fetchall()
    connection.close()

    # Index the forward FX return: the 15-minute return recorded at t+15
    # covers exactly t..t+15, which is the move a trade entered at t rides.
    forward: dict[tuple[str, str], float] = {}
    for row in rows:
        if row[7] is not None:
            forward[(str(row[0]), str(row[1]))] = float(row[7])

    series: dict[str, list[tuple[float, ...]]] = {}
    for row in rows:
        relationship_id = str(row[0])
        if keep is not None and relationship_id not in keep:
            continue
        if any(row[i] is None for i in (2, 3, 4, 5, 7)):
            continue
        future_key = (
            relationship_id,
            (
                parse_iso(row[1]) + timedelta(minutes=FORWARD_MINUTES)
            ).isoformat(),
        )
        if future_key not in forward:
            continue
        series.setdefault(relationship_id, []).append(
            (
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5]),
                float(row[6]) if row[6] is not None else float("nan"),
                forward[future_key],
            )
        )

    if not series:
        raise SystemExit("No snapshots with a matching forward return.")

    stride = FORWARD_MINUTES // 5  # snapshots per forward window

    pooled: dict[str, list[tuple[float, float]]] = {
        "return_15m": [],
        "return_60m": [],
        "return_240m": [],
        "commodity_impulse": [],
        "divergence_score": [],
    }
    for records in series.values():
        for record in records:
            pooled["return_15m"].append((record[0], record[5]))
            pooled["return_60m"].append((record[1], record[5]))
            pooled["return_240m"].append((record[2], record[5]))
            pooled["commodity_impulse"].append((record[3], record[5]))
            if not math.isnan(record[4]):
                pooled["divergence_score"].append((record[4], record[5]))

    total = sum(len(v) for v in series.values())
    print(
        f"\nForward prediction of the FX return over the next "
        f"{FORWARD_MINUTES} minutes"
    )
    print(
        f"{total} observations across {len(series)} relationships\n"
    )

    print("QUESTION 1 -- which horizon predicts the next move?\n")
    for name in (
        "return_15m",
        "return_60m",
        "return_240m",
        "commodity_impulse",
    ):
        print(f"  {name}")
        report(name, pooled[name], stride)

    print(
        "QUESTION 2 -- does divergence predict the move it bets on?\n"
        "  A positive beta means the currency does catch up, which is the\n"
        "  strategy's premise. Zero means it is trading noise. Negative\n"
        "  means it is trading the wrong way round.\n"
    )
    print("  divergence_score")
    report("divergence_score", pooled["divergence_score"], stride)

    print("Per relationship, divergence -> forward FX (independent sample):")
    for relationship_id, records in sorted(series.items()):
        pairs = [
            (r[4], r[5]) for r in records if not math.isnan(r[4])
        ][::stride]
        if len(pairs) < 30:
            continue
        beta, r2, t_stat, n = fit(
            [p[0] for p in pairs], [p[1] for p in pairs]
        )
        print(
            f"  {relationship_id[:34]:34} n={n:4} "
            f"beta={beta:+7.4f} R2={r2:6.4f} t={t_stat:+6.2f}"
        )


if __name__ == "__main__":
    main()
