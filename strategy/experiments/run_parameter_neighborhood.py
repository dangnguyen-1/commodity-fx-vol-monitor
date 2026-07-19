from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from strategy.backtest.run_backtest import (
    calculate_summary,
    load_fx_prices,
    run_event_backtest,
)
from strategy.experiments.run_period_comparison import (
    slice_period,
)
import strategy.experiments.run_rolling_relationship_selection as rolling


FEATURES_PATH = Path(
    "strategy/output/daily_features.csv"
)

OUTPUT_ROOT = Path(
    "strategy/output/experiments/"
    "parameter_neighborhood"
)


ROUND_TRIP_COST_BPS = 2.0

ADDITIONAL_ENTRY_DELAY_DAYS = 0

UNIVERSE_MODE = "rolling_soft_weight"

BASELINE_CONFIG = "baseline_3y_20_050"


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


SLEEVES = [
    "full",
    "brl_only",
    "ex_brl",
]


PARAMETER_CONFIGS: dict[
    str,
    dict[str, Any],
] = {
    "baseline_3y_20_050": {
        "changed_parameter": "baseline",
        "lookback_years": 3,
        "minimum_trailing_trades": 20,
        "weak_relationship_weight": 0.50,
    },
    "lookback_2y": {
        "changed_parameter": "lookback_years",
        "lookback_years": 2,
        "minimum_trailing_trades": 20,
        "weak_relationship_weight": 0.50,
    },
    "lookback_4y": {
        "changed_parameter": "lookback_years",
        "lookback_years": 4,
        "minimum_trailing_trades": 20,
        "weak_relationship_weight": 0.50,
    },
    "minimum_trades_15": {
        "changed_parameter": (
            "minimum_trailing_trades"
        ),
        "lookback_years": 3,
        "minimum_trailing_trades": 15,
        "weak_relationship_weight": 0.50,
    },
    "minimum_trades_25": {
        "changed_parameter": (
            "minimum_trailing_trades"
        ),
        "lookback_years": 3,
        "minimum_trailing_trades": 25,
        "weak_relationship_weight": 0.50,
    },
    "weak_weight_025": {
        "changed_parameter": (
            "weak_relationship_weight"
        ),
        "lookback_years": 3,
        "minimum_trailing_trades": 20,
        "weak_relationship_weight": 0.25,
    },
    "weak_weight_075": {
        "changed_parameter": (
            "weak_relationship_weight"
        ),
        "lookback_years": 3,
        "minimum_trailing_trades": 20,
        "weak_relationship_weight": 0.75,
    },
}


EXPECTED_BASELINE_RESULTS = {
    "validation": {
        "total_return_pct": -0.190552,
        "sharpe_ratio": -0.202697,
    },
    "research_holdout": {
        "total_return_pct": 0.261223,
        "sharpe_ratio": 0.430120,
    },
}


def build_parameterized_schedule(
    *,
    config: dict[str, Any],
    history_trades: pd.DataFrame,
    candidate_strategy: pd.DataFrame,
) -> pd.DataFrame:
    """
    Reuse the original schedule-building implementation
    while temporarily replacing only the three parameters
    being tested.

    The original global values are restored even if schedule
    construction raises an exception.
    """
    parameter_names = [
        "LOOKBACK_YEARS",
        "MIN_TRAILING_TRADES",
        "WEAK_RELATIONSHIP_WEIGHT",
    ]

    original_values = {
        name: getattr(rolling, name)
        for name in parameter_names
    }

    try:
        rolling.LOOKBACK_YEARS = int(
            config["lookback_years"]
        )

        rolling.MIN_TRAILING_TRADES = int(
            config[
                "minimum_trailing_trades"
            ]
        )

        rolling.WEAK_RELATIONSHIP_WEIGHT = float(
            config[
                "weak_relationship_weight"
            ]
        )

        schedule = (
            rolling.build_rolling_selection_schedule(
                history_trades,
                candidate_strategy,
            )
        )

    finally:
        for name, value in (
            original_values.items()
        ):
            setattr(
                rolling,
                name,
                value,
            )

    duplicate_count = schedule.duplicated(
        [
            "selection_year",
            "relationship_id",
        ]
    ).sum()

    if duplicate_count != 0:
        raise ValueError(
            "Parameterized schedule contains "
            f"{duplicate_count} duplicate "
            "year/relationship rows."
        )

    return schedule


