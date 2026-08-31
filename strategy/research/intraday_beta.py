"""Measures the intraday transmission coefficient per relationship.

THE QUESTION
------------
The spec assumes a fixed +1/-1 `relationship_direction`: a standardized
commodity move is expected to produce an equally large standardized FX move.
The daily screen showed that is badly wrong on daily data -- the strongest
relationship transmits at 0.42 and most at under 0.1.

But daily magnitudes do not transfer to intraday. The Epps effect lowers
measured correlation as sampling frequency rises, the two legs trade on
venues with non-overlapping sessions, and microstructure noise biases
high-frequency estimates toward zero. Using a daily beta to set an intraday
parameter would be exactly the mistake the daily screen's own docstring warns
against.

So this measures it directly, at the horizon the strategy actually trades,
from feature snapshots built by the real engine.

WHAT IT REGRESSES
-----------------
`observed_fx_impulse` on `commodity_impulse`, both of which are already
volatility-normalized by the feature builder. Beta on standardized inputs is
the number directly comparable to the spec's +/-1 -- it is what the spec is
implicitly asserting equals 1.

Run replay_feature_distribution.py first to populate the snapshots.

Usage:
    .venv/bin/python3 -m strategy.research.intraday_beta \\
        --database /tmp/replay.db --compare-daily /tmp/screen.csv
"""

from __future__ import annotations

import argparse
import csv
import math
import sqlite3
import statistics
from pathlib import Path

REPLAY_RUN_ID = "commodity_fx_intraday-threshold-replay"


def ols(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    n = len(xs)
    if n < 10:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    beta = sxy / sxx
    r2 = 0.0 if syy <= 0 else (sxy * sxy) / (sxx * syy)
    return beta, r2


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure intraday beta per relationship."
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--run-id", default=REPLAY_RUN_ID)
    parser.add_argument(
        "--compare-daily",
        type=Path,
        default=None,
        help="CSV from daily_relationship_screen, to sit alongside.",
    )
    args = parser.parse_args()

    connection = sqlite3.connect(args.database)
    rows = connection.execute(
        """
        SELECT relationship_id, commodity_impulse, observed_fx_impulse
        FROM feature_snapshots
        WHERE run_id = ?
          AND market_data_complete = 1
          AND commodity_impulse IS NOT NULL
          AND observed_fx_impulse IS NOT NULL
        """,
        (args.run_id,),
    ).fetchall()
    connection.close()

    if not rows:
        raise SystemExit(
            "No complete snapshots for that run id. "
            "Run replay_feature_distribution.py first."
        )

    grouped: dict[str, tuple[list[float], list[float]]] = {}
    for relationship_id, commodity, fx in rows:
        xs, ys = grouped.setdefault(str(relationship_id), ([], []))
        xs.append(float(commodity))
        ys.append(float(fx))

    daily: dict[str, dict[str, str]] = {}
    if args.compare_daily and args.compare_daily.exists():
        with args.compare_daily.open(encoding="utf-8") as handle:
            for record in csv.DictReader(handle):
                daily[record["relationship_id"]] = record

    print(
        f"\nIntraday transmission coefficient, {len(rows)} snapshots "
        f"across {len(grouped)} relationships"
    )
    print(
        "beta of observed_fx_impulse on commodity_impulse "
        "(both volatility-normalized)\n"
    )
    header = (
        f"{'relationship':34} {'n':>5} {'intraday_b':>11} {'R2':>7} "
        f"{'daily_b':>8} {'ratio':>7}"
    )
    print(header)
    print("-" * len(header))

    results = []
    for relationship_id, (xs, ys) in sorted(grouped.items()):
        fit = ols(xs, ys)
        if fit is None:
            continue
        beta, r2 = fit
        daily_row = daily.get(relationship_id)
        daily_beta = (
            float(daily_row["beta_standardized"])
            if daily_row and daily_row.get("beta_standardized")
            else None
        )
        # How much of the daily relationship survives at this frequency. The
        # Epps effect predicts well under 1; a ratio near or above 1 would
        # mean the daily estimate is not costing us anything here.
        ratio = (
            beta / daily_beta
            if daily_beta not in (None, 0)
            else None
        )
        results.append((relationship_id, len(xs), beta, r2, daily_beta, ratio))

    for relationship_id, n, beta, r2, daily_beta, ratio in sorted(
        results, key=lambda r: -abs(r[2])
    ):
        db = f"{daily_beta:8.3f}" if daily_beta is not None else "       -"
        rt = f"{ratio:7.2f}" if ratio is not None else "      -"
        print(
            f"{relationship_id[:34]:34} {n:5} {beta:11.3f} {r2:7.4f} {db} {rt}"
        )

    betas = [r[2] for r in results]
    ratios = [r[5] for r in results if r[5] is not None]
    print()
    print(f"intraday beta: median {statistics.median(betas):.3f}, "
          f"max {max(betas, key=abs):.3f}")
    print(
        "The spec assumes this quantity is 1.0 for every relationship."
    )
    if ratios:
        print(
            f"intraday/daily ratio: median {statistics.median(ratios):.2f} "
            f"(below 1 is the Epps effect; the daily screen overstates "
            f"intraday transmission by roughly "
            f"{1 / statistics.median(ratios):.1f}x)"
        )


if __name__ == "__main__":
    main()
