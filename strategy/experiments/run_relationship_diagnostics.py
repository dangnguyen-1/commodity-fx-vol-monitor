from pathlib import Path

import numpy as np
import pandas as pd


EXPERIMENT_ROOT = Path(
    "strategy/output/experiments/"
    "divergence_ablation"
)

OUTPUT_ROOT = Path(
    "strategy/output/experiments/"
    "relationship_diagnostics"
)


# Diagnose the strongest candidate strategy:
#
# market + fundamentals + news
# signal-volatility sizing
# divergence enabled
#
# We load both 0-bps and 2-bps runs so that gross signal
# quality and realistic after-cost performance can be
# examined separately.
RUN_SPECS = [
    {
        "period": "validation",
        "round_trip_cost_bps": 0.0,
        "path": (
            EXPERIMENT_ROOT
            / "validation"
            / "signal_volatility"
            / "market_fundamentals_news"
            / "with_divergence"
            / "0_bps"
            / "trades.csv"
        ),
    },
    {
        "period": "validation",
        "round_trip_cost_bps": 2.0,
        "path": (
            EXPERIMENT_ROOT
            / "validation"
            / "signal_volatility"
            / "market_fundamentals_news"
            / "with_divergence"
            / "2_bps"
            / "trades.csv"
        ),
    },
    {
        "period": "research_holdout",
        "round_trip_cost_bps": 0.0,
        "path": (
            EXPERIMENT_ROOT
            / "test"
            / "signal_volatility"
            / "market_fundamentals_news"
            / "with_divergence"
            / "0_bps"
            / "trades.csv"
        ),
    },
    {
        "period": "research_holdout",
        "round_trip_cost_bps": 2.0,
        "path": (
            EXPERIMENT_ROOT
            / "test"
            / "signal_volatility"
            / "market_fundamentals_news"
            / "with_divergence"
            / "2_bps"
            / "trades.csv"
        ),
    },
]


MIN_TRADES_PER_PERIOD = 20


RELATIONSHIP_COLUMNS = [
    "relationship_id",
    "commodity",
    "currency",
    "fx_symbol",
    "relationship_type",
]


REQUIRED_COLUMNS = [
    "position_id",
    "signal_date",
    "entry_date",
    "exit_date",
    "relationship_id",
    "commodity",
    "currency",
    "fx_symbol",
    "relationship_type",
    "primary_trade_rule",
    "trade_direction",
    "notional_usd",
    "position_size_pct_at_entry",
    "gross_pnl_usd",
    "transaction_cost_usd",
    "net_pnl_usd",
    "trade_return_on_notional",
    "winning_trade",
    "exit_reason",
    "actual_holding_calendar_days",
    "combined_trade_score",
    "confirmation_score",
    "divergence_score",
]


def safe_divide(
    numerator: float,
    denominator: float,
) -> float:
    if denominator == 0:
        return np.nan

    return float(
        numerator / denominator
    )


def calculate_profit_factor(
    pnl: pd.Series,
) -> float:
    positive_pnl = float(
        pnl.loc[pnl > 0].sum()
    )

    negative_pnl = float(
        -pnl.loc[pnl < 0].sum()
    )

    if negative_pnl == 0:
        if positive_pnl > 0:
            return np.inf

        return np.nan

    return positive_pnl / negative_pnl


def load_trade_runs() -> pd.DataFrame:
    runs: list[pd.DataFrame] = []

    for spec in RUN_SPECS:
        path = spec["path"]

        if not path.exists():
            raise FileNotFoundError(
                f"Missing trade file: {path}"
            )

        trades = pd.read_csv(path)

        missing_columns = sorted(
            set(REQUIRED_COLUMNS)
            - set(trades.columns)
        )

        if missing_columns:
            raise ValueError(
                f"{path} is missing required "
                f"columns: {missing_columns}"
            )

        for date_column in [
            "signal_date",
            "entry_date",
            "exit_date",
        ]:
            trades[date_column] = (
                pd.to_datetime(
                    trades[date_column]
                )
            )

        trades["period"] = spec["period"]

        trades[
            "round_trip_cost_bps"
        ] = spec[
            "round_trip_cost_bps"
        ]

        trades["calendar_year"] = (
            trades["entry_date"].dt.year
        )

        trades[
            "is_confirmed_divergence"
        ] = (
            trades[
                "primary_trade_rule"
            ]
            .eq("confirmed_divergence")
            .astype(int)
        )

        trades["is_long_trade"] = (
            trades["trade_direction"]
            .eq(1)
            .astype(int)
        )

        runs.append(trades)

    combined = pd.concat(
        runs,
        ignore_index=True,
    )

    duplicate_count = combined.duplicated(
        [
            "period",
            "round_trip_cost_bps",
            "position_id",
        ]
    ).sum()

    if duplicate_count != 0:
        raise ValueError(
            "Combined trade data contains "
            f"{duplicate_count} duplicate "
            "period/cost/position rows."
        )

    return combined