def disable_entries(
    strategy: pd.DataFrame,
    mask: pd.Series,
) -> pd.DataFrame:
    """
    Disable new entries while retaining all daily signal
    rows required to manage positions that are already open.
    """
    result = strategy.copy()

    mask = mask.reindex(
        result.index,
        fill_value=False,
    )

    result.loc[
        mask,
        "trade_candidate",
    ] = 0

    result.loc[
        mask,
        "has_position",
    ] = 0

    available_size_columns = [
        column
        for column in rolling.SIZE_COLUMNS
        if column in result.columns
    ]

    if available_size_columns:
        result.loc[
            mask,
            available_size_columns,
        ] = 0.0

    return result


def build_sleeve_strategy(
    weighted_strategy: pd.DataFrame,
    *,
    sleeve: str,
) -> pd.DataFrame:
    if sleeve == "full":
        return weighted_strategy.copy()

    currency = (
        weighted_strategy["currency"]
        .astype(str)
    )

    if sleeve == "brl_only":
        return disable_entries(
            weighted_strategy,
            currency.ne("BRL"),
        )

    if sleeve == "ex_brl":
        return disable_entries(
            weighted_strategy,
            currency.eq("BRL"),
        )

    raise ValueError(
        f"Unknown sleeve: {sleeve}"
    )


def add_summary_metadata(
    summary: pd.DataFrame,
    *,
    config_name: str,
    config: dict[str, Any],
    period_name: str,
    sleeve: str,
) -> pd.DataFrame:
    metadata = pd.DataFrame(
        [
            {
                "config_name": config_name,
                "changed_parameter": (
                    config[
                        "changed_parameter"
                    ]
                ),
                "lookback_years": int(
                    config[
                        "lookback_years"
                    ]
                ),
                "minimum_trailing_trades": int(
                    config[
                        "minimum_trailing_trades"
                    ]
                ),
                "weak_relationship_weight": float(
                    config[
                        "weak_relationship_weight"
                    ]
                ),
                "period": period_name,
                "sleeve": sleeve,
                "universe_mode": UNIVERSE_MODE,
                "additional_entry_delay_days": (
                    ADDITIONAL_ENTRY_DELAY_DAYS
                ),
            }
        ]
    )

    return pd.concat(
        [
            metadata.reset_index(
                drop=True
            ),
            summary.reset_index(
                drop=True
            ),
        ],
        axis=1,
    )


