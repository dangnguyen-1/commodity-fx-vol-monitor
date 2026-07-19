from pathlib import Path

import pandas as pd

from strategy.backtest.run_backtest import (
    calculate_summary,
    load_fx_prices,
    run_event_backtest,
)
from strategy.experiments.run_final_layer_ablation import (
    VARIANTS,
    build_candidate_strategy,
)
from strategy.experiments.run_period_comparison import (
    slice_period,
)
from strategy.experiments.run_rolling_relationship_selection import (
    SIZE_COLUMNS,
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
    "concentration_stress"
)


VARIANT_NAME = "market_fundamentals_news"

UNIVERSE_MODE = "rolling_soft_weight"

ROUND_TRIP_COST_BPS = 2.0

ADDITIONAL_ENTRY_DELAY_DAYS = 0


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


def load_selection_schedule() -> pd.DataFrame:
    if not SELECTION_SCHEDULE_PATH.exists():
        raise FileNotFoundError(
            "Missing rolling-selection schedule: "
            f"{SELECTION_SCHEDULE_PATH}"
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


def disable_entries(
    strategy: pd.DataFrame,
    mask: pd.Series,
) -> pd.DataFrame:
    """
    Disable new entries while retaining the daily signal
    rows used to manage positions that are already open.
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

    result.loc[
        mask,
        SIZE_COLUMNS,
    ] = 0.0

    return result


def build_case_strategy(
    weighted_strategy: pd.DataFrame,
    *,
    stress_type: str,
    excluded_value: str,
) -> pd.DataFrame:
    if stress_type == "baseline":
        return weighted_strategy.copy()

    if stress_type == "currency":
        mask = weighted_strategy[
            "currency"
        ].astype(str).eq(
            excluded_value
        )

    elif stress_type == "relationship":
        mask = weighted_strategy[
            "relationship_id"
        ].astype(str).eq(
            excluded_value
        )

    elif stress_type == "calendar_year":
        mask = (
            weighted_strategy[
                "date"
            ].dt.year.astype(str).eq(
                excluded_value
            )
        )

    else:
        raise ValueError(
            f"Unknown stress type: {stress_type}"
        )

    disabled_rows = int(mask.sum())

    if disabled_rows == 0:
        raise ValueError(
            f"{stress_type}/{excluded_value}: "
            "the exclusion matched no strategy rows."
        )

    return disable_entries(
        weighted_strategy,
        mask,
    )


def run_case(
    *,
    period_name: str,
    stress_type: str,
    excluded_value: str,
    weighted_strategy: pd.DataFrame,
    period_fx_prices: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    case_strategy = build_case_strategy(
        weighted_strategy,
        stress_type=stress_type,
        excluded_value=excluded_value,
    )

    trades, equity, decisions = (
        run_event_backtest(
            case_strategy,
            period_fx_prices,
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

    metadata = pd.DataFrame(
        [
            {
                "period": period_name,
                "variant": VARIANT_NAME,
                "universe_mode": UNIVERSE_MODE,
                "stress_type": stress_type,
                "excluded_value": (
                    excluded_value
                ),
                "additional_entry_delay_days": (
                    ADDITIONAL_ENTRY_DELAY_DAYS
                ),
            }
        ]
    )

    summary = pd.concat(
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

    approved_entries = (
        int(
            decisions[
                "entry_decision"
            ].eq("approved").sum()
        )
        if not decisions.empty
        else 0
    )

    diagnostic = pd.DataFrame(
        [
            {
                "period": period_name,
                "stress_type": stress_type,
                "excluded_value": (
                    excluded_value
                ),
                "strategy_rows": len(
                    case_strategy
                ),
                "available_relationships": int(
                    case_strategy[
                        "relationship_id"
                    ].nunique()
                ),
                "eligible_entry_rows": int(
                    (
                        case_strategy[
                            "trade_candidate"
                        ].eq(1)
                        & case_strategy[
                            "has_position"
                        ].eq(1)
                        & case_strategy[
                            "position_size_pct"
                        ].gt(0)
                    ).sum()
                ),
                "approved_entries": (
                    approved_entries
                ),
                "completed_trades": len(
                    trades
                ),
                "transaction_cost_usd": float(
                    trades[
                        "transaction_cost_usd"
                    ].sum()
                )
                if not trades.empty
                else 0.0,
            }
        ]
    )

    print(
        f"{period_name:18s} | "
        f"{stress_type:14s} | "
        f"{excluded_value[:45]:45s} | "
        f"return "
        f"{summary['total_return_pct'].iloc[0]: .6f}% | "
        f"Sharpe "
        f"{summary['sharpe_ratio'].iloc[0]: .4f}"
    )

    return summary, diagnostic


def build_stress_deltas(
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
            "stress_type"
        ].eq("baseline"),
        ["period"] + metric_columns,
    ].copy()

    stressed = summaries.loc[
        ~summaries[
            "stress_type"
        ].eq("baseline"),
        [
            "period",
            "stress_type",
            "excluded_value",
        ]
        + metric_columns,
    ].copy()

    comparison = stressed.merge(
        baseline,
        on="period",
        how="left",
        validate="many_to_one",
        suffixes=(
            "_stressed",
            "_baseline",
        ),
    )

    for metric in metric_columns:
        comparison[
            f"delta_{metric}"
        ] = (
            comparison[
                f"{metric}_stressed"
            ]
            - comparison[
                f"{metric}_baseline"
            ]
        )

    # Positive values mean that excluding the item damaged
    # performance, indicating dependence on that item.
    comparison[
        "return_dependency_pct"
    ] = (
        comparison[
            "total_return_pct_baseline"
        ]
        - comparison[
            "total_return_pct_stressed"
        ]
    )

    comparison[
        "sharpe_dependency"
    ] = (
        comparison[
            "sharpe_ratio_baseline"
        ]
        - comparison[
            "sharpe_ratio_stressed"
        ]
    )

    comparison[
        "exclusion_improves_return"
    ] = (
        comparison[
            "delta_total_return_pct"
        ] > 0
    ).astype(int)

    comparison[
        "exclusion_improves_sharpe"
    ] = (
        comparison[
            "delta_sharpe_ratio"
        ] > 0
    ).astype(int)

    comparison[
        "exclusion_improves_drawdown"
    ] = (
        comparison[
            "delta_max_drawdown_pct"
        ] > 0
    ).astype(int)

    comparison[
        "positive_baseline_becomes_nonpositive"
    ] = (
        comparison[
            "total_return_pct_baseline"
        ].gt(0)
        & comparison[
            "total_return_pct_stressed"
        ].le(0)
    ).astype(int)

    comparison[
        "reduces_transaction_cost"
    ] = (
        comparison[
            "delta_total_transaction_cost_usd"
        ] < 0
    ).astype(int)

    return comparison


def build_category_summary(
    stress_deltas: pd.DataFrame,
) -> pd.DataFrame:
    records = []

    for (
        period,
        stress_type,
    ), group in stress_deltas.groupby(
        [
            "period",
            "stress_type",
        ],
        sort=True,
    ):
        most_dependent_row = (
            group.sort_values(
                "return_dependency_pct",
                ascending=False,
            )
            .iloc[0]
        )

        largest_detractor_row = (
            group.sort_values(
                "delta_total_return_pct",
                ascending=False,
            )
            .iloc[0]
        )

        records.append(
            {
                "period": period,
                "stress_type": stress_type,
                "cases": len(group),
                "largest_dependency": (
                    most_dependent_row[
                        "excluded_value"
                    ]
                ),
                "largest_return_dependency_pct": float(
                    most_dependent_row[
                        "return_dependency_pct"
                    ]
                ),
                "largest_detractor": (
                    largest_detractor_row[
                        "excluded_value"
                    ]
                ),
                "largest_return_improvement_pct": float(
                    largest_detractor_row[
                        "delta_total_return_pct"
                    ]
                ),
                "positive_to_nonpositive_cases": int(
                    group[
                        "positive_baseline_becomes_nonpositive"
                    ].sum()
                ),
                "exclusions_improving_return": int(
                    group[
                        "exclusion_improves_return"
                    ].sum()
                ),
                "exclusions_improving_sharpe": int(
                    group[
                        "exclusion_improves_sharpe"
                    ].sum()
                ),
            }
        )

    return pd.DataFrame(records)


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

    config = VARIANTS[
        VARIANT_NAME
    ]

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

    summaries = []
    diagnostics = []

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

        active_entry_mask = (
            weighted_strategy[
                "trade_candidate"
            ].eq(1)
            & weighted_strategy[
                "has_position"
            ].eq(1)
            & weighted_strategy[
                "position_size_pct"
            ].gt(0)
        )

        active_entries = (
            weighted_strategy.loc[
                active_entry_mask
            ]
        )

        currencies = sorted(
            active_entries[
                "currency"
            ]
            .dropna()
            .astype(str)
            .unique()
        )

        relationships = sorted(
            active_entries[
                "relationship_id"
            ]
            .dropna()
            .astype(str)
            .unique()
        )

        calendar_years = sorted(
            weighted_strategy[
                "date"
            ].dt.year.unique()
        )

        print(
            f"\n{'=' * 78}\n"
            f"Concentration stress: "
            f"{period_name}\n"
            f"Currencies: {len(currencies)}\n"
            f"Relationships: "
            f"{len(relationships)}\n"
            f"Calendar years: "
            f"{len(calendar_years)}\n"
            f"{'=' * 78}"
        )

        summary, diagnostic = run_case(
            period_name=period_name,
            stress_type="baseline",
            excluded_value="none",
            weighted_strategy=(
                weighted_strategy
            ),
            period_fx_prices=(
                period_fx_prices
            ),
        )

        summaries.append(summary)
        diagnostics.append(diagnostic)

        for currency in currencies:
            summary, diagnostic = run_case(
                period_name=period_name,
                stress_type="currency",
                excluded_value=currency,
                weighted_strategy=(
                    weighted_strategy
                ),
                period_fx_prices=(
                    period_fx_prices
                ),
            )

            summaries.append(summary)
            diagnostics.append(
                diagnostic
            )

        for relationship_id in relationships:
            summary, diagnostic = run_case(
                period_name=period_name,
                stress_type="relationship",
                excluded_value=(
                    relationship_id
                ),
                weighted_strategy=(
                    weighted_strategy
                ),
                period_fx_prices=(
                    period_fx_prices
                ),
            )

            summaries.append(summary)
            diagnostics.append(
                diagnostic
            )

        for calendar_year in calendar_years:
            summary, diagnostic = run_case(
                period_name=period_name,
                stress_type="calendar_year",
                excluded_value=str(
                    calendar_year
                ),
                weighted_strategy=(
                    weighted_strategy
                ),
                period_fx_prices=(
                    period_fx_prices
                ),
            )

            summaries.append(summary)
            diagnostics.append(
                diagnostic
            )

    concentration_summary = pd.concat(
        summaries,
        ignore_index=True,
    )

    concentration_diagnostics = (
        pd.concat(
            diagnostics,
            ignore_index=True,
        )
    )

    stress_deltas = build_stress_deltas(
        concentration_summary
    )

    category_summary = (
        build_category_summary(
            stress_deltas
        )
    )

    concentration_summary.to_csv(
        OUTPUT_ROOT
        / "concentration_stress_summary.csv",
        index=False,
    )

    concentration_diagnostics.to_csv(
        OUTPUT_ROOT
        / "concentration_stress_diagnostics.csv",
        index=False,
    )

    stress_deltas.to_csv(
        OUTPUT_ROOT
        / "concentration_stress_deltas.csv",
        index=False,
    )

    category_summary.to_csv(
        OUTPUT_ROOT
        / "concentration_category_summary.csv",
        index=False,
    )

    for stress_type in [
        "currency",
        "relationship",
        "calendar_year",
    ]:
        stress_deltas.loc[
            stress_deltas[
                "stress_type"
            ].eq(stress_type)
        ].to_csv(
            OUTPUT_ROOT
            / f"{stress_type}_leave_one_out.csv",
            index=False,
        )

    print(
        f"\n{'=' * 78}\n"
        "Concentration stress complete\n"
        f"{'=' * 78}"
    )

    print("\nCategory summary:")
    print(
        category_summary.to_string(
            index=False
        )
    )

    holdout_relationships = (
        stress_deltas.loc[
            stress_deltas[
                "period"
            ].eq("research_holdout")
            & stress_deltas[
                "stress_type"
            ].eq("relationship")
        ]
        .sort_values(
            "return_dependency_pct",
            ascending=False,
        )
    )

    print(
        "\nLargest holdout relationship "
        "dependencies:"
    )

    print(
        holdout_relationships[
            [
                "excluded_value",
                "total_return_pct_baseline",
                "total_return_pct_stressed",
                "return_dependency_pct",
                "sharpe_ratio_stressed",
                "positive_baseline_becomes_nonpositive",
            ]
        ]
        .head(10)
        .to_string(index=False)
    )

    print(
        "\nHoldout currency exclusions:"
    )

    print(
        stress_deltas.loc[
            stress_deltas[
                "period"
            ].eq("research_holdout")
            & stress_deltas[
                "stress_type"
            ].eq("currency"),
            [
                "excluded_value",
                "total_return_pct_stressed",
                "delta_total_return_pct",
                "sharpe_ratio_stressed",
                "delta_sharpe_ratio",
                "positive_baseline_becomes_nonpositive",
            ],
        ]
        .sort_values(
            "delta_total_return_pct"
        )
        .to_string(index=False)
    )

    print(
        "\nCalendar-year exclusions:"
    )

    print(
        stress_deltas.loc[
            stress_deltas[
                "stress_type"
            ].eq("calendar_year"),
            [
                "period",
                "excluded_value",
                "total_return_pct_stressed",
                "delta_total_return_pct",
                "sharpe_ratio_stressed",
                "delta_sharpe_ratio",
            ],
        ]
        .sort_values(
            [
                "period",
                "excluded_value",
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