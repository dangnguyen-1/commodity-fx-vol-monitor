from pathlib import Path

import pandas as pd

from strategy.backtest.run_backtest import (
    build_group_reports,
    calculate_summary,
    load_fx_prices,
    run_event_backtest,
)
from strategy.experiments.run_final_layer_ablation import (
    VARIANTS,
    build_candidate_strategy,
)
from strategy.experiments.run_layer_comparison import (
    save_variant_outputs,
)
from strategy.experiments.run_period_comparison import (
    slice_period,
)
from strategy.experiments.run_rolling_relationship_selection import (
    apply_rolling_selection,
)


FEATURES_PATH = Path(
    "strategy/output/daily_features.csv"
)

SELECTION_SCHEDULE_PATH = Path(
    "strategy/config/v1/"
    "rolling_selection_schedule.csv"
)

OUTPUT_ROOT = Path(
    "strategy/output/experiments/"
    "execution_stress"
)


VARIANT_NAME = "market_fundamentals_news"

UNIVERSE_MODE = "rolling_soft_weight"


EVALUATION_PERIODS = {
    "validation": {
        "start": "2019-01-01",
        "end": "2022-12-31",
    },
    "research_holdout": {
        "start": "2023-01-01",
        "end": None,
    },
}


ADDITIONAL_ENTRY_DELAYS = [
    0,
    1,
    2,
]


ROUND_TRIP_COSTS_BPS = [
    2.0,
    5.0,
    10.0,
]


def load_selection_schedule() -> pd.DataFrame:
    if not SELECTION_SCHEDULE_PATH.exists():
        raise FileNotFoundError(
            "Missing frozen rolling-selection "
            f"schedule: {SELECTION_SCHEDULE_PATH}"
        )

    schedule = pd.read_csv(
        SELECTION_SCHEDULE_PATH
    )

    required_columns = [
        "selection_year",
        "relationship_id",
        "selected",
        "rolling_soft_weight_weight",
    ]

    missing_columns = sorted(
        set(required_columns)
        - set(schedule.columns)
    )

    if missing_columns:
        raise ValueError(
            "Selection schedule is missing "
            f"columns: {missing_columns}"
        )

    schedule["selection_year"] = (
        pd.to_numeric(
            schedule["selection_year"],
            errors="raise",
        )
        .astype(int)
    )

    duplicate_count = schedule.duplicated(
        [
            "selection_year",
            "relationship_id",
        ]
    ).sum()

    if duplicate_count != 0:
        raise ValueError(
            "Selection schedule contains "
            f"{duplicate_count} duplicate "
            "year/relationship rows."
        )

    return schedule


def add_summary_metadata(
    summary: pd.DataFrame,
    *,
    period_name: str,
    additional_entry_delay_days: int,
    effective_start: pd.Timestamp,
    effective_end: pd.Timestamp,
) -> pd.DataFrame:
    metadata = pd.DataFrame(
        [
            {
                "period": period_name,
                "variant": VARIANT_NAME,
                "universe_mode": UNIVERSE_MODE,
                "effective_period_start": (
                    effective_start.date()
                ),
                "effective_period_end": (
                    effective_end.date()
                ),
                "additional_entry_delay_days": (
                    additional_entry_delay_days
                ),
                "entry_timing": (
                    "next_fx_open_plus_delay"
                ),
                "selection_schedule_source": str(
                    SELECTION_SCHEDULE_PATH
                ),
            }
        ]
    )

    return pd.concat(
        [
            metadata.reset_index(drop=True),
            summary.reset_index(drop=True),
        ],
        axis=1,
    )


def build_diagnostics(
    *,
    period_name: str,
    additional_entry_delay_days: int,
    round_trip_cost_bps: float,
    weighted_strategy: pd.DataFrame,
    trades: pd.DataFrame,
    decisions: pd.DataFrame,
) -> pd.DataFrame:
    approved_entries = (
        int(
            decisions[
                "entry_decision"
            ].eq("approved").sum()
        )
        if not decisions.empty
        else 0
    )

    missing_delayed_entries = (
        int(
            decisions[
                "entry_rejection_reason"
            ]
            .eq(
                "missing_delayed_entry_open"
            )
            .sum()
        )
        if not decisions.empty
        else 0
    )

    invalidated_delayed_entries = (
        int(
            decisions[
                "entry_rejection_reason"
            ]
            .eq(
                "delayed_entry_signal_invalidated"
            )
            .sum()
        )
        if not decisions.empty
        else 0
    )

    missing_revalidation_signals = (
        int(
            decisions[
                "entry_rejection_reason"
            ]
            .eq(
                "missing_entry_revalidation_signal"
            )
            .sum()
        )
        if not decisions.empty
        else 0
    )

    return pd.DataFrame(
        [
            {
                "period": period_name,
                "variant": VARIANT_NAME,
                "universe_mode": UNIVERSE_MODE,
                "additional_entry_delay_days": (
                    additional_entry_delay_days
                ),
                "round_trip_cost_bps": (
                    round_trip_cost_bps
                ),
                "strategy_rows": len(
                    weighted_strategy
                ),
                "relationships": int(
                    weighted_strategy[
                        "relationship_id"
                    ].nunique()
                ),
                "trade_candidate_rows": int(
                    weighted_strategy[
                        "trade_candidate"
                    ].sum()
                ),
                "sized_position_rows": int(
                    weighted_strategy[
                        "has_position"
                    ].sum()
                ),
                "approved_entries": (
                    approved_entries
                ),
                "completed_trades": len(trades),
                "missing_delayed_entry_open": (
                    missing_delayed_entries
                ),
                "invalidated_delayed_entries": (
                    invalidated_delayed_entries
                ),
                "missing_revalidation_signals": (
                    missing_revalidation_signals
                ),
            }
        ]
    )


