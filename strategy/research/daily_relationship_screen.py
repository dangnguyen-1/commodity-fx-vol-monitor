"""Screens each commodity-FX relationship against 16 years of daily bars.

WHAT THIS IS FOR
----------------
Handoff section 2.6 proposes replacing the spec's fixed +1/-1 relationship
direction with a rolling beta, and gating signals on whether the relationship
is currently "holding" (its R² / correlation). Those ideas have never been
tested against anything.

This tests their *premise* on daily data, and it is important to be clear
about what that can and cannot establish.

It CANNOT validate the intraday implementation. Daily beta and intraday beta
are different parameters, not one parameter measured at different precision:
the Epps effect means measured correlation falls as sampling frequency rises,
the two legs trade on venues with non-overlapping sessions, and
microstructure noise biases high-frequency beta toward zero. An R² threshold
calibrated here would be the wrong number intraday.

It CAN establish whether the premise is worth pursuing at all. The asymmetry
is what makes it worth running:

    negative results transfer, positive results do not.

A relationship whose beta is indistinguishable from zero, or wildly unstable,
across sixteen years of daily data is not going to be rescued by a 5-minute
refinement. And if beta turns out to sit near 1 and barely move, then the
spec's fixed +/-1 is a fine approximation and change #1 is solving a problem
that does not exist -- which is worth knowing in an afternoon rather than
after a two-month paper run.

WHAT IT REPORTS
---------------
Per relationship, so the shape of the evidence is visible rather than
compressed into a verdict somebody has to trust:

  n_days          overlapping daily observations
  beta_full       beta of FX return on commodity return, whole sample
  r2_full         R² of that fit
  beta_mean/sd    mean and standard deviation of beta across non-overlapping
                  windows -- the stability question
  beta_stability  |mean| / sd. Above ~2 the sign is dependable; below ~1 the
                  rolling estimate is mostly noise
  pct_windows_    share of windows whose R² clears the gate proposed in the
  above_r2        handoff (default 0.15)
  breakdown_runs  how often R² drops below the gate, and for how long -- the
                  premise of change #3

Usage (on the VM, where DATABASE_URL points at the collector Postgres):
    .venv/bin/python3 -m strategy.research.daily_relationship_screen
    .venv/bin/python3 -m strategy.research.daily_relationship_screen --csv out.csv
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import statistics
from dataclasses import dataclass, asdict
from pathlib import Path

import psycopg2
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = (
    PROJECT_ROOT / "paper_trading" / "data" / "paper_trading.db"
)

# The R² gate proposed in handoff 2.6. Treated as a value to *measure
# against*, not one to adopt -- see the module docstring on why a threshold
# derived from daily data would be the wrong number intraday.
DEFAULT_R2_GATE = 0.15

# Beta is re-estimated over non-overlapping windows of this many trading
# days. Non-overlapping matters: rolling windows share observations, so their
# betas are autocorrelated by construction and would look far more stable
# than they are.
DEFAULT_WINDOW_DAYS = 60


@dataclass
class Screen:
    relationship_id: str
    commodity_symbol: str
    fx_symbol: str
    n_days: int
    n_windows: int
    beta_full: float | None
    beta_standardized: float | None
    r2_full: float | None
    beta_mean: float | None
    beta_sd: float | None
    beta_stability: float | None
    beta_sign_flips: int
    pct_windows_above_r2: float | None
    median_window_r2: float | None
    breakdown_runs: int
    median_breakdown_len: float | None


def ols(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    """Slope and R² of y on x. None when the fit is degenerate."""
    n = len(xs)
    if n < 3:
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


def log_returns(series: list[tuple[str, float]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for (_, prev), (day, cur) in zip(series, series[1:]):
        if prev > 0 and cur > 0:
            out[day] = math.log(cur / prev)
    return out


def load_daily(cursor, symbol: str) -> list[tuple[str, float]]:
    cursor.execute(
        """
        SELECT to_char(to_timestamp(timestamp) AT TIME ZONE 'UTC','YYYY-MM-DD'),
               close
        FROM market_data
        WHERE symbol = %s AND timeframe = '1D'
        ORDER BY timestamp
        """,
        (symbol,),
    )
    # One row per calendar date; later rows win if a date somehow repeats.
    seen: dict[str, float] = {}
    for day, close in cursor.fetchall():
        if close is not None and float(close) > 0:
            seen[str(day)] = float(close)
    return sorted(seen.items())


def screen_relationship(
    cursor,
    *,
    relationship_id: str,
    commodity_symbol: str,
    fx_symbol: str,
    window_days: int,
    r2_gate: float,
) -> Screen:
    commodity = log_returns(load_daily(cursor, commodity_symbol))
    fx = log_returns(load_daily(cursor, fx_symbol))

    days = sorted(set(commodity) & set(fx))
    xs = [commodity[d] for d in days]
    ys = [fx[d] for d in days]

    full = ols(xs, ys)
    betas: list[float] = []
    window_r2: list[float] = []
    for start in range(0, len(days) - window_days + 1, window_days):
        chunk = ols(
            xs[start : start + window_days],
            ys[start : start + window_days],
        )
        if chunk is not None:
            betas.append(chunk[0])
            window_r2.append(chunk[1])

    beta_mean = statistics.fmean(betas) if betas else None
    beta_sd = statistics.pstdev(betas) if len(betas) > 1 else None
    stability = (
        abs(beta_mean) / beta_sd
        if beta_mean is not None and beta_sd not in (None, 0)
        else None
    )
    sign_flips = sum(
        1
        for a, b in zip(betas, betas[1:])
        if (a > 0) != (b > 0)
    )

    # How often the relationship stops holding, and for how long. Consecutive
    # sub-gate windows are one breakdown, not several.
    runs: list[int] = []
    current = 0
    for r2 in window_r2:
        if r2 < r2_gate:
            current += 1
        elif current:
            runs.append(current)
            current = 0
    if current:
        runs.append(current)

    return Screen(
        relationship_id=relationship_id,
        commodity_symbol=commodity_symbol,
        fx_symbol=fx_symbol,
        n_days=len(days),
        n_windows=len(betas),
        beta_full=None if full is None else round(full[0], 4),
        # The strategy never compares raw returns -- both legs are divided by
        # their own volatility first. Beta on standardized returns is just the
        # correlation, and that is the number directly comparable to the
        # spec's assumed +/-1 relationship_direction. Raw beta above is
        # reported too, but it is in the wrong units for that comparison.
        beta_standardized=(
            None
            if full is None
            else round(math.copysign(math.sqrt(full[1]), full[0]), 4)
        ),
        r2_full=None if full is None else round(full[1], 4),
        beta_mean=None if beta_mean is None else round(beta_mean, 4),
        beta_sd=None if beta_sd is None else round(beta_sd, 4),
        beta_stability=None if stability is None else round(stability, 2),
        beta_sign_flips=sign_flips,
        pct_windows_above_r2=(
            round(
                100.0
                * sum(1 for r in window_r2 if r >= r2_gate)
                / len(window_r2),
                1,
            )
            if window_r2
            else None
        ),
        median_window_r2=(
            round(statistics.median(window_r2), 4) if window_r2 else None
        ),
        breakdown_runs=len(runs),
        median_breakdown_len=(
            round(statistics.median(runs), 1) if runs else None
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Screen relationships against daily history."
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--r2-gate", type=float, default=DEFAULT_R2_GATE)
    parser.add_argument("--csv", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is not set.")

    sqlite_connection = sqlite3.connect(args.database)
    registry = sqlite_connection.execute(
        """
        SELECT relationship_id, live_commodity_symbol, live_fx_symbol, active
        FROM live_instrument_registry
        ORDER BY relationship_id
        """
    ).fetchall()
    sqlite_connection.close()

    postgres = psycopg2.connect(database_url)
    results: list[Screen] = []
    try:
        with postgres.cursor() as cursor:
            for relationship_id, commodity, fx, active in registry:
                if not int(active):
                    continue
                results.append(
                    screen_relationship(
                        cursor,
                        relationship_id=str(relationship_id),
                        commodity_symbol=str(commodity),
                        fx_symbol=str(fx),
                        window_days=args.window_days,
                        r2_gate=args.r2_gate,
                    )
                )
    finally:
        postgres.close()

    header = (
        f"{'relationship':38} {'days':>5} {'win':>4} {'beta':>7} {'std_b':>7} {'R2':>6} "
        f"{'b_mean':>7} {'b_sd':>7} {'stab':>5} {'flip':>4} "
        f"{'%>gate':>6} {'medR2':>6} {'brk':>4}"
    )
    print(
        f"\nDaily relationship screen  "
        f"(window={args.window_days}d non-overlapping, R2 gate={args.r2_gate})"
    )
    print(header)
    print("-" * len(header))
    for row in sorted(
        results,
        key=lambda r: (r.beta_stability is None, -(r.beta_stability or 0)),
    ):
        def fmt(value, width, dp=4):
            return (
                " " * (width - 1) + "-"
                if value is None
                else f"{value:>{width}.{dp}f}"
            )

        print(
            f"{row.relationship_id[:38]:38} {row.n_days:5} {row.n_windows:4} "
            f"{fmt(row.beta_full,7)} {fmt(row.beta_standardized,7)} {fmt(row.r2_full,6)} "
            f"{fmt(row.beta_mean,7)} {fmt(row.beta_sd,7)} "
            f"{fmt(row.beta_stability,5,2)} {row.beta_sign_flips:4} "
            f"{fmt(row.pct_windows_above_r2,6,1)} "
            f"{fmt(row.median_window_r2,6)} {row.breakdown_runs:4}"
        )

    if args.csv:
        import csv

        with args.csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=list(asdict(results[0]).keys())
            )
            writer.writeheader()
            for row in results:
                writer.writerow(asdict(row))
        print(f"\nwrote {args.csv}")

    if args.json:
        args.json.write_text(
            json.dumps([asdict(r) for r in results], indent=2),
            encoding="utf-8",
        )
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
