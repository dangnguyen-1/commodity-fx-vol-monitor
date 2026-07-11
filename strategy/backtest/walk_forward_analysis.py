from __future__ import annotations
from pathlib import Path

import numpy as np
import pandas as pd

from strategy.backtest.run_backtest import (
    INITIAL_CAPITAL,
    calculate_summary,
    load_candidates,
    load_fx_prices,
    run_event_backtest,
)
from strategy.backtest.validate_variants import (
    ExperimentSpec,
    prepare_experiment_candidates,
    slice_period_inputs,
    trim_equity_to_relevant_end,
)


OUTPUT_DIR = Path("strategy/output/backtest")

START_YEAR = 2010
LAST_COMPLETE_YEAR = 2025
FULL_SAMPLE_END_DATE = "2026-12-31"

ROLLING_WINDOW_YEARS = 3

ROUND_TRIP_COST_BPS = 2.0
BOOTSTRAP_SAMPLES = 5_000
BOOTSTRAP_BLOCK_MONTHS = 3
BOOTSTRAP_SEED = 42


CONFIGURATIONS = [
    ExperimentSpec(
        name="baseline_3d",
        family="stability",
        holding_period_days=3,
    ),
    ExperimentSpec(
        name="baseline_5d",
        family="stability",
        holding_period_days=5,
        posthoc_exploratory=True,
    ),
    ExperimentSpec(
        name="short_only_3d",
        family="stability",
        direction_mode="short_only",
        holding_period_days=3,
        posthoc_exploratory=True,
    ),
    ExperimentSpec(
        name="force_short_3d",
        family="stability",
        direction_mode="force_short",
        holding_period_days=3,
        posthoc_exploratory=True,
    ),
    ExperimentSpec(
        name="no_cad_brl_3d",
        family="stability",
        subset="no_cad_brl",
        holding_period_days=3,
        posthoc_exploratory=True,
    ),
]


