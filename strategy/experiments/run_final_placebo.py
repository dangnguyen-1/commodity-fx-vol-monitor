from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd

import strategy.experiments.run_statistical_robustness as stats


FEATURES_PATH = Path(
    "strategy/output/daily_features.csv"
)

SELECTION_SCHEDULE_PATH = Path(
    "strategy/config/v1/"
    "rolling_selection_schedule.csv"
)

OUTPUT_ROOT = Path(
    "strategy/output/experiments/"
    "final_placebo"
)


PERIOD_NAME = "research_holdout"

PERIOD_START = "2023-01-01"

PERIOD_END = None

SLEEVE = "full"


FINAL_PLACEBO_REPLICATIONS = int(
    os.getenv(
        "STRATEGY_FINAL_PLACEBO_REPLICATIONS",
        "500",
    )
)


def save_baseline_outputs(
    *,
    trades: pd.DataFrame,
    equity: pd.DataFrame,
    decisions: pd.DataFrame,
    summary: pd.DataFrame,
) -> None:
    baseline_dir = (
        OUTPUT_ROOT
        / "baseline"
    )

    baseline_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    trades.to_csv(
        baseline_dir / "trades.csv",
        index=False,
    )

    equity.to_csv(
        baseline_dir / "equity.csv",
        index=False,
    )

    decisions.to_csv(
        baseline_dir / "decisions.csv",
        index=False,
    )

    summary.to_csv(
        baseline_dir / "summary.csv",
        index=False,
    )


def add_monte_carlo_diagnostics(
    *,
    placebo_summary: pd.DataFrame,
    placebo_draws: pd.DataFrame,
) -> pd.DataFrame:
    result = placebo_summary.copy()

    actual_return = float(
        result[
            "actual_total_return_pct"
        ].iloc[0]
    )

    actual_sharpe = float(
        result[
            "actual_sharpe_ratio"
        ].iloc[0]
    )

    actual_profit_factor = float(
        result[
            "actual_profit_factor"
        ].iloc[0]
    )

    return_exceedances = int(
        placebo_draws[
            "total_return_pct"
        ]
        .ge(actual_return)
        .sum()
    )

    sharpe_exceedances = int(
        placebo_draws[
            "sharpe_ratio"
        ]
        .ge(actual_sharpe)
        .sum()
    )

    profit_factor_exceedances = int(
        placebo_draws[
            "profit_factor"
        ]
        .ge(actual_profit_factor)
        .sum()
    )

    result[
        "return_placebo_exceedances"
    ] = return_exceedances

    result[
        "sharpe_placebo_exceedances"
    ] = sharpe_exceedances

    result[
        "profit_factor_placebo_exceedances"
    ] = profit_factor_exceedances

    result[
        "return_empirical_rank"
    ] = (
        len(placebo_draws)
        - return_exceedances
        + 1
    )

    result[
        "sharpe_empirical_rank"
    ] = (
        len(placebo_draws)
        - sharpe_exceedances
        + 1
    )

    result[
        "profit_factor_empirical_rank"
    ] = (
        len(placebo_draws)
        - profit_factor_exceedances
        + 1
    )

    for metric in [
        "total_return_pct",
        "sharpe_ratio",
        "profit_factor",
    ]:
        p_value_column = (
            f"empirical_p_value_{metric}"
        )

        p_value = float(
            result[
                p_value_column
            ].iloc[0]
        )

        result[
            f"monte_carlo_se_{metric}"
        ] = np.sqrt(
            p_value
            * (1.0 - p_value)
            / FINAL_PLACEBO_REPLICATIONS
        )

    return result