def summarize_groups(
    trades: pd.DataFrame,
    group_columns: list[str],
) -> pd.DataFrame:
    records: list[dict] = []

    grouped = trades.groupby(
        group_columns,
        dropna=False,
        sort=True,
    )

    for keys, group in grouped:
        if not isinstance(keys, tuple):
            keys = (keys,)

        record = dict(
            zip(group_columns, keys)
        )

        total_notional = float(
            group["notional_usd"]
            .abs()
            .sum()
        )

        gross_pnl = float(
            group["gross_pnl_usd"].sum()
        )

        transaction_cost = float(
            group[
                "transaction_cost_usd"
            ].sum()
        )

        net_pnl = float(
            group["net_pnl_usd"].sum()
        )

        trade_count = len(group)

        record.update(
            {
                "total_trades": trade_count,
                "winning_trades": int(
                    group[
                        "winning_trade"
                    ].sum()
                ),
                "win_rate_pct": (
                    100.0
                    * group[
                        "winning_trade"
                    ].mean()
                ),
                "total_notional_usd": (
                    total_notional
                ),
                "gross_pnl_usd": gross_pnl,
                "transaction_cost_usd": (
                    transaction_cost
                ),
                "net_pnl_usd": net_pnl,
                "gross_return_on_notional_pct": (
                    100.0
                    * safe_divide(
                        gross_pnl,
                        total_notional,
                    )
                ),
                "net_return_on_notional_pct": (
                    100.0
                    * safe_divide(
                        net_pnl,
                        total_notional,
                    )
                ),
                "cost_drag_bps": (
                    10000.0
                    * safe_divide(
                        transaction_cost,
                        total_notional,
                    )
                ),
                "mean_net_pnl_usd": float(
                    group[
                        "net_pnl_usd"
                    ].mean()
                ),
                "median_net_pnl_usd": float(
                    group[
                        "net_pnl_usd"
                    ].median()
                ),
                "mean_trade_return_bps": (
                    10000.0
                    * group[
                        "trade_return_on_notional"
                    ].mean()
                ),
                "median_trade_return_bps": (
                    10000.0
                    * group[
                        "trade_return_on_notional"
                    ].median()
                ),
                "gross_profit_factor": (
                    calculate_profit_factor(
                        group[
                            "gross_pnl_usd"
                        ]
                    )
                ),
                "net_profit_factor": (
                    calculate_profit_factor(
                        group[
                            "net_pnl_usd"
                        ]
                    )
                ),
                "average_holding_days": float(
                    group[
                        "actual_holding_calendar_days"
                    ].mean()
                ),
                "average_position_size_pct": (
                    100.0
                    * group[
                        "position_size_pct_at_entry"
                    ].mean()
                ),
                "average_combined_trade_score": (
                    float(
                        group[
                            "combined_trade_score"
                        ].mean()
                    )
                ),
                "average_confirmation_score": (
                    float(
                        group[
                            "confirmation_score"
                        ].mean()
                    )
                ),
                "average_divergence_score": (
                    float(
                        group[
                            "divergence_score"
                        ].mean()
                    )
                ),
                "confirmed_divergence_share_pct": (
                    100.0
                    * group[
                        "is_confirmed_divergence"
                    ].mean()
                ),
                "long_trade_share_pct": (
                    100.0
                    * group[
                        "is_long_trade"
                    ].mean()
                ),
                "signal_flip_exit_share_pct": (
                    100.0
                    * group[
                        "exit_reason"
                    ]
                    .eq("signal_flip")
                    .mean()
                ),
                "divergence_close_exit_share_pct": (
                    100.0
                    * group[
                        "exit_reason"
                    ]
                    .eq("divergence_close")
                    .mean()
                ),
                "holding_period_exit_share_pct": (
                    100.0
                    * group[
                        "exit_reason"
                    ]
                    .eq("holding_period")
                    .mean()
                ),
            }
        )

        records.append(record)

    return pd.DataFrame(records)