def run_period(
    base_candidates: pd.DataFrame,
    fx_prices: pd.DataFrame,
    spec: ExperimentSpec,
    start_date: str,
    end_date: str,
) -> tuple[dict, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    prepared = prepare_experiment_candidates(base_candidates, spec)

    period_candidates, period_fx, nominal_end = slice_period_inputs(
        prepared,
        fx_prices,
        start_date,
        end_date,
    )

    trades, equity, decisions = run_event_backtest(
        period_candidates,
        period_fx,
        initial_capital=INITIAL_CAPITAL,
        round_trip_cost_bps=spec.round_trip_cost_bps,
    )

    equity = trim_equity_to_relevant_end(
        equity,
        trades,
        nominal_end,
    )

    summary = calculate_summary(
        trades,
        equity,
        decisions,
        initial_capital=INITIAL_CAPITAL,
        round_trip_cost_bps=spec.round_trip_cost_bps,
    ).iloc[0].to_dict()

    summary.update(
        {
            "configuration": spec.name,
            "start_date": start_date,
            "end_date": end_date,
            "subset": spec.subset,
            "direction_mode": spec.direction_mode,
            "holding_period_days": spec.holding_period_days,
            "posthoc_exploratory": spec.posthoc_exploratory,
            "test_is_untouched": False,
        }
    )

    return summary, trades, equity, decisions


def build_annual_results(
    candidates: pd.DataFrame,
    fx_prices: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict] = []

    total_runs = len(CONFIGURATIONS) * (LAST_COMPLETE_YEAR - START_YEAR + 1)
    run_number = 0

    for spec in CONFIGURATIONS:
        for year in range(START_YEAR, LAST_COMPLETE_YEAR + 1):
            run_number += 1
            print(f"[annual {run_number}/{total_runs}] {spec.name} | {year}")

            start_date = f"{year}-01-01"
            end_date = f"{year}-12-31"

            summary, _, _, _ = run_period(
                candidates,
                fx_prices,
                spec,
                start_date,
                end_date,
            )

            summary["year"] = year
            rows.append(summary)

    annual = pd.DataFrame(rows)

    front_cols = [
        "configuration",
        "year",
        "start_date",
        "end_date",
        "total_trades",
        "total_pnl_usd",
        "sharpe_ratio",
        "max_drawdown_pct",
        "win_rate_pct",
        "profit_factor",
        "ending_equity",
        "posthoc_exploratory",
        "test_is_untouched",
    ]

    front_cols = [col for col in front_cols if col in annual.columns]
    remaining_cols = [col for col in annual.columns if col not in front_cols]

    return annual[front_cols + remaining_cols]


def build_rolling_results(
    candidates: pd.DataFrame,
    fx_prices: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict] = []

    windows = [
        (start_year, start_year + ROLLING_WINDOW_YEARS - 1)
        for start_year in range(
            START_YEAR,
            LAST_COMPLETE_YEAR - ROLLING_WINDOW_YEARS + 2,
        )
    ]

    total_runs = len(CONFIGURATIONS) * len(windows)
    run_number = 0

    for spec in CONFIGURATIONS:
        for start_year, end_year in windows:
            run_number += 1
            print(
                f"[rolling {run_number}/{total_runs}] "
                f"{spec.name} | {start_year}-{end_year}"
            )

            start_date = f"{start_year}-01-01"
            end_date = f"{end_year}-12-31"

            summary, _, _, _ = run_period(
                candidates,
                fx_prices,
                spec,
                start_date,
                end_date,
            )

            summary["window_start_year"] = start_year
            summary["window_end_year"] = end_year
            summary["window_label"] = f"{start_year}-{end_year}"
            rows.append(summary)

    rolling = pd.DataFrame(rows)

    front_cols = [
        "configuration",
        "window_label",
        "window_start_year",
        "window_end_year",
        "total_trades",
        "total_pnl_usd",
        "sharpe_ratio",
        "max_drawdown_pct",
        "win_rate_pct",
        "profit_factor",
        "ending_equity",
        "posthoc_exploratory",
        "test_is_untouched",
    ]

    front_cols = [col for col in front_cols if col in rolling.columns]
    remaining_cols = [col for col in rolling.columns if col not in front_cols]

    return rolling[front_cols + remaining_cols]


def summarize_stability(
    annual: pd.DataFrame,
    rolling: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict] = []

    for configuration, annual_group in annual.groupby("configuration"):
        rolling_group = rolling[
            rolling["configuration"] == configuration
        ].copy()

        best_annual_idx = annual_group["total_pnl_usd"].idxmax()
        worst_annual_idx = annual_group["total_pnl_usd"].idxmin()

        best_rolling_idx = rolling_group["total_pnl_usd"].idxmax()
        worst_rolling_idx = rolling_group["total_pnl_usd"].idxmin()

        rows.append(
            {
                "configuration": configuration,
                "annual_periods": len(annual_group),
                "profitable_years": int(
                    (annual_group["total_pnl_usd"] > 0).sum()
                ),
                "profitable_year_pct": (
                    (annual_group["total_pnl_usd"] > 0).mean() * 100
                ),
                "annual_total_pnl_usd": annual_group["total_pnl_usd"].sum(),
                "median_annual_pnl_usd": annual_group["total_pnl_usd"].median(),
                "best_year": int(annual.loc[best_annual_idx, "year"]),
                "best_year_pnl_usd": annual.loc[
                    best_annual_idx,
                    "total_pnl_usd",
                ],
                "worst_year": int(annual.loc[worst_annual_idx, "year"]),
                "worst_year_pnl_usd": annual.loc[
                    worst_annual_idx,
                    "total_pnl_usd",
                ],
                "rolling_3y_periods": len(rolling_group),
                "profitable_rolling_3y_windows": int(
                    (rolling_group["total_pnl_usd"] > 0).sum()
                ),
                "profitable_rolling_3y_pct": (
                    (rolling_group["total_pnl_usd"] > 0).mean() * 100
                ),
                "median_rolling_3y_pnl_usd": (
                    rolling_group["total_pnl_usd"].median()
                ),
                "best_rolling_3y_window": rolling.loc[
                    best_rolling_idx,
                    "window_label",
                ],
                "best_rolling_3y_pnl_usd": rolling.loc[
                    best_rolling_idx,
                    "total_pnl_usd",
                ],
                "worst_rolling_3y_window": rolling.loc[
                    worst_rolling_idx,
                    "window_label",
                ],
                "worst_rolling_3y_pnl_usd": rolling.loc[
                    worst_rolling_idx,
                    "total_pnl_usd",
                ],
            }
        )

    return pd.DataFrame(rows).sort_values(
        "profitable_rolling_3y_pct",
        ascending=False,
    )


def monthly_pnl_from_equity(equity: pd.DataFrame) -> pd.Series:
    if equity.empty:
        return pd.Series(dtype=float)

    monthly = equity.copy()
    monthly["date"] = pd.to_datetime(monthly["date"])
    monthly["daily_pnl_usd"] = monthly["equity_usd"].diff()

    monthly.loc[monthly.index[0], "daily_pnl_usd"] = (
        monthly.loc[monthly.index[0], "equity_usd"]
        - INITIAL_CAPITAL
    )

    monthly["month"] = monthly["date"].dt.to_period("M")

    monthly_pnl = (
        monthly.groupby("month")["daily_pnl_usd"]
        .sum()
        .sort_index()
    )

    # Exclude the last month when the dataset ends before that
    # calendar month is complete.
    last_date = monthly["date"].max()
    last_month = last_date.to_period("M")
    last_calendar_date = last_month.end_time.normalize()

    if last_date.normalize() < last_calendar_date:
        monthly_pnl = monthly_pnl[
            monthly_pnl.index < last_month
        ]

    return monthly_pnl


def moving_block_bootstrap_means(
    values: np.ndarray,
    *,
    samples: int,
    block_length: int,
    seed: int,
) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]

    n = len(values)
    if n == 0:
        return np.array([], dtype=float)

    block_length = max(1, min(block_length, n))
    rng = np.random.default_rng(seed)

    possible_starts = np.arange(n - block_length + 1)
    bootstrap_means = np.empty(samples, dtype=float)

    blocks_needed = int(np.ceil(n / block_length))

    for sample_idx in range(samples):
        starts = rng.choice(
            possible_starts,
            size=blocks_needed,
            replace=True,
        )

        sampled = np.concatenate(
            [values[start:start + block_length] for start in starts]
        )[:n]

        bootstrap_means[sample_idx] = sampled.mean()

    return bootstrap_means