def run_case(
    *,
    period_name: str,
    additional_entry_delay_days: int,
    round_trip_cost_bps: float,
    weighted_strategy: pd.DataFrame,
    period_fx_prices: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    effective_start = max(
        weighted_strategy["date"].min(),
        period_fx_prices["date"].min(),
    )

    effective_end = min(
        weighted_strategy["date"].max(),
        period_fx_prices["date"].max(),
    )

    print(
        f"\n{'=' * 78}\n"
        f"Period: {period_name}\n"
        f"Additional entry delay: "
        f"{additional_entry_delay_days} "
        "trading day(s)\n"
        f"Round-trip cost: "
        f"{round_trip_cost_bps:.1f} bps\n"
        f"Dates: {effective_start.date()} "
        f"through {effective_end.date()}\n"
        f"{'=' * 78}"
    )

    trades, equity, decisions = (
        run_event_backtest(
            weighted_strategy,
            period_fx_prices,
            round_trip_cost_bps=(
                round_trip_cost_bps
            ),
            additional_entry_delay_days=(
                additional_entry_delay_days
            ),
        )
    )

    summary = calculate_summary(
        trades,
        equity,
        decisions,
        round_trip_cost_bps=(
            round_trip_cost_bps
        ),
    )

    summary = add_summary_metadata(
        summary,
        period_name=period_name,
        additional_entry_delay_days=(
            additional_entry_delay_days
        ),
        effective_start=effective_start,
        effective_end=effective_end,
    )

    diagnostics = build_diagnostics(
        period_name=period_name,
        additional_entry_delay_days=(
            additional_entry_delay_days
        ),
        round_trip_cost_bps=(
            round_trip_cost_bps
        ),
        weighted_strategy=weighted_strategy,
        trades=trades,
        decisions=decisions,
    )

    reports = build_group_reports(
        trades
    )

    output_dir = (
        OUTPUT_ROOT
        / period_name
        / (
            f"delay_"
            f"{additional_entry_delay_days}_days"
        )
        / f"{round_trip_cost_bps:g}_bps"
    )

    save_variant_outputs(
        output_dir=output_dir,
        trades=trades,
        equity=equity,
        decisions=decisions,
        summary=summary,
        diagnostics=diagnostics,
        reports=reports,
    )

    print(
        summary[
            [
                "period",
                "additional_entry_delay_days",
                "round_trip_cost_bps",
                "total_return_pct",
                "annualized_return_pct",
                "annualized_volatility_pct",
                "sharpe_ratio",
                "max_drawdown_pct",
                "profit_factor",
                "total_trades",
                "total_transaction_cost_usd",
            ]
        ].to_string(index=False)
    )

    return summary, diagnostics


def build_delay_deltas(
    summaries: pd.DataFrame,
) -> pd.DataFrame:
    key_columns = [
        "period",
        "round_trip_cost_bps",
    ]

    metric_columns = [
        "total_return_pct",
        "annualized_return_pct",
        "annualized_volatility_pct",
        "sharpe_ratio",
        "max_drawdown_pct",
        "profit_factor",
        "total_trades",
        "total_transaction_cost_usd",
    ]

    baseline = summaries.loc[
        summaries[
            "additional_entry_delay_days"
        ].eq(0),
        key_columns + metric_columns,
    ].copy()

    comparisons = []

    for delay in [1, 2]:
        delayed = summaries.loc[
            summaries[
                "additional_entry_delay_days"
            ].eq(delay),
            key_columns + metric_columns,
        ].copy()

        comparison = baseline.merge(
            delayed,
            on=key_columns,
            how="inner",
            validate="one_to_one",
            suffixes=(
                "_delay_0",
                f"_delay_{delay}",
            ),
        )

        comparison.insert(
            2,
            "additional_entry_delay_days",
            delay,
        )

        for metric in metric_columns:
            comparison[
                f"delta_{metric}"
            ] = (
                comparison[
                    f"{metric}_delay_{delay}"
                ]
                - comparison[
                    f"{metric}_delay_0"
                ]
            )

        comparison[
            "delay_improves_return"
        ] = (
            comparison[
                "delta_total_return_pct"
            ] > 0
        ).astype(int)

        comparison[
            "delay_improves_sharpe"
        ] = (
            comparison[
                "delta_sharpe_ratio"
            ] > 0
        ).astype(int)

        comparison[
            "delay_improves_drawdown"
        ] = (
            comparison[
                "delta_max_drawdown_pct"
            ] > 0
        ).astype(int)

        comparisons.append(comparison)

    return pd.concat(
        comparisons,
        ignore_index=True,
    )


def main() -> None:
    if not FEATURES_PATH.exists():
        raise FileNotFoundError(
            f"Missing feature file: "
            f"{FEATURES_PATH}"
        )

    features = pd.read_csv(
        FEATURES_PATH
    )

    features["date"] = pd.to_datetime(
        features["date"]
    )

    selection_schedule = (
        load_selection_schedule()
    )

    config = VARIANTS[VARIANT_NAME]

    candidate_strategy = (
        build_candidate_strategy(
            features,
            variant_name=VARIANT_NAME,
            config=config,
        )
    )

    fx_prices = load_fx_prices()

    OUTPUT_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    summaries: list[pd.DataFrame] = []
    diagnostics: list[pd.DataFrame] = []

    for (
        period_name,
        period_config,
    ) in EVALUATION_PERIODS.items():
        period_strategy = slice_period(
            candidate_strategy,
            start_date=(
                period_config["start"]
            ),
            end_date=(
                period_config["end"]
            ),
        )

        period_fx_prices = slice_period(
            fx_prices,
            start_date=(
                period_config["start"]
            ),
            end_date=(
                period_config["end"]
            ),
        )

        weighted_strategy = (
            apply_rolling_selection(
                period_strategy,
                universe_mode=(
                    UNIVERSE_MODE
                ),
                selection_schedule=(
                    selection_schedule
                ),
            )
        )

        for delay in (
            ADDITIONAL_ENTRY_DELAYS
        ):
            for cost_bps in (
                ROUND_TRIP_COSTS_BPS
            ):
                summary, diagnostic = (
                    run_case(
                        period_name=(
                            period_name
                        ),
                        additional_entry_delay_days=(
                            delay
                        ),
                        round_trip_cost_bps=(
                            cost_bps
                        ),
                        weighted_strategy=(
                            weighted_strategy
                        ),
                        period_fx_prices=(
                            period_fx_prices
                        ),
                    )
                )

                summaries.append(summary)
                diagnostics.append(
                    diagnostic
                )

    comparison_summary = pd.concat(
        summaries,
        ignore_index=True,
    )

    comparison_diagnostics = pd.concat(
        diagnostics,
        ignore_index=True,
    )

    delay_deltas = build_delay_deltas(
        comparison_summary
    )

    comparison_summary.to_csv(
        OUTPUT_ROOT
        / "execution_stress_summary.csv",
        index=False,
    )

    comparison_diagnostics.to_csv(
        OUTPUT_ROOT
        / "execution_stress_diagnostics.csv",
        index=False,
    )

    delay_deltas.to_csv(
        OUTPUT_ROOT
        / "execution_delay_deltas.csv",
        index=False,
    )

    print(
        f"\n{'=' * 78}\n"
        "Execution stress complete\n"
        f"{'=' * 78}"
    )

    display_columns = [
        "period",
        "additional_entry_delay_days",
        "round_trip_cost_bps",
        "total_return_pct",
        "annualized_return_pct",
        "sharpe_ratio",
        "max_drawdown_pct",
        "profit_factor",
        "total_trades",
        "total_transaction_cost_usd",
    ]

    print(
        comparison_summary[
            display_columns
        ]
        .sort_values(
            [
                "period",
                "additional_entry_delay_days",
                "round_trip_cost_bps",
            ]
        )
        .to_string(index=False)
    )

    print(
        "\nEntry-delay deltas "
        "(delayed minus zero-delay):"
    )

    delta_columns = [
        "period",
        "round_trip_cost_bps",
        "additional_entry_delay_days",
        "delta_total_return_pct",
        "delta_sharpe_ratio",
        "delta_max_drawdown_pct",
        "delta_profit_factor",
        "delta_total_trades",
        "delta_total_transaction_cost_usd",
        "delay_improves_return",
        "delay_improves_sharpe",
        "delay_improves_drawdown",
    ]

    print(
        delay_deltas[
            delta_columns
        ]
        .sort_values(
            [
                "period",
                "additional_entry_delay_days",
                "round_trip_cost_bps",
            ]
        )
        .to_string(index=False)
    )

    print(
        "\nSaved outputs to:"
        f"\n{OUTPUT_ROOT}"
    )


if __name__ == "__main__":
    main()