def build_relationship_stability(
    relationship_summary: pd.DataFrame,
) -> pd.DataFrame:
    metrics = [
        "total_trades",
        "win_rate_pct",
        "gross_pnl_usd",
        "transaction_cost_usd",
        "net_pnl_usd",
        "gross_return_on_notional_pct",
        "net_return_on_notional_pct",
        "gross_profit_factor",
        "net_profit_factor",
        "average_holding_days",
        "confirmed_divergence_share_pct",
    ]

    wide = relationship_summary.pivot(
        index=RELATIONSHIP_COLUMNS,
        columns=[
            "period",
            "round_trip_cost_bps",
        ],
        values=metrics,
    )

    wide.columns = [
        (
            f"{metric}_"
            f"{period}_"
            f"{cost:g}bps"
        )
        for metric, period, cost
        in wide.columns
    ]

    wide = wide.reset_index()

    validation_zero = (
        "net_return_on_notional_pct_"
        "validation_0bps"
    )

    validation_two = (
        "net_return_on_notional_pct_"
        "validation_2bps"
    )

    holdout_zero = (
        "net_return_on_notional_pct_"
        "research_holdout_0bps"
    )

    holdout_two = (
        "net_return_on_notional_pct_"
        "research_holdout_2bps"
    )

    validation_trades = (
        "total_trades_validation_2bps"
    )

    holdout_trades = (
        "total_trades_"
        "research_holdout_2bps"
    )

    wide["sufficient_trades"] = (
        wide[validation_trades].ge(
            MIN_TRADES_PER_PERIOD
        )
        & wide[holdout_trades].ge(
            MIN_TRADES_PER_PERIOD
        )
    ).astype(int)

    wide[
        "gross_positive_both_periods"
    ] = (
        wide[validation_zero].gt(0)
        & wide[holdout_zero].gt(0)
    ).astype(int)

    wide[
        "net_positive_both_periods"
    ] = (
        wide[validation_two].gt(0)
        & wide[holdout_two].gt(0)
    ).astype(int)

    wide[
        "survives_costs_both_periods"
    ] = (
        wide[
            "gross_positive_both_periods"
        ].eq(1)
        & wide[
            "net_positive_both_periods"
        ].eq(1)
    ).astype(int)

    validation_positive = (
        wide[validation_two] > 0
    )

    holdout_positive = (
        wide[holdout_two] > 0
    )

    wide["stability_class_2bps"] = (
        np.select(
            [
                (
                    validation_positive
                    & holdout_positive
                ),
                (
                    validation_positive
                    & ~holdout_positive
                ),
                (
                    ~validation_positive
                    & holdout_positive
                ),
            ],
            [
                "consistent_profitable",
                "validation_only",
                "holdout_only",
            ],
            default=(
                "consistent_unprofitable"
            ),
        )
    )

    wide[
        "minimum_period_net_return_pct"
    ] = wide[
        [
            validation_two,
            holdout_two,
        ]
    ].min(axis=1)

    wide[
        "average_period_net_return_pct"
    ] = wide[
        [
            validation_two,
            holdout_two,
        ]
    ].mean(axis=1)

    wide[
        "holdout_minus_validation_return_pct"
    ] = (
        wide[holdout_two]
        - wide[validation_two]
    )

    wide[
        "validation_cost_impact_pct"
    ] = (
        wide[validation_two]
        - wide[validation_zero]
    )

    wide[
        "holdout_cost_impact_pct"
    ] = (
        wide[holdout_two]
        - wide[holdout_zero]
    )

    return (
        wide.sort_values(
            [
                "survives_costs_both_periods",
                "sufficient_trades",
                "minimum_period_net_return_pct",
                "average_period_net_return_pct",
            ],
            ascending=[
                False,
                False,
                False,
                False,
            ],
        )
        .reset_index(drop=True)
    )