def build_bootstrap_report(
    candidates: pd.DataFrame,
    fx_prices: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict] = []

    for index, spec in enumerate(CONFIGURATIONS, start=1):
        print(
            f"[bootstrap {index}/{len(CONFIGURATIONS)}] "
            f"{spec.name}"
        )

        _, _, equity, _ = run_period(
            candidates,
            fx_prices,
            spec,
            f"{START_YEAR}-01-01",
            FULL_SAMPLE_END_DATE,
        )

        monthly_pnl = monthly_pnl_from_equity(equity)

        bootstrap_means = moving_block_bootstrap_means(
            monthly_pnl.to_numpy(),
            samples=BOOTSTRAP_SAMPLES,
            block_length=BOOTSTRAP_BLOCK_MONTHS,
            seed=BOOTSTRAP_SEED + index,
        )

        if len(bootstrap_means) == 0:
            lower = np.nan
            upper = np.nan
            probability_positive = np.nan
        else:
            lower, upper = np.quantile(
                bootstrap_means,
                [0.025, 0.975],
            )
            probability_positive = (
                (bootstrap_means > 0).mean() * 100
            )

        rows.append(
            {
                "configuration": spec.name,
                "months": len(monthly_pnl),
                "observed_mean_monthly_pnl_usd": monthly_pnl.mean(),
                "observed_median_monthly_pnl_usd": monthly_pnl.median(),
                "profitable_month_pct": (
                    (monthly_pnl > 0).mean() * 100
                    if len(monthly_pnl)
                    else np.nan
                ),
                "bootstrap_block_months": BOOTSTRAP_BLOCK_MONTHS,
                "bootstrap_samples": BOOTSTRAP_SAMPLES,
                "bootstrap_mean_monthly_pnl_ci_lower_usd": lower,
                "bootstrap_mean_monthly_pnl_ci_upper_usd": upper,
                "bootstrap_probability_mean_positive_pct": (
                    probability_positive
                ),
                "ci_excludes_zero": bool(
                    pd.notna(lower)
                    and pd.notna(upper)
                    and (lower > 0 or upper < 0)
                ),
                "posthoc_exploratory": spec.posthoc_exploratory,
                "test_is_untouched": False,
            }
        )

    return pd.DataFrame(rows)