def build_case_diagnostics(
    *,
    config_name: str,
    period_name: str,
    sleeve: str,
    strategy: pd.DataFrame,
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

    eligible_entry_mask = (
        strategy[
            "trade_candidate"
        ].eq(1)
        & strategy[
            "has_position"
        ].eq(1)
        & strategy[
            "position_size_pct"
        ].gt(0)
    )

    yearly_selected_counts = (
        strategy.loc[
            strategy["selected"].eq(1)
        ]
        .groupby(
            "selection_year"
        )[
            "relationship_id"
        ]
        .nunique()
    )

    return pd.DataFrame(
        [
            {
                "config_name": config_name,
                "period": period_name,
                "sleeve": sleeve,
                "strategy_rows": len(
                    strategy
                ),
                "relationships_available": int(
                    strategy[
                        "relationship_id"
                    ].nunique()
                ),
                "eligible_entry_rows": int(
                    eligible_entry_mask.sum()
                ),
                "approved_entries": (
                    approved_entries
                ),
                "completed_trades": len(
                    trades
                ),
                "average_selection_weight": float(
                    strategy[
                        "selection_weight"
                    ].mean()
                ),
                "minimum_yearly_selected": (
                    int(
                        yearly_selected_counts.min()
                    )
                    if not yearly_selected_counts.empty
                    else 0
                ),
                "maximum_yearly_selected": (
                    int(
                        yearly_selected_counts.max()
                    )
                    if not yearly_selected_counts.empty
                    else 0
                ),
                "transaction_cost_usd": (
                    float(
                        trades[
                            "transaction_cost_usd"
                        ].sum()
                    )
                    if not trades.empty
                    else 0.0
                ),
            }
        ]
    )


def save_case_outputs(
    *,
    config_name: str,
    period_name: str,
    sleeve: str,
    trades: pd.DataFrame,
    equity: pd.DataFrame,
    decisions: pd.DataFrame,
    summary: pd.DataFrame,
    diagnostics: pd.DataFrame,
) -> None:
    output_dir = (
        OUTPUT_ROOT
        / "cases"
        / config_name
        / period_name
        / sleeve
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    trades.to_csv(
        output_dir / "trades.csv",
        index=False,
    )

    equity.to_csv(
        output_dir / "equity.csv",
        index=False,
    )

    decisions.to_csv(
        output_dir / "decisions.csv",
        index=False,
    )

    summary.to_csv(
        output_dir / "summary.csv",
        index=False,
    )

    diagnostics.to_csv(
        output_dir / "diagnostics.csv",
        index=False,
    )


def run_case(
    *,
    config_name: str,
    config: dict[str, Any],
    period_name: str,
    sleeve: str,
    strategy: pd.DataFrame,
    fx_prices: pd.DataFrame,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
]:
    trades, equity, decisions = (
        run_event_backtest(
            strategy,
            fx_prices,
            round_trip_cost_bps=(
                ROUND_TRIP_COST_BPS
            ),
            additional_entry_delay_days=(
                ADDITIONAL_ENTRY_DELAY_DAYS
            ),
        )
    )

    summary = calculate_summary(
        trades,
        equity,
        decisions,
        round_trip_cost_bps=(
            ROUND_TRIP_COST_BPS
        ),
    )

    summary = add_summary_metadata(
        summary,
        config_name=config_name,
        config=config,
        period_name=period_name,
        sleeve=sleeve,
    )

    diagnostics = build_case_diagnostics(
        config_name=config_name,
        period_name=period_name,
        sleeve=sleeve,
        strategy=strategy,
        trades=trades,
        decisions=decisions,
    )

    save_case_outputs(
        config_name=config_name,
        period_name=period_name,
        sleeve=sleeve,
        trades=trades,
        equity=equity,
        decisions=decisions,
        summary=summary,
        diagnostics=diagnostics,
    )

    row = summary.iloc[0]

    print(
        f"{config_name:22s} | "
        f"{period_name:16s} | "
        f"{sleeve:8s} | "
        f"return "
        f"{row['total_return_pct']: .6f}% | "
        f"Sharpe "
        f"{row['sharpe_ratio']: .4f} | "
        f"PF "
        f"{row['profit_factor']: .4f} | "
        f"trades "
        f"{int(row['total_trades'])}"
    )

    return summary, diagnostics


def build_parameter_deltas(
    summaries: pd.DataFrame,
) -> pd.DataFrame:
    metric_columns = [
        "total_pnl_usd",
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
            "config_name"
        ].eq(BASELINE_CONFIG),
        [
            "period",
            "sleeve",
            *metric_columns,
        ],
    ].copy()

    alternatives = summaries.loc[
        ~summaries[
            "config_name"
        ].eq(BASELINE_CONFIG)
    ].copy()

    comparison = alternatives.merge(
        baseline,
        on=[
            "period",
            "sleeve",
        ],
        how="left",
        validate="many_to_one",
        suffixes=(
            "_alternative",
            "_baseline",
        ),
    )

    for metric in metric_columns:
        comparison[
            f"delta_{metric}"
        ] = (
            comparison[
                f"{metric}_alternative"
            ]
            - comparison[
                f"{metric}_baseline"
            ]
        )

    comparison[
        "improves_return"
    ] = (
        comparison[
            "delta_total_return_pct"
        ] > 0
    ).astype(int)

    comparison[
        "improves_sharpe"
    ] = (
        comparison[
            "delta_sharpe_ratio"
        ] > 0
    ).astype(int)

    comparison[
        "improves_drawdown"
    ] = (
        comparison[
            "delta_max_drawdown_pct"
        ] > 0
    ).astype(int)

    comparison[
        "improves_profit_factor"
    ] = (
        comparison[
            "delta_profit_factor"
        ] > 0
    ).astype(int)

    return comparison


def build_validation_decision_table(
    summaries: pd.DataFrame,
) -> pd.DataFrame:
    validation = summaries.loc[
        summaries["period"].eq(
            "validation"
        )
        & summaries["sleeve"].eq(
            "full"
        )
    ].copy()

    holdout = summaries.loc[
        summaries["period"].eq(
            "research_holdout"
        )
        & summaries["sleeve"].eq(
            "full"
        ),
        [
            "config_name",
            "total_return_pct",
            "sharpe_ratio",
            "max_drawdown_pct",
            "profit_factor",
        ],
    ].copy()

    holdout = holdout.rename(
        columns={
            "total_return_pct": (
                "diagnostic_holdout_return_pct"
            ),
            "sharpe_ratio": (
                "diagnostic_holdout_sharpe"
            ),
            "max_drawdown_pct": (
                "diagnostic_holdout_drawdown_pct"
            ),
            "profit_factor": (
                "diagnostic_holdout_profit_factor"
            ),
        }
    )

    baseline_row = validation.loc[
        validation[
            "config_name"
        ].eq(BASELINE_CONFIG)
    ].iloc[0]

    validation[
        "delta_validation_return_pct"
    ] = (
        validation[
            "total_return_pct"
        ]
        - float(
            baseline_row[
                "total_return_pct"
            ]
        )
    )

    validation[
        "delta_validation_sharpe"
    ] = (
        validation[
            "sharpe_ratio"
        ]
        - float(
            baseline_row[
                "sharpe_ratio"
            ]
        )
    )

    validation[
        "delta_validation_drawdown_pct"
    ] = (
        validation[
            "max_drawdown_pct"
        ]
        - float(
            baseline_row[
                "max_drawdown_pct"
            ]
        )
    )

    validation[
        "delta_validation_profit_factor"
    ] = (
        validation[
            "profit_factor"
        ]
        - float(
            baseline_row[
                "profit_factor"
            ]
        )
    )

    validation[
        "improves_validation_return"
    ] = (
        validation[
            "delta_validation_return_pct"
        ] > 0
    ).astype(int)

    validation[
        "improves_validation_sharpe"
    ] = (
        validation[
            "delta_validation_sharpe"
        ] > 0
    ).astype(int)

    validation[
        "improves_validation_drawdown"
    ] = (
        validation[
            "delta_validation_drawdown_pct"
        ] > 0
    ).astype(int)

    validation[
        "improves_validation_profit_factor"
    ] = (
        validation[
            "delta_validation_profit_factor"
        ] > 0
    ).astype(int)

    improvement_columns = [
        "improves_validation_return",
        "improves_validation_sharpe",
        "improves_validation_drawdown",
        "improves_validation_profit_factor",
    ]

    validation[
        "validation_improvement_count"
    ] = validation[
        improvement_columns
    ].sum(axis=1)

    validation[
        "strict_validation_improvement"
    ] = (
        validation[
            improvement_columns
        ].all(axis=1)
    ).astype(int)

    decision_table = validation.merge(
        holdout,
        on="config_name",
        how="left",
        validate="one_to_one",
    )

    return decision_table.sort_values(
        [
            "strict_validation_improvement",
            "validation_improvement_count",
            "sharpe_ratio",
            "total_return_pct",
        ],
        ascending=[
            False,
            False,
            False,
            False,
        ],
    ).reset_index(
        drop=True
    )


def build_schedule_overlap(
    schedules: dict[
        str,
        pd.DataFrame,
    ],
) -> pd.DataFrame:
    baseline = schedules[
        BASELINE_CONFIG
    ][
        [
            "selection_year",
            "relationship_id",
            "selected",
            "rolling_soft_weight_weight",
        ]
    ].rename(
        columns={
            "selected": (
                "baseline_selected"
            ),
            "rolling_soft_weight_weight": (
                "baseline_weight"
            ),
        }
    )

    records = []

    for config_name, schedule in (
        schedules.items()
    ):
        current = schedule[
            [
                "selection_year",
                "relationship_id",
                "selected",
                "rolling_soft_weight_weight",
            ]
        ].rename(
            columns={
                "selected": (
                    "config_selected"
                ),
                "rolling_soft_weight_weight": (
                    "config_weight"
                ),
            }
        )

        comparison = baseline.merge(
            current,
            on=[
                "selection_year",
                "relationship_id",
            ],
            how="inner",
            validate="one_to_one",
        )

        for selection_year, group in (
            comparison.groupby(
                "selection_year",
                sort=True,
            )
        ):
            baseline_selected = (
                group[
                    "baseline_selected"
                ].eq(1)
            )

            config_selected = (
                group[
                    "config_selected"
                ].eq(1)
            )

            intersection = int(
                (
                    baseline_selected
                    & config_selected
                ).sum()
            )

            union = int(
                (
                    baseline_selected
                    | config_selected
                ).sum()
            )

            jaccard = (
                intersection / union
                if union > 0
                else 1.0
            )

            records.append(
                {
                    "config_name": (
                        config_name
                    ),
                    "selection_year": int(
                        selection_year
                    ),
                    "baseline_selected_relationships": int(
                        baseline_selected.sum()
                    ),
                    "config_selected_relationships": int(
                        config_selected.sum()
                    ),
                    "selected_intersection": (
                        intersection
                    ),
                    "selected_union": union,
                    "selection_jaccard": (
                        jaccard
                    ),
                    "mean_absolute_weight_difference": float(
                        (
                            group[
                                "config_weight"
                            ]
                            - group[
                                "baseline_weight"
                            ]
                        )
                        .abs()
                        .mean()
                    ),
                    "identical_selected_set": int(
                        jaccard == 1.0
                    ),
                }
            )

    return pd.DataFrame(records)


def build_schedule_overlap_summary(
    overlap: pd.DataFrame,
) -> pd.DataFrame:
    return (
        overlap.groupby(
            "config_name",
            as_index=False,
        )
        .agg(
            years=(
                "selection_year",
                "nunique",
            ),
            mean_selection_jaccard=(
                "selection_jaccard",
                "mean",
            ),
            minimum_selection_jaccard=(
                "selection_jaccard",
                "min",
            ),
            maximum_selection_jaccard=(
                "selection_jaccard",
                "max",
            ),
            identical_years=(
                "identical_selected_set",
                "sum",
            ),
            mean_absolute_weight_difference=(
                "mean_absolute_weight_difference",
                "mean",
            ),
        )
        .sort_values(
            "mean_selection_jaccard",
            ascending=False,
        )
        .reset_index(
            drop=True
        )
    )


def validate_baseline_reproduction(
    summaries: pd.DataFrame,
) -> None:
    baseline = summaries.loc[
        summaries[
            "config_name"
        ].eq(BASELINE_CONFIG)
        & summaries[
            "sleeve"
        ].eq("full")
    ]

    for period_name, expected in (
        EXPECTED_BASELINE_RESULTS.items()
    ):
        row = baseline.loc[
            baseline[
                "period"
            ].eq(period_name)
        ]

        if len(row) != 1:
            raise ValueError(
                "Expected exactly one baseline "
                f"row for {period_name}; "
                f"found {len(row)}."
            )

        actual_return = float(
            row[
                "total_return_pct"
            ].iloc[0]
        )

        actual_sharpe = float(
            row[
                "sharpe_ratio"
            ].iloc[0]
        )

        return_difference = abs(
            actual_return
            - expected[
                "total_return_pct"
            ]
        )

        sharpe_difference = abs(
            actual_sharpe
            - expected[
                "sharpe_ratio"
            ]
        )

        if (
            return_difference > 1e-5
            or sharpe_difference > 1e-5
        ):
            raise ValueError(
                "Baseline reproduction failed "
                f"for {period_name}.\n"
                f"Expected return: "
                f"{expected['total_return_pct']}\n"
                f"Actual return: "
                f"{actual_return}\n"
                f"Expected Sharpe: "
                f"{expected['sharpe_ratio']}\n"
                f"Actual Sharpe: "
                f"{actual_sharpe}"
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

    candidate_strategy = (
        rolling.build_candidate_strategy(
            features
        )
    )

    fx_prices = load_fx_prices()

    OUTPUT_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    print(
        f"\n{'=' * 78}\n"
        "Building shared causal "
        "selection history\n"
        f"{'=' * 78}"
    )

    (
        history_trades,
        history_equity,
        history_decisions,
    ) = rolling.run_selection_history(
        candidate_strategy,
        fx_prices,
    )

    history_output_dir = (
        OUTPUT_ROOT
        / "selection_history"
    )

    history_output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    history_trades.to_csv(
        history_output_dir
        / "trades_2bps.csv",
        index=False,
    )

    history_equity.to_csv(
        history_output_dir
        / "equity_2bps.csv",
        index=False,
    )

    history_decisions.to_csv(
        history_output_dir
        / "decisions_2bps.csv",
        index=False,
    )

    schedules: dict[
        str,
        pd.DataFrame,
    ] = {}

    selection_count_frames = []

    for config_name, config in (
        PARAMETER_CONFIGS.items()
    ):
        print(
            f"\nBuilding schedule: "
            f"{config_name}"
        )

        schedule = (
            build_parameterized_schedule(
                config=config,
                history_trades=(
                    history_trades
                ),
                candidate_strategy=(
                    candidate_strategy
                ),
            )
        )

        schedules[config_name] = (
            schedule
        )

        config_output_dir = (
            OUTPUT_ROOT
            / "schedules"
            / config_name
        )

        config_output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        schedule.to_csv(
            config_output_dir
            / "rolling_selection_schedule.csv",
            index=False,
        )

        selection_counts = (
            rolling.build_selection_counts(
                schedule
            )
        )

        selection_counts.insert(
            0,
            "config_name",
            config_name,
        )

        selection_counts.insert(
            1,
            "lookback_years",
            int(
                config[
                    "lookback_years"
                ]
            ),
        )

        selection_counts.insert(
            2,
            "minimum_trailing_trades",
            int(
                config[
                    "minimum_trailing_trades"
                ]
            ),
        )

        selection_counts.insert(
            3,
            "weak_relationship_weight",
            float(
                config[
                    "weak_relationship_weight"
                ]
            ),
        )

        selection_counts.to_csv(
            config_output_dir
            / "selection_counts.csv",
            index=False,
        )

        selection_count_frames.append(
            selection_counts
        )

    summary_frames = []
    diagnostic_frames = []

    for config_name, config in (
        PARAMETER_CONFIGS.items()
    ):
        schedule = schedules[
            config_name
        ]

        print(
            f"\n{'=' * 78}\n"
            f"Parameter configuration: "
            f"{config_name}\n"
            f"Lookback years: "
            f"{config['lookback_years']}\n"
            f"Minimum trailing trades: "
            f"{config['minimum_trailing_trades']}\n"
            f"Weak relationship weight: "
            f"{config['weak_relationship_weight']}\n"
            f"{'=' * 78}"
        )

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
                rolling.apply_rolling_selection(
                    period_strategy,
                    universe_mode=(
                        UNIVERSE_MODE
                    ),
                    selection_schedule=(
                        schedule
                    ),
                )
            )

            for sleeve in SLEEVES:
                sleeve_strategy = (
                    build_sleeve_strategy(
                        weighted_strategy,
                        sleeve=sleeve,
                    )
                )

                (
                    summary,
                    diagnostics,
                ) = run_case(
                    config_name=(
                        config_name
                    ),
                    config=config,
                    period_name=(
                        period_name
                    ),
                    sleeve=sleeve,
                    strategy=(
                        sleeve_strategy
                    ),
                    fx_prices=(
                        period_fx_prices
                    ),
                )

                summary_frames.append(
                    summary
                )

                diagnostic_frames.append(
                    diagnostics
                )

    summaries = pd.concat(
        summary_frames,
        ignore_index=True,
    )

    diagnostics = pd.concat(
        diagnostic_frames,
        ignore_index=True,
    )

    selection_counts = pd.concat(
        selection_count_frames,
        ignore_index=True,
    )

    validate_baseline_reproduction(
        summaries
    )

    parameter_deltas = (
        build_parameter_deltas(
            summaries
        )
    )

    validation_decision_table = (
        build_validation_decision_table(
            summaries
        )
    )

    schedule_overlap = (
        build_schedule_overlap(
            schedules
        )
    )

    schedule_overlap_summary = (
        build_schedule_overlap_summary(
            schedule_overlap
        )
    )

    full_strategy_summary = (
        summaries.loc[
            summaries[
                "sleeve"
            ].eq("full")
        ]
        .sort_values(
            [
                "period",
                "config_name",
            ]
        )
        .reset_index(
            drop=True
        )
    )

    summaries.to_csv(
        OUTPUT_ROOT
        / "parameter_neighborhood_summary.csv",
        index=False,
    )

    diagnostics.to_csv(
        OUTPUT_ROOT
        / "parameter_neighborhood_diagnostics.csv",
        index=False,
    )

    selection_counts.to_csv(
        OUTPUT_ROOT
        / "parameter_selection_counts.csv",
        index=False,
    )

    parameter_deltas.to_csv(
        OUTPUT_ROOT
        / "parameter_neighborhood_deltas.csv",
        index=False,
    )

    validation_decision_table.to_csv(
        OUTPUT_ROOT
        / "validation_decision_table.csv",
        index=False,
    )

    full_strategy_summary.to_csv(
        OUTPUT_ROOT
        / "full_strategy_neighborhood.csv",
        index=False,
    )

    schedule_overlap.to_csv(
        OUTPUT_ROOT
        / "schedule_overlap_by_year.csv",
        index=False,
    )

    schedule_overlap_summary.to_csv(
        OUTPUT_ROOT
        / "schedule_overlap_summary.csv",
        index=False,
    )

    print(
        f"\n{'=' * 78}\n"
        "Parameter-neighborhood "
        "analysis complete\n"
        f"{'=' * 78}"
    )

    print(
        "\nFull-strategy results:"
    )

    full_display_columns = [
        "period",
        "config_name",
        "lookback_years",
        "minimum_trailing_trades",
        "weak_relationship_weight",
        "total_return_pct",
        "sharpe_ratio",
        "max_drawdown_pct",
        "profit_factor",
        "total_trades",
        "total_transaction_cost_usd",
    ]

    print(
        full_strategy_summary[
            full_display_columns
        ].to_string(
            index=False
        )
    )

    print(
        "\nValidation-only decision table:"
    )

    decision_display_columns = [
        "config_name",
        "changed_parameter",
        "lookback_years",
        "minimum_trailing_trades",
        "weak_relationship_weight",
        "total_return_pct",
        "sharpe_ratio",
        "max_drawdown_pct",
        "profit_factor",
        "delta_validation_return_pct",
        "delta_validation_sharpe",
        "delta_validation_drawdown_pct",
        "delta_validation_profit_factor",
        "validation_improvement_count",
        "strict_validation_improvement",
        "diagnostic_holdout_return_pct",
        "diagnostic_holdout_sharpe",
    ]

    print(
        validation_decision_table[
            decision_display_columns
        ].to_string(
            index=False
        )
    )

    print(
        "\nSchedule-overlap summary:"
    )

    print(
        schedule_overlap_summary.to_string(
            index=False
        )
    )

    print(
        "\nSleeve results:"
    )

    sleeve_display_columns = [
        "period",
        "config_name",
        "sleeve",
        "total_return_pct",
        "sharpe_ratio",
        "profit_factor",
        "total_trades",
    ]

    print(
        summaries[
            sleeve_display_columns
        ]
        .sort_values(
            [
                "period",
                "config_name",
                "sleeve",
            ]
        )
        .to_string(
            index=False
        )
    )

    print(
        "\nBaseline reproduction: passed"
    )

    print(
        "\nSaved outputs to:"
        f"\n{OUTPUT_ROOT}"
    )


if __name__ == "__main__":
    main()