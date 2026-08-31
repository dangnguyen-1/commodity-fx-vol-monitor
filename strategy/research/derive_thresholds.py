"""Derives entry and exit thresholds from an observed feature distribution.

WHY THESE NEED DERIVING
-----------------------
The thresholds in the spec were chosen against the pre-v0.2.0 normalization
and have been carried, knowingly wrong, through two releases. v0.2.0 fixed
the volatility scaling and roughly doubled divergence scores; v0.3.0 applied
measured transmission betas, which shrank the expected impulse -- by 19x for
Crude Oil/CAD and less for the metals. The numbers in the file have not
meant what they say since before either change.

TWO CONSTRAINTS, NOT ONE
------------------------
Picking a percentile for selectivity alone is not enough. The spec's own
promotion criteria (governance.formal_strategy_review) require 200 closed
trades within 8 calendar weeks, and *both* must be met. A gate tight enough
to look impressively selective can make that arithmetically impossible,
which does not make the strategy better -- it makes the validation
impossible to complete.

So this reports, for each candidate threshold, both the selectivity and the
implied trade count over an 8-week run, and flags which candidates can
actually satisfy the promotion criteria. Sizing the test is legitimate;
tuning to the outcome is not, and these are different things.

The trade estimate is deliberately crude. It caps each relationship at the
number of non-overlapping positions the maximum holding time allows, since
`position_already_open` blocks a second entry on the same relationship --
which is what stops a naive signal count from overstating trades by an order
of magnitude.

Usage:
    .venv/bin/python3 -m strategy.research.derive_thresholds \\
        --database /tmp/replay.db --days-observed 5
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
from pathlib import Path

from strategy.config.intraday.load_intraday_spec import (
    DEFAULT_SPEC_PATH,
    load_intraday_spec,
)


REPLAY_RUN_ID = "commodity_fx_intraday-threshold-replay"
STEP9_TRADES = 200
STEP9_WEEKS = 8
TRADING_DAYS_PER_WEEK = 5


def percentile(ordered: list[float], p: float) -> float:
    if not ordered:
        return float("nan")
    index = min(
        len(ordered) - 1,
        max(0, int(round(p / 100.0 * (len(ordered) - 1)))),
    )
    return ordered[index]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Derive thresholds from observed features."
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC_PATH)
    parser.add_argument("--run-id", default=REPLAY_RUN_ID)
    parser.add_argument(
        "--min-abs-beta",
        type=float,
        default=None,
        help=(
            "Restrict to relationships whose measured |beta| clears this. "
            "A global threshold across relationships with very different "
            "residual variances selects the wrong ones -- see the note "
            "printed with the per-relationship medians."
        ),
    )
    parser.add_argument(
        "--days-observed",
        type=float,
        required=True,
        help="Trading days the replay covers, to scale trade estimates.",
    )
    args = parser.parse_args()

    spec = load_intraday_spec(args.spec)
    holding_minutes = float(spec["exits"]["maximum_holding_time"]["minutes"])
    max_positions = int(spec["risk"]["maximum_simultaneous_positions"])

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
        SELECT relationship_id, divergence_score
        FROM feature_snapshots
        WHERE run_id = ?
          AND market_data_complete = 1
          AND divergence_score IS NOT NULL
        """,
        (args.run_id,),
    ).fetchall()
    connection.close()

    if not rows:
        raise SystemExit(
            "No complete snapshots with a divergence score. Run "
            "replay_feature_distribution.py against this database first."
        )

    by_relationship: dict[str, list[float]] = {}
    for relationship_id, divergence in rows:
        if keep is not None and str(relationship_id) not in keep:
            continue
        by_relationship.setdefault(str(relationship_id), []).append(
            abs(float(divergence))
        )

    everything = sorted(v for values in by_relationship.values() for v in values)
    relationships = len(by_relationship)

    print(
        f"\n{len(everything)} complete snapshots across {relationships} "
        f"relationships, {args.days_observed:g} trading days"
    )
    print(f"spec v{spec['strategy']['specification_version']}\n")

    print("|divergence| distribution")
    for p in (50, 75, 90, 95, 97.5, 99):
        print(f"  p{p:<5g} {percentile(everything, p):7.3f}")

    # A relationship cannot hold two positions at once, so the ceiling on
    # trades per relationship per day is set by the holding limit, not by
    # how often a signal fires.
    max_trades_per_relationship_day = (24 * 60) / holding_minutes
    horizon_days = STEP9_WEEKS * TRADING_DAYS_PER_WEEK

    print(
        f"\nEach relationship can hold at most "
        f"{max_trades_per_relationship_day:.0f} non-overlapping positions a "
        f"day at a {holding_minutes:.0f}-minute limit."
    )
    print(
        f"Step 9 needs {STEP9_TRADES} closed trades within {STEP9_WEEKS} "
        f"weeks (~{horizon_days:.0f} trading days).\n"
    )

    header = (
        f"{'percentile':>10} {'threshold':>10} {'signals/day':>12} "
        f"{'trades/day':>11} {'8wk trades':>11}  verdict"
    )
    print(header)
    print("-" * len(header))

    for p in (50, 75, 90, 95, 97.5, 99):
        threshold = percentile(everything, p)
        # Signals per relationship per day above this threshold, then capped
        # by the holding limit and finally by the portfolio position cap.
        trades_per_day = 0.0
        signals_per_day = 0.0
        for values in by_relationship.values():
            hits = sum(1 for v in values if v >= threshold)
            per_day = hits / args.days_observed
            signals_per_day += per_day
            trades_per_day += min(
                per_day, max_trades_per_relationship_day
            )
        # The book cannot run more than max_positions at once either.
        trades_per_day = min(
            trades_per_day,
            max_positions * max_trades_per_relationship_day,
        )
        eight_week = trades_per_day * horizon_days
        verdict = (
            "meets Step 9"
            if eight_week >= STEP9_TRADES
            else f"short by {STEP9_TRADES - eight_week:.0f}"
        )
        print(
            f"{p:>9g}% {threshold:10.3f} {signals_per_day:12.1f} "
            f"{trades_per_day:11.1f} {eight_week:11.0f}  {verdict}"
        )

    print(
        "\nSignals/day counts every evaluation above the threshold; "
        "trades/day applies the holding-time and position caps. The gap "
        "between them is the share of signals that arrive while the same "
        "relationship is already in a position."
    )

    print(
        "\nPer-relationship |divergence| medians. Watch the ordering: "
        "divergence is expected minus observed, so a relationship whose "
        "beta is near zero contributes no expected term and its divergence "
        "is simply the raw FX move -- which is larger, not smaller, than "
        "for a relationship that explains part of it. A single global "
        "threshold therefore selects hardest for the relationships that "
        "transmit least."
    )
    for relationship_id, values in sorted(
        by_relationship.items(), key=lambda kv: -statistics.median(kv[1])
    ):
        print(
            f"  {relationship_id[:38]:38} n={len(values):5} "
            f"median={statistics.median(values):6.3f} "
            f"p95={percentile(sorted(values), 95):6.3f}"
        )


if __name__ == "__main__":
    main()