def main() -> None:
    trades = load_trade_runs()

    OUTPUT_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    relationship_groups = [
        "period",
        "round_trip_cost_bps",
    ] + RELATIONSHIP_COLUMNS

    relationship_summary = (
        summarize_groups(
            trades,
            relationship_groups,
        )
    )

    overall_summary = summarize_groups(
        trades,
        [
            "period",
            "round_trip_cost_bps",
        ],
    )

    primary_rule_summary = (
        summarize_groups(
            trades,
            [
                "period",
                "round_trip_cost_bps",
                "primary_trade_rule",
            ],
        )
    )

    exit_reason_summary = (
        summarize_groups(
            trades,
            [
                "period",
                "round_trip_cost_bps",
                "exit_reason",
            ],
        )
    )

    direction_summary = summarize_groups(
        trades,
        [
            "period",
            "round_trip_cost_bps",
            "trade_direction",
        ],
    )

    relationship_type_summary = (
        summarize_groups(
            trades,
            [
                "period",
                "round_trip_cost_bps",
                "relationship_type",
            ],
        )
    )

    yearly_summary = summarize_groups(
        trades,
        [
            "period",
            "round_trip_cost_bps",
            "calendar_year",
        ],
    )

    two_bps_trades = trades.loc[
        trades[
            "round_trip_cost_bps"
        ].eq(2.0)
    ].copy()

    relationship_rule_summary = (
        summarize_groups(
            two_bps_trades,
            (
                [
                    "period",
                ]
                + RELATIONSHIP_COLUMNS
                + [
                    "primary_trade_rule",
                ]
            ),
        )
    )

    relationship_exit_summary = (
        summarize_groups(
            two_bps_trades,
            (
                [
                    "period",
                ]
                + RELATIONSHIP_COLUMNS
                + [
                    "exit_reason",
                ]
            ),
        )
    )

    relationship_year_summary = (
        summarize_groups(
            two_bps_trades,
            (
                [
                    "period",
                    "calendar_year",
                ]
                + RELATIONSHIP_COLUMNS
            ),
        )
    )

    relationship_stability = (
        build_relationship_stability(
            relationship_summary
        )
    )

    trades.to_csv(
        OUTPUT_ROOT
        / "combined_trade_runs.csv",
        index=False,
    )

    overall_summary.to_csv(
        OUTPUT_ROOT
        / "overall_summary.csv",
        index=False,
    )

    relationship_summary.to_csv(
        OUTPUT_ROOT
        / "relationship_summary.csv",
        index=False,
    )

    relationship_stability.to_csv(
        OUTPUT_ROOT
        / "relationship_stability.csv",
        index=False,
    )

    primary_rule_summary.to_csv(
        OUTPUT_ROOT
        / "primary_rule_summary.csv",
        index=False,
    )

    exit_reason_summary.to_csv(
        OUTPUT_ROOT
        / "exit_reason_summary.csv",
        index=False,
    )

    direction_summary.to_csv(
        OUTPUT_ROOT
        / "direction_summary.csv",
        index=False,
    )

    relationship_type_summary.to_csv(
        OUTPUT_ROOT
        / "relationship_type_summary.csv",
        index=False,
    )

    yearly_summary.to_csv(
        OUTPUT_ROOT
        / "yearly_summary.csv",
        index=False,
    )

    relationship_rule_summary.to_csv(
        OUTPUT_ROOT
        / "relationship_rule_summary_2bps.csv",
        index=False,
    )

    relationship_exit_summary.to_csv(
        OUTPUT_ROOT
        / "relationship_exit_summary_2bps.csv",
        index=False,
    )

    relationship_year_summary.to_csv(
        OUTPUT_ROOT
        / "relationship_year_summary_2bps.csv",
        index=False,
    )

    print(
        f"\n{'=' * 78}\n"
        "Relationship diagnostics complete\n"
        f"{'=' * 78}"
    )

    print("\nLoaded trade runs:")
    print(
        trades.groupby(
            [
                "period",
                "round_trip_cost_bps",
            ]
        )
        .size()
        .rename("trades")
        .reset_index()
        .to_string(index=False)
    )

    print("\nOverall performance:")
    print(
        overall_summary[
            [
                "period",
                "round_trip_cost_bps",
                "total_trades",
                "gross_pnl_usd",
                "transaction_cost_usd",
                "net_pnl_usd",
                "net_return_on_notional_pct",
                "win_rate_pct",
                "net_profit_factor",
            ]
        ].to_string(index=False)
    )

    print(
        "\nTop relationship stability results:"
    )

    stability_columns = [
        "relationship_id",
        "relationship_type",
        "total_trades_validation_2bps",
        (
            "net_return_on_notional_pct_"
            "validation_2bps"
        ),
        (
            "net_profit_factor_"
            "validation_2bps"
        ),
        (
            "total_trades_"
            "research_holdout_2bps"
        ),
        (
            "net_return_on_notional_pct_"
            "research_holdout_2bps"
        ),
        (
            "net_profit_factor_"
            "research_holdout_2bps"
        ),
        "stability_class_2bps",
        "sufficient_trades",
        "survives_costs_both_periods",
    ]

    print(
        relationship_stability[
            stability_columns
        ]
        .head(32)
        .to_string(index=False)
    )

    print(
        "\nSaved outputs to:"
        f"\n{OUTPUT_ROOT}"
    )


if __name__ == "__main__":
    main()