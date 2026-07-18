from pathlib import Path

import pandas as pd

from strategy.backtest.run_backtest import (
    build_group_reports,
    calculate_summary,
    load_fx_prices,
    run_event_backtest,
)
from strategy.divergence.build_divergence import (
    build_divergence,
)
from strategy.experiments.run_layer_comparison import (
    save_variant_outputs,
)
from strategy.experiments.run_period_comparison import (
    slice_period,
)
from strategy.experiments.run_rolling_relationship_selection import (
    LOOKBACK_YEARS,
    MIN_TRAILING_NET_RETURN_PCT,
    MIN_TRAILING_PROFIT_FACTOR,
    MIN_TRAILING_TRADES,
    SELECTION_COST_BPS,
    WEAK_RELATIONSHIP_WEIGHT,
    apply_rolling_selection,
    build_rolling_selection_schedule,
    build_selection_counts,
)
from strategy.rules.build_trade_rules import (
    build_trade_rules,
)
from strategy.signals.build_signals import (
    build_signals,
)
from strategy.sizing.build_position_sizes import (
    build_position_sizes,
)


FEATURES_PATH = Path(
    "strategy/output/daily_features.csv"
)

OUTPUT_ROOT = Path(
    "strategy/output/experiments/"
    "final_layer_ablation"
)


VARIANTS = {
    "market_fundamentals": {
        "use_sentiment": False,
        "use_fundamentals": True,
    },
    "market_fundamentals_news": {
        "use_sentiment": True,
        "use_fundamentals": True,
    },
}


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


ROUND_TRIP_COSTS_BPS = [
    0.0,
    2.0,
]


UNIVERSE_MODE = "rolling_soft_weight"


def build_candidate_strategy(
    features: pd.DataFrame,
    *,
    variant_name: str,
    config: dict[str, bool],
) -> pd.DataFrame:
    """
    Build one information-layer variant while keeping:

    - divergence enabled;
    - signal-volatility sizing;
    - all signal and risk thresholds unchanged.
    """
    signals = build_signals(
        features,
        use_sentiment=config[
            "use_sentiment"
        ],
        use_fundamentals=config[
            "use_fundamentals"
        ],
    )

    divergence = build_divergence(
        signals
    )

    trade_rules = build_trade_rules(
        divergence,
        enable_confirmed=True,
        enable_divergence=True,
    )

    sized_trades = build_position_sizes(
        trade_rules
    )

    sized_trades["date"] = pd.to_datetime(
        sized_trades["date"]
    )

    duplicate_count = (
        sized_trades.duplicated(
            [
                "relationship_id",
                "date",
            ]
        ).sum()
    )

    if duplicate_count != 0:
        raise ValueError(
            f"{variant_name}: candidate strategy "
            f"contains {duplicate_count} duplicate "
            "relationship/date rows."
        )

    expected_sentiment = int(
        config["use_sentiment"]
    )

    expected_fundamentals = int(
        config["use_fundamentals"]
    )

    if not signals[
        "uses_sentiment_layer"
    ].eq(expected_sentiment).all():
        raise ValueError(
            f"{variant_name}: incorrect "
            "sentiment-layer metadata."
        )

    if not signals[
        "uses_fundamental_layer"
    ].eq(expected_fundamentals).all():
        raise ValueError(
            f"{variant_name}: incorrect "
            "fundamental-layer metadata."
        )

    if not config["use_sentiment"]:
        nonzero_sentiment = int(
            signals[
                "sentiment_layer_score"
            ].ne(0).sum()
        )

        if nonzero_sentiment != 0:
            raise ValueError(
                f"{variant_name}: disabled "
                "sentiment layer contains "
                f"{nonzero_sentiment} "
                "nonzero rows."
            )

    divergence_entries = int(
        trade_rules[
            "confirmed_divergence_entry"
        ].sum()
    )

    if divergence_entries == 0:
        raise ValueError(
            f"{variant_name}: divergence is "
            "enabled but no divergence entries "
            "were created."
        )

    return sized_trades


def run_variant_selection_history(
    *,
    variant_name: str,
    candidate_strategy: pd.DataFrame,
    fx_prices: pd.DataFrame,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
]:
    """
    Build a variant-specific causal trade history.

    The resulting completed trades determine that
    variant's annual rolling relationship weights.
    """
    print(
        f"\n{'=' * 78}\n"
        f"Building selection history: "
        f"{variant_name}\n"
        f"Selection cost: "
        f"{SELECTION_COST_BPS:.1f} bps\n"
        f"{'=' * 78}"
    )

    trades, equity, decisions = (
        run_event_backtest(
            candidate_strategy,
            fx_prices,
            round_trip_cost_bps=(
                SELECTION_COST_BPS
            ),
        )
    )

    if trades.empty:
        raise ValueError(
            f"{variant_name}: selection-history "
            "backtest produced no completed trades."
        )

    for date_column in [
        "signal_date",
        "entry_date",
        "exit_date",
    ]:
        trades[date_column] = pd.to_datetime(
            trades[date_column]
        )

    return trades, equity, decisions


