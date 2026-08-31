"""Replays feature snapshots over stored history to see what v0.2.0 produces.

WHY
---
Correcting the volatility normalization in v0.2.0 moved every quantity the
entry and exit thresholds are expressed in. Divergence scores roughly
doubled on the handful of snapshots available at the time, and one
relationship changed sign. So thresholds like

    minimum_absolute_divergence_score: 0.75
    close_when_absolute_divergence_below: 0.25

no longer mean what they meant when they were chosen, and the spec records
them as `pending_rederivation_against_v0_2_0_features`.

Re-deriving them needs a distribution, not five points. This walks the
evaluation grid over whatever 1-minute history is stored, builds features at
each boundary under the current spec, and reports what the distribution
actually looks like -- so a threshold can be picked as a percentile of
observed values rather than guessed.

WHAT THIS IS NOT
----------------
A backtest. Nothing here simulates entries, fills or P&L; it only measures
the scale of the inputs. And the sample is whatever history happens to be
stored, which is currently a handful of days rather than months -- enough
for a first read on scale, not enough to freeze numbers for an eight-week
run that forbids changes once started.

Usage:
    .venv/bin/python3 -m strategy.research.replay_feature_distribution \\
        --database /tmp/replay.db --start 2026-07-13 --end 2026-07-17
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from paper_trading.features.build_feature_snapshots import (
    build_feature_snapshots,
)
from strategy.config.intraday.load_intraday_spec import (
    DEFAULT_SPEC_PATH,
    load_intraday_spec,
)


REPLAY_RUN_ID = "commodity_fx_intraday-threshold-replay"


def percentiles(values: list[float], points: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    out: dict[str, float] = {}
    for p in points:
        index = min(
            len(ordered) - 1,
            max(0, int(round(p / 100.0 * (len(ordered) - 1)))),
        )
        out[f"p{p:g}"] = ordered[index]
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay features over stored history."
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC_PATH)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD (UTC)")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD (UTC)")
    parser.add_argument(
        "--step-minutes",
        type=int,
        default=None,
        help="Defaults to the spec's evaluation interval.",
    )
    args = parser.parse_args()

    spec = load_intraday_spec(args.spec)
    step = args.step_minutes or int(
        spec["signals"]["evaluation"]["interval_minutes"]
    )

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(
        tzinfo=timezone.utc
    ) + timedelta(days=1)

    print(
        f"Replaying {args.start} -> {args.end} every {step}m "
        f"under spec v{spec['strategy']['specification_version']}"
    )

    stamp = start
    built = 0
    failed = 0
    while stamp < end:
        try:
            build_feature_snapshots(
                database_path=args.database,
                spec_path=args.spec,
                evaluation_timestamp=stamp,
                run_id=REPLAY_RUN_ID,
                run_mode="local_replay",
            )
            built += 1
        except Exception:  # noqa: BLE001
            # Boundaries with no usable bars are expected and uninteresting;
            # the point is the distribution over the ones that worked.
            failed += 1
        stamp += timedelta(minutes=step)
        if (built + failed) % 200 == 0:
            print(f"  {built} built, {failed} skipped...", flush=True)

    print(f"\n{built} evaluation points built, {failed} skipped.\n")

    connection = sqlite3.connect(args.database)
    rows = connection.execute(
        """
        SELECT commodity_impulse, expected_fx_impulse,
               observed_fx_impulse, divergence_score
        FROM feature_snapshots
        WHERE run_id = ? AND market_data_complete = 1
        """,
        (REPLAY_RUN_ID,),
    ).fetchall()
    connection.close()

    if not rows:
        print("No complete snapshots were produced - nothing to summarise.")
        print("The stored history probably lacks the 240 minutes of")
        print("continuous bars a complete feature requires.")
        raise SystemExit(1)

    print(f"complete snapshots: {len(rows)}\n")
    columns = {
        "commodity_impulse": [r[0] for r in rows if r[0] is not None],
        "expected_fx_impulse": [r[1] for r in rows if r[1] is not None],
        "observed_fx_impulse": [r[2] for r in rows if r[2] is not None],
        "divergence_score": [r[3] for r in rows if r[3] is not None],
        "abs_divergence": [abs(r[3]) for r in rows if r[3] is not None],
    }

    points = [1, 5, 25, 50, 75, 90, 95, 99]
    header = f"{'quantity':22} {'n':>6} {'mean':>8} {'sd':>8} " + " ".join(
        f"{'p'+str(p):>8}" for p in points
    )
    print(header)
    print("-" * len(header))
    for name, values in columns.items():
        if not values:
            continue
        pcts = percentiles(values, points)
        row = (
            f"{name:22} {len(values):6} "
            f"{statistics.fmean(values):8.3f} "
            f"{(statistics.pstdev(values) if len(values) > 1 else 0):8.3f} "
        )
        row += " ".join(f"{pcts[f'p{p:g}']:8.3f}" for p in points)
        print(row)

    # What the current thresholds would actually do against this sample.
    entry = spec["signals"]["entry_modes"]
    absd = columns["abs_divergence"]
    print("\nCurrent thresholds against this sample:")
    for mode in ("divergence", "confirmed"):
        gate = float(entry[mode]["minimum_absolute_divergence_score"])
        hits = sum(1 for v in absd if v >= gate)
        print(
            f"  {mode:11} |divergence| >= {gate:.2f}: "
            f"{hits}/{len(absd)} = {100.0 * hits / len(absd):.1f}% of points"
        )
    close_gate = float(
        spec["exits"]["divergence_convergence"][
            "close_when_absolute_divergence_below"
        ]
    )
    below = sum(1 for v in absd if v < close_gate)
    print(
        f"  exit        |divergence| <  {close_gate:.2f}: "
        f"{below}/{len(absd)} = {100.0 * below / len(absd):.1f}% of points"
    )


if __name__ == "__main__":
    main()
