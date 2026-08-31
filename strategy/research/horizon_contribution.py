"""Measures what each return horizon contributes to predicting the FX move.

THE QUESTION
------------
The spec blends three commodity return horizons into one impulse:

    0.50 * normalized_return_15m
  + 0.30 * normalized_return_60m
  + 0.20 * normalized_return_240m

Those weights have never been tested. And the horizon-matched beta check
turned up a suggestive result: regressing the 15-minute FX return on the
15-minute commodity return alone gives a *higher* R2 than regressing it on
the blend -- 0.304 against 0.229 for Gold/AUD, 0.309 against 0.189 for
Copper. If a single term beats the blend that contains it, the other terms
are adding more noise than signal.

WHAT THIS REPORTS
-----------------
Per relationship, the univariate R2 of each horizon on its own, then the
joint fit with all three, and the least-squares weights that fit implies
(rescaled to sum to 1 so they sit alongside the spec's).

A caveat that matters: those implied weights are in-sample. Three
coefficients on thousands of observations is not a serious overfitting risk,
but the sample is five days in one regime, and weights fitted to a single
regime are exactly the kind of parameter that flatters a backtest. Read the
univariate columns as the finding; treat the implied weights as a direction,
not a prescription.

Usage:
    .venv/bin/python3 -m strategy.research.horizon_contribution \\
        --database /tmp/replay.db --min-abs-beta 0.35
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
from pathlib import Path


REPLAY_RUN_ID = "commodity_fx_intraday-threshold-replay"
HORIZONS = ("15m", "60m", "240m")


def univariate(xs: list[float], ys: list[float]) -> tuple[float, float]:
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx <= 0 or syy <= 0:
        return 0.0, 0.0
    return sxy / sxx, (sxy * sxy) / (sxx * syy)


def multiple_regression(
    columns: list[list[float]],
    ys: list[float],
) -> tuple[list[float], float] | None:
    """Least squares with an intercept, solved by Gaussian elimination.

    Three predictors, so the normal equations are small enough to solve
    directly rather than pulling in a linear algebra dependency.
    """
    k = len(columns)
    n = len(ys)
    design = [[1.0] + [columns[j][i] for j in range(k)] for i in range(n)]
    size = k + 1

    ata = [[0.0] * size for _ in range(size)]
    atb = [0.0] * size
    for i in range(n):
        row = design[i]
        for a in range(size):
            atb[a] += row[a] * ys[i]
            for b in range(size):
                ata[a][b] += row[a] * row[b]

    for col in range(size):
        pivot = max(range(col, size), key=lambda r: abs(ata[r][col]))
        if abs(ata[pivot][col]) < 1e-12:
            return None
        ata[col], ata[pivot] = ata[pivot], ata[col]
        atb[col], atb[pivot] = atb[pivot], atb[col]
        for r in range(size):
            if r == col:
                continue
            factor = ata[r][col] / ata[col][col]
            for c in range(col, size):
                ata[r][c] -= factor * ata[col][c]
            atb[r] -= factor * atb[col]

    solution = [atb[i] / ata[i][i] for i in range(size)]
    coefficients = solution[1:]

    my = sum(ys) / n
    ss_total = sum((y - my) ** 2 for y in ys)
    ss_residual = 0.0
    for i in range(n):
        predicted = solution[0] + sum(
            coefficients[j] * columns[j][i] for j in range(k)
        )
        ss_residual += (ys[i] - predicted) ** 2
    r2 = 0.0 if ss_total <= 0 else 1.0 - ss_residual / ss_total
    return coefficients, r2


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Per-horizon contribution to FX prediction."
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
        SELECT relationship_id,
               normalized_commodity_return_15m,
               normalized_commodity_return_60m,
               normalized_commodity_return_240m,
               normalized_fx_return_15m
        FROM feature_snapshots
        WHERE run_id = ?
          AND market_data_complete = 1
          AND normalized_commodity_return_15m IS NOT NULL
          AND normalized_commodity_return_60m IS NOT NULL
          AND normalized_commodity_return_240m IS NOT NULL
          AND normalized_fx_return_15m IS NOT NULL
        """,
        (args.run_id,),
    ).fetchall()
    connection.close()

    grouped: dict[str, list[tuple[float, float, float, float]]] = {}
    for relationship_id, r15, r60, r240, fx in rows:
        if keep is not None and str(relationship_id) not in keep:
            continue
        grouped.setdefault(str(relationship_id), []).append(
            (float(r15), float(r60), float(r240), float(fx))
        )

    if not grouped:
        raise SystemExit("No usable snapshots.")

    print(
        f"\nHorizon contribution, {sum(len(v) for v in grouped.values())} "
        f"snapshots across {len(grouped)} relationships"
    )
    print("Spec blend weights: 15m 0.50 / 60m 0.30 / 240m 0.20\n")

    header = (
        f"{'relationship':32} {'n':>5} "
        f"{'R2_15m':>7} {'R2_60m':>7} {'R2_240m':>8} {'R2_all':>7}  "
        f"{'implied weights (15/60/240)':>28}"
    )
    print(header)
    print("-" * len(header))

    implied_totals = [0.0, 0.0, 0.0]
    counted = 0
    for relationship_id, records in sorted(grouped.items()):
        if len(records) < 200:
            continue
        c15 = [r[0] for r in records]
        c60 = [r[1] for r in records]
        c240 = [r[2] for r in records]
        fx = [r[3] for r in records]

        _, r2_15 = univariate(c15, fx)
        _, r2_60 = univariate(c60, fx)
        _, r2_240 = univariate(c240, fx)

        joint = multiple_regression([c15, c60, c240], fx)
        if joint is None:
            continue
        coefficients, r2_all = joint
        total = sum(abs(c) for c in coefficients)
        weights = (
            [abs(c) / total for c in coefficients]
            if total > 0
            else [0.0, 0.0, 0.0]
        )
        for i in range(3):
            implied_totals[i] += weights[i]
        counted += 1

        print(
            f"{relationship_id[:32]:32} {len(records):5} "
            f"{r2_15:7.4f} {r2_60:7.4f} {r2_240:8.4f} {r2_all:7.4f}  "
            f"{weights[0]:8.2f} {weights[1]:8.2f} {weights[2]:9.2f}"
        )

    if counted:
        mean_weights = [t / counted for t in implied_totals]
        print(
            f"\nmean implied weights: 15m {mean_weights[0]:.2f} / "
            f"60m {mean_weights[1]:.2f} / 240m {mean_weights[2]:.2f}"
        )
        print(
            "against the spec's        15m 0.50 / 60m 0.30 / 240m 0.20"
        )
        print(
            "\nIn-sample, five days, one regime. Read the univariate R2 "
            "columns as the finding; the implied weights are a direction, "
            "not a prescription."
        )


if __name__ == "__main__":
    main()