def main() -> None:
    if not FEATURES_PATH.exists():
        raise FileNotFoundError(
            f"Missing feature file: "
            f"{FEATURES_PATH}"
        )

    if not SELECTION_SCHEDULE_PATH.exists():
        raise FileNotFoundError(
            "Missing final 2-year schedule: "
            f"{SELECTION_SCHEDULE_PATH}"
        )

    if FINAL_PLACEBO_REPLICATIONS <= 0:
        raise ValueError(
            "FINAL_PLACEBO_REPLICATIONS "
            "must be positive."
        )

    # Force the imported statistical module to use the
    # final v1 configuration for this test.
    stats.SELECTION_SCHEDULE_PATH = (
        SELECTION_SCHEDULE_PATH
    )

    stats.PLACEBO_REPLICATIONS = (
        FINAL_PLACEBO_REPLICATIONS
    )

    stats.PLACEBO_SLEEVES = [
        SLEEVE
    ]

    features = pd.read_csv(
        FEATURES_PATH
    )

    features["date"] = pd.to_datetime(
        features["date"]
    )

    selection_schedule = (
        stats.load_selection_schedule()
    )

    config = stats.VARIANTS[
        stats.VARIANT_NAME
    ]

    candidate_strategy = (
        stats.build_candidate_strategy(
            features,
            variant_name=(
                stats.VARIANT_NAME
            ),
            config=config,
        )
    )

    fx_prices = (
        stats.load_fx_prices()
    )

    period_strategy = stats.slice_period(
        candidate_strategy,
        start_date=PERIOD_START,
        end_date=PERIOD_END,
    )

    period_fx_prices = stats.slice_period(
        fx_prices,
        start_date=PERIOD_START,
        end_date=PERIOD_END,
    )

    weighted_strategy = (
        stats.apply_rolling_selection(
            period_strategy,
            universe_mode=(
                stats.UNIVERSE_MODE
            ),
            selection_schedule=(
                selection_schedule
            ),
        )
    )

    OUTPUT_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    print(
        f"\n{'=' * 78}\n"
        "Final v1 timing placebo\n"
        f"Period: {PERIOD_NAME}\n"
        f"Sleeve: {SLEEVE}\n"
        f"Replications: "
        f"{FINAL_PLACEBO_REPLICATIONS}\n"
        f"Schedule: "
        f"{SELECTION_SCHEDULE_PATH}\n"
        f"{'=' * 78}"
    )

    (
        baseline_trades,
        baseline_equity,
        baseline_decisions,
        baseline_summary,
    ) = stats.run_strategy_case(
        period_name=PERIOD_NAME,
        sleeve=SLEEVE,
        strategy=weighted_strategy,
        fx_prices=period_fx_prices,
    )

    save_baseline_outputs(
        trades=baseline_trades,
        equity=baseline_equity,
        decisions=baseline_decisions,
        summary=baseline_summary,
    )

    baseline_row = (
        baseline_summary.iloc[0]
    )

    print(
        "\nActual frozen strategy:"
    )

    print(
        f"Return: "
        f"{baseline_row['total_return_pct']:.6f}%\n"
        f"Sharpe: "
        f"{baseline_row['sharpe_ratio']:.6f}\n"
        f"Profit factor: "
        f"{baseline_row['profit_factor']:.6f}\n"
        f"Trades: "
        f"{int(baseline_row['total_trades'])}"
    )

    (
        placebo_draws,
        placebo_offsets,
    ) = stats.run_placebo_distribution(
        period_name=PERIOD_NAME,
        unweighted_period_strategy=(
            period_strategy
        ),
        period_fx_prices=(
            period_fx_prices
        ),
        selection_schedule=(
            selection_schedule
        ),
    )

    placebo_summary = (
        stats.summarize_placebos(
            placebo_draws,
            baseline_summary,
        )
    )

    placebo_summary = (
        add_monte_carlo_diagnostics(
            placebo_summary=(
                placebo_summary
            ),
            placebo_draws=(
                placebo_draws
            ),
        )
    )

    run_configuration = pd.DataFrame(
        [
            {
                "period": PERIOD_NAME,
                "period_start": PERIOD_START,
                "period_end": PERIOD_END,
                "sleeve": SLEEVE,
                "variant": (
                    stats.VARIANT_NAME
                ),
                "universe_mode": (
                    stats.UNIVERSE_MODE
                ),
                "selection_schedule": str(
                    SELECTION_SCHEDULE_PATH
                ),
                "lookback_years": 2,
                "minimum_trailing_trades": 20,
                "weak_relationship_weight": (
                    0.50
                ),
                "round_trip_cost_bps": (
                    stats.ROUND_TRIP_COST_BPS
                ),
                "additional_entry_delay_days": (
                    stats.ADDITIONAL_ENTRY_DELAY_DAYS
                ),
                "placebo_replications": (
                    FINAL_PLACEBO_REPLICATIONS
                ),
                "minimum_shift_observations": (
                    stats.PLACEBO_MIN_SHIFT_OBSERVATIONS
                ),
                "random_seed": (
                    stats.RANDOM_SEED
                ),
            }
        ]
    )

    placebo_draws.to_csv(
        OUTPUT_ROOT
        / "final_timing_placebo_draws.csv",
        index=False,
    )

    placebo_offsets.to_csv(
        OUTPUT_ROOT
        / "final_timing_placebo_offsets.csv",
        index=False,
    )

    placebo_summary.to_csv(
        OUTPUT_ROOT
        / "final_timing_placebo_summary.csv",
        index=False,
    )

    run_configuration.to_csv(
        OUTPUT_ROOT
        / "final_placebo_configuration.csv",
        index=False,
    )

    print(
        f"\n{'=' * 78}\n"
        "Final timing placebo complete\n"
        f"{'=' * 78}"
    )

    display_columns = [
        "period",
        "sleeve",
        "placebo_replications",
        "actual_total_return_pct",
        "placebo_2_5_total_return_pct",
        "placebo_median_total_return_pct",
        "placebo_97_5_total_return_pct",
        "actual_percentile_total_return_pct",
        "return_placebo_exceedances",
        "empirical_p_value_total_return_pct",
        "monte_carlo_se_total_return_pct",
        "actual_sharpe_ratio",
        "placebo_2_5_sharpe_ratio",
        "placebo_median_sharpe_ratio",
        "placebo_97_5_sharpe_ratio",
        "actual_percentile_sharpe_ratio",
        "sharpe_placebo_exceedances",
        "empirical_p_value_sharpe_ratio",
        "monte_carlo_se_sharpe_ratio",
        "actual_profit_factor",
        "placebo_median_profit_factor",
        "actual_percentile_profit_factor",
        "profit_factor_placebo_exceedances",
        "empirical_p_value_profit_factor",
    ]

    print(
        placebo_summary[
            display_columns
        ].to_string(
            index=False
        )
    )

    print(
        "\nSaved outputs to:"
        f"\n{OUTPUT_ROOT}"
    )


if __name__ == "__main__":
    main()