def build_run_diagnostics(
    *,
    period_name: str,
    variant_name: str,
    config: dict[str, bool],
    round_trip_cost_bps: float,
    weighted_strategy: pd.DataFrame,
) -> pd.DataFrame:
    yearly_records = []

    for selection_year, group in (
        weighted_strategy.groupby(
            "selection_year"
        )
    ):
        qualified_relationships = int(
            group.loc[
                group["selected"].eq(1),
                "relationship_id",
            ].nunique()
        )

        traded_relationships = int(
            group.loc[
                group["has_position"].eq(1),
                "relationship_id",
            ].nunique()
        )

        yearly_records.append(
            {
                "selection_year": (
                    selection_year
                ),
                "qualified_relationships": (
                    qualified_relationships
                ),
                "traded_relationships": (
                    traded_relationships
                ),
            }
        )

    yearly = pd.DataFrame(
        yearly_records
    )

    return pd.DataFrame(
        [
            {
                "period": period_name,
                "variant": variant_name,
                "uses_sentiment": int(
                    config["use_sentiment"]
                ),
                "uses_fundamentals": int(
                    config[
                        "use_fundamentals"
                    ]
                ),
                "sizing_mode": (
                    "signal_volatility"
                ),
                "divergence_enabled": 1,
                "universe_mode": (
                    UNIVERSE_MODE
                ),
                "round_trip_cost_bps": (
                    round_trip_cost_bps
                ),
                "selection_cost_bps": (
                    SELECTION_COST_BPS
                ),
                "lookback_years": (
                    LOOKBACK_YEARS
                ),
                "minimum_trailing_trades": (
                    MIN_TRAILING_TRADES
                ),
                "minimum_trailing_net_return_pct": (
                    MIN_TRAILING_NET_RETURN_PCT
                ),
                "minimum_trailing_profit_factor": (
                    MIN_TRAILING_PROFIT_FACTOR
                ),
                "weak_relationship_weight": (
                    WEAK_RELATIONSHIP_WEIGHT
                ),
                "rows": len(
                    weighted_strategy
                ),
                "relationships_available": int(
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
                "average_selection_weight": float(
                    weighted_strategy[
                        "selection_weight"
                    ].mean()
                ),
                "minimum_yearly_qualified_relationships": int(
                    yearly[
                        "qualified_relationships"
                    ].min()
                ),
                "maximum_yearly_qualified_relationships": int(
                    yearly[
                        "qualified_relationships"
                    ].max()
                ),
                "average_yearly_qualified_relationships": float(
                    yearly[
                        "qualified_relationships"
                    ].mean()
                ),
                "minimum_yearly_traded_relationships": int(
                    yearly[
                        "traded_relationships"
                    ].min()
                ),
                "maximum_yearly_traded_relationships": int(
                    yearly[
                        "traded_relationships"
                    ].max()
                ),
            }
        ]
    )


def add_summary_metadata(
    summary: pd.DataFrame,
    *,
    period_name: str,
    variant_name: str,
    config: dict[str, bool],
    effective_start: pd.Timestamp,
    effective_end: pd.Timestamp,
) -> pd.DataFrame:
    metadata = pd.DataFrame(
        [
            {
                "period": period_name,
                "variant": variant_name,
                "uses_sentiment": int(
                    config["use_sentiment"]
                ),
                "uses_fundamentals": int(
                    config[
                        "use_fundamentals"
                    ]
                ),
                "effective_period_start": (
                    effective_start.date()
                ),
                "effective_period_end": (
                    effective_end.date()
                ),
                "sizing_mode": (
                    "signal_volatility"
                ),
                "divergence_enabled": 1,
                "universe_mode": (
                    UNIVERSE_MODE
                ),
                "selection_method": (
                    "rolling_trailing_performance"
                ),
                "selection_cost_bps": (
                    SELECTION_COST_BPS
                ),
                "lookback_years": (
                    LOOKBACK_YEARS
                ),
                "minimum_trailing_trades": (
                    MIN_TRAILING_TRADES
                ),
                "minimum_trailing_net_return_pct": (
                    MIN_TRAILING_NET_RETURN_PCT
                ),
                "minimum_trailing_profit_factor": (
                    MIN_TRAILING_PROFIT_FACTOR
                ),
                "weak_relationship_weight": (
                    WEAK_RELATIONSHIP_WEIGHT
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


def run_evaluation_case(
    *,
    period_name: str,
    variant_name: str,
    config: dict[str, bool],
    round_trip_cost_bps: float,
    weighted_strategy: pd.DataFrame,
    period_fx_prices: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if weighted_strategy.empty:
        raise ValueError(
            f"{period_name}/{variant_name}: "
            "no strategy rows."
        )

    if period_fx_prices.empty:
        raise ValueError(
            f"{period_name}: no FX rows."
        )

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
        f"Variant: {variant_name}\n"
        f"Universe: {UNIVERSE_MODE}\n"
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
        variant_name=variant_name,
        config=config,
        effective_start=effective_start,
        effective_end=effective_end,
    )

    diagnostics = build_run_diagnostics(
        period_name=period_name,
        variant_name=variant_name,
        config=config,
        round_trip_cost_bps=(
            round_trip_cost_bps
        ),
        weighted_strategy=weighted_strategy,
    )

    reports = build_group_reports(
        trades
    )

    cost_folder = (
        f"{round_trip_cost_bps:g}_bps"
    )

    output_dir = (
        OUTPUT_ROOT
        / variant_name
        / period_name
        / cost_folder
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
                "variant",
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


def build_news_deltas(
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
        "win_rate_pct",
        "profit_factor",
        "total_trades",
        "total_transaction_cost_usd",
    ]

    fundamentals = summaries.loc[
        summaries["variant"].eq(
            "market_fundamentals"
        ),
        key_columns + metric_columns,
    ].copy()

    full_model = summaries.loc[
        summaries["variant"].eq(
            "market_fundamentals_news"
        ),
        key_columns + metric_columns,
    ].copy()

    comparison = fundamentals.merge(
        full_model,
        on=key_columns,
        how="inner",
        validate="one_to_one",
        suffixes=(
            "_market_fundamentals",
            "_market_fundamentals_news",
        ),
    )

    for metric in metric_columns:
        comparison[
            f"delta_{metric}"
        ] = (
            comparison[
                f"{metric}_"
                "market_fundamentals_news"
            ]
            - comparison[
                f"{metric}_"
                "market_fundamentals"
            ]
        )

    comparison[
        "news_improves_return"
    ] = (
        comparison[
            "delta_total_return_pct"
        ] > 0
    ).astype(int)

    comparison[
        "news_improves_sharpe"
    ] = (
        comparison[
            "delta_sharpe_ratio"
        ] > 0
    ).astype(int)

    comparison[
        "news_improves_drawdown"
    ] = (
        comparison[
            "delta_max_drawdown_pct"
        ] > 0
    ).astype(int)

    comparison[
        "news_improves_profit_factor"
    ] = (
        comparison[
            "delta_profit_factor"
        ] > 0
    ).astype(int)

    comparison[
        "news_reduces_transaction_cost"
    ] = (
        comparison[
            "delta_total_transaction_cost_usd"
        ] < 0
    ).astype(int)

    return comparison


def build_selection_overlap(
    schedules: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    fundamentals = schedules[
        "market_fundamentals"
    ]

    full_model = schedules[
        "market_fundamentals_news"
    ]

    years = sorted(
        set(
            fundamentals[
                "selection_year"
            ].unique()
        )
        | set(
            full_model[
                "selection_year"
            ].unique()
        )
    )

    records = []

    for year in years:
        fundamentals_selected = set(
            fundamentals.loc[
                fundamentals[
                    "selection_year"
                ].eq(year)
                & fundamentals[
                    "selected"
                ].eq(1),
                "relationship_id",
            ]
        )

        full_selected = set(
            full_model.loc[
                full_model[
                    "selection_year"
                ].eq(year)
                & full_model[
                    "selected"
                ].eq(1),
                "relationship_id",
            ]
        )

        intersection = (
            fundamentals_selected
            & full_selected
        )

        union = (
            fundamentals_selected
            | full_selected
        )

        if len(union) == 0:
            jaccard_similarity = 1.0
        else:
            jaccard_similarity = (
                len(intersection)
                / len(union)
            )

        records.append(
            {
                "selection_year": year,
                "fundamentals_selected_relationships": (
                    len(fundamentals_selected)
                ),
                "full_model_selected_relationships": (
                    len(full_selected)
                ),
                "shared_selected_relationships": (
                    len(intersection)
                ),
                "selection_union_relationships": (
                    len(union)
                ),
                "selection_jaccard_similarity": (
                    jaccard_similarity
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

    fx_prices = load_fx_prices()

    OUTPUT_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    candidate_strategies = {}
    selection_schedules = {}

    summaries: list[pd.DataFrame] = []
    diagnostics: list[pd.DataFrame] = []
    selection_count_frames = []

    for (
        variant_name,
        config,
    ) in VARIANTS.items():
        candidate_strategy = (
            build_candidate_strategy(
                features,
                variant_name=variant_name,
                config=config,
            )
        )

        candidate_strategies[
            variant_name
        ] = candidate_strategy

        (
            history_trades,
            history_equity,
            history_decisions,
        ) = run_variant_selection_history(
            variant_name=variant_name,
            candidate_strategy=(
                candidate_strategy
            ),
            fx_prices=fx_prices,
        )

        selection_schedule = (
            build_rolling_selection_schedule(
                history_trades,
                candidate_strategy,
            )
        )

        selection_schedules[
            variant_name
        ] = selection_schedule

        selection_counts = (
            build_selection_counts(
                selection_schedule
            )
        )

        selection_counts.insert(
            0,
            "variant",
            variant_name,
        )

        selection_count_frames.append(
            selection_counts
        )

        variant_output_dir = (
            OUTPUT_ROOT
            / variant_name
        )

        history_output_dir = (
            variant_output_dir
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
            / "equity_curve_2bps.csv",
            index=False,
        )

        history_decisions.to_csv(
            history_output_dir
            / "entry_decisions_2bps.csv",
            index=False,
        )

        selection_schedule.to_csv(
            variant_output_dir
            / "rolling_selection_schedule.csv",
            index=False,
        )

        selection_schedule.loc[
            selection_schedule[
                "selected"
            ].eq(1)
        ].to_csv(
            variant_output_dir
            / "selected_relationships_by_year.csv",
            index=False,
        )

        print(
            f"\nAnnual selection counts: "
            f"{variant_name}"
        )

        print(
            selection_counts.to_string(
                index=False
            )
        )

    combined_selection_counts = pd.concat(
        selection_count_frames,
        ignore_index=True,
    )

    combined_selection_counts.to_csv(
        OUTPUT_ROOT
        / "selection_counts_by_variant.csv",
        index=False,
    )

    selection_overlap = (
        build_selection_overlap(
            selection_schedules
        )
    )

    selection_overlap.to_csv(
        OUTPUT_ROOT
        / "selection_overlap_by_year.csv",
        index=False,
    )

    for (
        variant_name,
        config,
    ) in VARIANTS.items():
        candidate_strategy = (
            candidate_strategies[
                variant_name
            ]
        )

        selection_schedule = (
            selection_schedules[
                variant_name
            ]
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

            for cost_bps in (
                ROUND_TRIP_COSTS_BPS
            ):
                (
                    summary,
                    diagnostic,
                ) = run_evaluation_case(
                    period_name=period_name,
                    variant_name=variant_name,
                    config=config,
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

    news_deltas = build_news_deltas(
        comparison_summary
    )

    comparison_summary.to_csv(
        OUTPUT_ROOT
        / "final_layer_ablation_summary.csv",
        index=False,
    )

    comparison_diagnostics.to_csv(
        OUTPUT_ROOT
        / "final_layer_ablation_diagnostics.csv",
        index=False,
    )

    news_deltas.to_csv(
        OUTPUT_ROOT
        / "final_layer_ablation_deltas.csv",
        index=False,
    )

    print(
        f"\n{'=' * 78}\n"
        "Final information-layer ablation complete\n"
        f"{'=' * 78}"
    )

    display_columns = [
        "period",
        "variant",
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

    print(
        comparison_summary[
            display_columns
        ].to_string(index=False)
    )

    print(
        "\nNews-layer deltas "
        "(full model minus fundamentals):"
    )

    delta_columns = [
        "period",
        "round_trip_cost_bps",
        "delta_total_return_pct",
        "delta_sharpe_ratio",
        "delta_max_drawdown_pct",
        "delta_profit_factor",
        "delta_total_trades",
        "delta_total_transaction_cost_usd",
        "news_improves_return",
        "news_improves_sharpe",
        "news_improves_drawdown",
        "news_improves_profit_factor",
        "news_reduces_transaction_cost",
    ]

    print(
        news_deltas[
            delta_columns
        ].to_string(index=False)
    )

    print(
        "\nAnnual selection overlap:"
    )

    print(
        selection_overlap.to_string(
            index=False
        )
    )

    print(
        "\nSaved outputs to:"
        f"\n{OUTPUT_ROOT}"
    )


if __name__ == "__main__":
    main()