def build_paired_direction_report(
    candidates: pd.DataFrame,
    fx_prices: pd.DataFrame,
) -> pd.DataFrame:
    baseline_spec = next(
        spec for spec in CONFIGURATIONS
        if spec.name == "baseline_3d"
    )
    forced_short_spec = next(
        spec for spec in CONFIGURATIONS
        if spec.name == "force_short_3d"
    )

    _, _, baseline_equity, _ = run_period(
        candidates,
        fx_prices,
        baseline_spec,
        f"{START_YEAR}-01-01",
        FULL_SAMPLE_END_DATE,
    )

    _, _, forced_short_equity, _ = run_period(
        candidates,
        fx_prices,
        forced_short_spec,
        f"{START_YEAR}-01-01",
        FULL_SAMPLE_END_DATE,
    )

    baseline_monthly = monthly_pnl_from_equity(baseline_equity).rename(
        "baseline_monthly_pnl_usd"
    )
    forced_short_monthly = monthly_pnl_from_equity(
        forced_short_equity
    ).rename("force_short_monthly_pnl_usd")

    paired = pd.concat(
        [baseline_monthly, forced_short_monthly],
        axis=1,
        join="inner",
    ).dropna()

    paired["force_short_minus_baseline_usd"] = (
        paired["force_short_monthly_pnl_usd"]
        - paired["baseline_monthly_pnl_usd"]
    )

    differences = paired[
        "force_short_minus_baseline_usd"
    ].to_numpy()

    bootstrap_means = moving_block_bootstrap_means(
        differences,
        samples=BOOTSTRAP_SAMPLES,
        block_length=BOOTSTRAP_BLOCK_MONTHS,
        seed=BOOTSTRAP_SEED + 100,
    )

    lower, upper = (
        np.quantile(bootstrap_means, [0.025, 0.975])
        if len(bootstrap_means)
        else (np.nan, np.nan)
    )

    summary = pd.DataFrame(
        [
            {
                "comparison": "force_short_3d_minus_baseline_3d",
                "paired_months": len(paired),
                "observed_mean_monthly_difference_usd": (
                    paired["force_short_minus_baseline_usd"].mean()
                ),
                "observed_median_monthly_difference_usd": (
                    paired["force_short_minus_baseline_usd"].median()
                ),
                "force_short_better_month_pct": (
                    (
                        paired["force_short_minus_baseline_usd"] > 0
                    ).mean()
                    * 100
                ),
                "bootstrap_block_months": BOOTSTRAP_BLOCK_MONTHS,
                "bootstrap_samples": BOOTSTRAP_SAMPLES,
                "bootstrap_mean_difference_ci_lower_usd": lower,
                "bootstrap_mean_difference_ci_upper_usd": upper,
                "bootstrap_probability_force_short_better_pct": (
                    (bootstrap_means > 0).mean() * 100
                    if len(bootstrap_means)
                    else np.nan
                ),
                "ci_excludes_zero": bool(
                    pd.notna(lower)
                    and pd.notna(upper)
                    and (lower > 0 or upper < 0)
                ),
                "posthoc_exploratory": True,
                "test_is_untouched": False,
            }
        ]
    )

    paired = paired.reset_index()
    paired["month"] = paired["month"].astype(str)

    return summary, paired


def main() -> None:
    candidates = load_candidates()
    fx_prices = load_fx_prices()

    annual = build_annual_results(candidates, fx_prices)
    rolling = build_rolling_results(candidates, fx_prices)
    stability = summarize_stability(annual, rolling)
    bootstrap = build_bootstrap_report(candidates, fx_prices)
    paired_summary, paired_monthly = build_paired_direction_report(
        candidates,
        fx_prices,
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    annual.to_csv(
        OUTPUT_DIR / "walk_forward_annual.csv",
        index=False,
    )
    rolling.to_csv(
        OUTPUT_DIR / "walk_forward_rolling_3y.csv",
        index=False,
    )
    stability.to_csv(
        OUTPUT_DIR / "walk_forward_stability_summary.csv",
        index=False,
    )
    bootstrap.to_csv(
        OUTPUT_DIR / "walk_forward_bootstrap.csv",
        index=False,
    )
    paired_summary.to_csv(
        OUTPUT_DIR / "walk_forward_paired_direction_summary.csv",
        index=False,
    )
    paired_monthly.to_csv(
        OUTPUT_DIR / "walk_forward_paired_direction_monthly.csv",
        index=False,
    )

    print(f"\nSaved walk-forward outputs to {OUTPUT_DIR}")

    print("\nStability summary:")
    print(stability.to_string(index=False))

    print("\nBootstrap summary:")
    print(bootstrap.to_string(index=False))

    print("\nPaired direction comparison:")
    print(paired_summary.to_string(index=False))

    print(
        "\nImportant: these configurations are diagnostic and several are "
        "post-hoc. The 2023-2026 period is not an untouched test set."
    )


if __name__ == "__main__":
    main()