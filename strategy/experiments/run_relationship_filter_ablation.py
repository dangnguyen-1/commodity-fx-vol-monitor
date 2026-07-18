from pathlib import Path

import numpy as np
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

SELECTION_SOURCE_PATH = Path(
    "strategy/output/experiments/"
    "divergence_ablation/"
    "validation/"
    "signal_volatility/"
    "market_fundamentals_news/"
    "with_divergence/"
    "2_bps/"
    "trades.csv"
)

OUTPUT_ROOT = Path(
    "strategy/output/experiments/"
    "relationship_filter_ablation"
)


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


UNIVERSE_MODES = [
    "all_relationships",
    "validation_filtered",
]


ROUND_TRIP_COSTS_BPS = [
    0.0,
    2.0,
]


MIN_VALIDATION_TRADES = 20

MIN_VALIDATION_NET_RETURN_PCT = 0.0

MIN_VALIDATION_PROFIT_FACTOR = 1.0


RELATIONSHIP_METADATA_COLUMNS = [
    "relationship_id",
    "commodity",
    "currency",
    "fx_symbol",
    "relationship_type",
]


SELECTION_REQUIRED_COLUMNS = [
    "relationship_id",
    "commodity",
    "currency",
    "fx_symbol",
    "relationship_type",
    "notional_usd",
    "net_pnl_usd",
    "transaction_cost_usd",
    "winning_trade",
]


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


def build_validation_selection(
    trades_path: Path,
) -> pd.DataFrame:
    """
    Select relationships using only validation-period
    performance under the realistic 2-bps cost assumption.
    """
    if not trades_path.exists():
        raise FileNotFoundError(
            "Missing validation selection file: "
            f"{trades_path}"
        )

    trades = pd.read_csv(
        trades_path
    )

    missing_columns = sorted(
        set(SELECTION_REQUIRED_COLUMNS)
        - set(trades.columns)
    )

    if missing_columns:
        raise ValueError(
            "Validation trade file is missing "
            f"required columns: {missing_columns}"
        )

    records: list[dict] = []

    grouped = trades.groupby(
        RELATIONSHIP_METADATA_COLUMNS,
        dropna=False,
        sort=True,
    )

    for keys, group in grouped:
        record = dict(
            zip(
                RELATIONSHIP_METADATA_COLUMNS,
                keys,
            )
        )

        total_trades = len(group)

        total_notional = float(
            group["notional_usd"]
            .abs()
            .sum()
        )

        net_pnl = float(
            group["net_pnl_usd"].sum()
        )

        transaction_cost = float(
            group[
                "transaction_cost_usd"
            ].sum()
        )

        if total_notional == 0:
            net_return_pct = np.nan
        else:
            net_return_pct = (
                100.0
                * net_pnl
                / total_notional
            )

        net_profit_factor = (
            calculate_profit_factor(
                group["net_pnl_usd"]
            )
        )

        record.update(
            {
                "validation_total_trades": (
                    total_trades
                ),
                "validation_winning_trades": int(
                    group["winning_trade"].sum()
                ),
                "validation_win_rate_pct": (
                    100.0
                    * group[
                        "winning_trade"
                    ].mean()
                ),
                "validation_total_notional_usd": (
                    total_notional
                ),
                "validation_transaction_cost_usd": (
                    transaction_cost
                ),
                "validation_net_pnl_usd": (
                    net_pnl
                ),
                "validation_net_return_on_notional_pct": (
                    net_return_pct
                ),
                "validation_net_profit_factor": (
                    net_profit_factor
                ),
            }
        )

        records.append(record)

    selection = pd.DataFrame(records)

    selection[
        "passes_trade_count"
    ] = (
        selection[
            "validation_total_trades"
        ].ge(MIN_VALIDATION_TRADES)
    ).astype(int)

    selection[
        "passes_net_return"
    ] = (
        selection[
            "validation_net_return_on_notional_pct"
        ].gt(MIN_VALIDATION_NET_RETURN_PCT)
    ).astype(int)

    selection[
        "passes_profit_factor"
    ] = (
        selection[
            "validation_net_profit_factor"
        ].gt(MIN_VALIDATION_PROFIT_FACTOR)
    ).astype(int)

    selection["selected"] = (
        selection["passes_trade_count"].eq(1)
        & selection["passes_net_return"].eq(1)
        & selection[
            "passes_profit_factor"
        ].eq(1)
    ).astype(int)

    selection = (
        selection.sort_values(
            [
                "selected",
                "validation_net_return_on_notional_pct",
                "validation_net_profit_factor",
            ],
            ascending=[
                False,
                False,
                False,
            ],
        )
        .reset_index(drop=True)
    )

    if selection["selected"].sum() == 0:
        raise ValueError(
            "The validation selection rule "
            "selected no relationships."
        )

    return selection


def build_candidate_strategy(
    features: pd.DataFrame,
) -> pd.DataFrame:
    """
    Build the strongest candidate strategy branch:

    market + fundamentals + news
    divergence enabled
    signal-volatility sizing
    """
    signals = build_signals(
        features,
        use_sentiment=True,
        use_fundamentals=True,
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
            "Candidate strategy contains "
            f"{duplicate_count} duplicate "
            "relationship/date rows."
        )

    return sized_trades


def apply_universe_filter(
    sized_trades: pd.DataFrame,
    *,
    universe_mode: str,
    selected_relationship_ids: set[str],
) -> pd.DataFrame:
    if universe_mode == "all_relationships":
        result = sized_trades.copy()

    elif universe_mode == "validation_filtered":
        result = sized_trades.loc[
            sized_trades[
                "relationship_id"
            ].isin(selected_relationship_ids)
        ].copy()

    else:
        raise ValueError(
            f"Unknown universe mode: "
            f"{universe_mode}"
        )

    result = result.reset_index(
        drop=True
    )

    if result.empty:
        raise ValueError(
            f"{universe_mode}: no rows remain "
            "after universe filtering."
        )

    if universe_mode == "validation_filtered":
        unexpected_relationships = (
            set(
                result[
                    "relationship_id"
                ].unique()
            )
            - selected_relationship_ids
        )

        if unexpected_relationships:
            raise ValueError(
                "Filtered universe contains "
                "unexpected relationships: "
                f"{sorted(unexpected_relationships)}"
            )

    return result


def build_run_diagnostics(
    *,
    period_name: str,
    universe_mode: str,
    round_trip_cost_bps: float,
    sized_trades: pd.DataFrame,
    selected_relationship_count: int,
    total_relationship_count: int,
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "period": period_name,
                "universe_mode": universe_mode,
                "round_trip_cost_bps": (
                    round_trip_cost_bps
                ),
                "selection_source_period": (
                    "validation"
                ),
                "selection_source_cost_bps": (
                    2.0
                ),
                "minimum_validation_trades": (
                    MIN_VALIDATION_TRADES
                ),
                "minimum_validation_net_return_pct": (
                    MIN_VALIDATION_NET_RETURN_PCT
                ),
                "minimum_validation_profit_factor": (
                    MIN_VALIDATION_PROFIT_FACTOR
                ),
                "selected_relationship_count": (
                    selected_relationship_count
                ),
                "total_relationship_count": (
                    total_relationship_count
                ),
                "rows": len(sized_trades),
                "relationships_in_run": int(
                    sized_trades[
                        "relationship_id"
                    ].nunique()
                ),
                "trade_candidate_rows": int(
                    sized_trades[
                        "trade_candidate"
                    ].sum()
                ),
                "sized_position_rows": int(
                    sized_trades[
                        "has_position"
                    ].sum()
                ),
                "confirmed_divergence_rows": int(
                    sized_trades[
                        "primary_trade_rule"
                    ]
                    .eq(
                        "confirmed_divergence"
                    )
                    .sum()
                ),
            }
        ]
    )


def add_summary_metadata(
    summary: pd.DataFrame,
    *,
    period_name: str,
    universe_mode: str,
    effective_start: pd.Timestamp,
    effective_end: pd.Timestamp,
    selected_relationship_count: int,
    total_relationship_count: int,
) -> pd.DataFrame:
    metadata = pd.DataFrame(
        [
            {
                "period": period_name,
                "universe_mode": universe_mode,
                "effective_period_start": (
                    effective_start.date()
                ),
                "effective_period_end": (
                    effective_end.date()
                ),
                "strategy_variant": (
                    "market_fundamentals_news"
                ),
                "sizing_mode": (
                    "signal_volatility"
                ),
                "divergence_enabled": 1,
                "selection_source_period": (
                    "validation"
                ),
                "selection_source_cost_bps": (
                    2.0
                ),
                "selected_relationship_count": (
                    selected_relationship_count
                ),
                "total_relationship_count": (
                    total_relationship_count
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


def run_filter_case(
    *,
    period_name: str,
    universe_mode: str,
    round_trip_cost_bps: float,
    period_sized_trades: pd.DataFrame,
    period_fx_prices: pd.DataFrame,
    selected_relationship_count: int,
    total_relationship_count: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if period_sized_trades.empty:
        raise ValueError(
            f"{period_name}/{universe_mode}: "
            "no strategy rows."
        )

    if period_fx_prices.empty:
        raise ValueError(
            f"{period_name}: no FX rows."
        )

    effective_start = max(
        period_sized_trades[
            "date"
        ].min(),
        period_fx_prices[
            "date"
        ].min(),
    )

    effective_end = min(
        period_sized_trades[
            "date"
        ].max(),
        period_fx_prices[
            "date"
        ].max(),
    )

    print(
        f"\n{'=' * 76}\n"
        f"Period: {period_name}\n"
        f"Universe: {universe_mode}\n"
        f"Round-trip cost: "
        f"{round_trip_cost_bps:.1f} bps\n"
        f"Relationships: "
        f"{period_sized_trades['relationship_id'].nunique()}\n"
        f"Dates: {effective_start.date()} "
        f"through {effective_end.date()}\n"
        f"{'=' * 76}"
    )

    trades, equity, decisions = (
        run_event_backtest(
            period_sized_trades,
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
        universe_mode=universe_mode,
        effective_start=effective_start,
        effective_end=effective_end,
        selected_relationship_count=(
            selected_relationship_count
        ),
        total_relationship_count=(
            total_relationship_count
        ),
    )

    diagnostics = build_run_diagnostics(
        period_name=period_name,
        universe_mode=universe_mode,
        round_trip_cost_bps=(
            round_trip_cost_bps
        ),
        sized_trades=period_sized_trades,
        selected_relationship_count=(
            selected_relationship_count
        ),
        total_relationship_count=(
            total_relationship_count
        ),
    )

    reports = build_group_reports(
        trades
    )

    cost_folder = (
        f"{round_trip_cost_bps:g}_bps"
    )

    output_dir = (
        OUTPUT_ROOT
        / period_name
        / universe_mode
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
                "universe_mode",
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


def build_filter_deltas(
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

    all_relationships = summaries.loc[
        summaries["universe_mode"].eq(
            "all_relationships"
        ),
        key_columns + metric_columns,
    ].copy()

    filtered = summaries.loc[
        summaries["universe_mode"].eq(
            "validation_filtered"
        ),
        key_columns + metric_columns,
    ].copy()

    comparison = all_relationships.merge(
        filtered,
        on=key_columns,
        how="inner",
        validate="one_to_one",
        suffixes=(
            "_all_relationships",
            "_validation_filtered",
        ),
    )

    for metric in metric_columns:
        comparison[
            f"delta_{metric}"
        ] = (
            comparison[
                f"{metric}_validation_filtered"
            ]
            - comparison[
                f"{metric}_all_relationships"
            ]
        )

    comparison[
        "filter_improves_return"
    ] = (
        comparison[
            "delta_total_return_pct"
        ] > 0
    ).astype(int)

    comparison[
        "filter_improves_sharpe"
    ] = (
        comparison[
            "delta_sharpe_ratio"
        ] > 0
    ).astype(int)

    comparison[
        "filter_improves_drawdown"
    ] = (
        comparison[
            "delta_max_drawdown_pct"
        ] > 0
    ).astype(int)

    comparison[
        "filter_reduces_transaction_cost"
    ] = (
        comparison[
            "delta_total_transaction_cost_usd"
        ] < 0
    ).astype(int)

    return comparison


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

    selection = build_validation_selection(
        SELECTION_SOURCE_PATH
    )

    selected_relationships = (
        selection.loc[
            selection["selected"].eq(1),
            RELATIONSHIP_METADATA_COLUMNS,
        ]
        .copy()
        .reset_index(drop=True)
    )

    selected_relationship_ids = set(
        selected_relationships[
            "relationship_id"
        ].tolist()
    )

    candidate_strategy = (
        build_candidate_strategy(
            features
        )
    )

    total_relationship_count = int(
        candidate_strategy[
            "relationship_id"
        ].nunique()
    )

    selected_relationship_count = len(
        selected_relationship_ids
    )

    unavailable_relationships = (
        selected_relationship_ids
        - set(
            candidate_strategy[
                "relationship_id"
            ].unique()
        )
    )

    if unavailable_relationships:
        raise ValueError(
            "Selected relationships are missing "
            "from the candidate strategy: "
            f"{sorted(unavailable_relationships)}"
        )

    fx_prices = load_fx_prices()

    OUTPUT_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    selection.to_csv(
        OUTPUT_ROOT
        / "validation_relationship_selection.csv",
        index=False,
    )

    selected_relationships.to_csv(
        OUTPUT_ROOT
        / "selected_relationships.csv",
        index=False,
    )

    print(
        f"\nSelected "
        f"{selected_relationship_count} of "
        f"{total_relationship_count} "
        "relationships using validation only:"
    )

    print(
        selection.loc[
            selection["selected"].eq(1),
            [
                "relationship_id",
                "validation_total_trades",
                "validation_net_return_on_notional_pct",
                "validation_net_profit_factor",
            ],
        ].to_string(index=False)
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

        for universe_mode in UNIVERSE_MODES:
            universe_strategy = (
                apply_universe_filter(
                    period_strategy,
                    universe_mode=(
                        universe_mode
                    ),
                    selected_relationship_ids=(
                        selected_relationship_ids
                    ),
                )
            )

            for cost_bps in (
                ROUND_TRIP_COSTS_BPS
            ):
                (
                    summary,
                    diagnostic,
                ) = run_filter_case(
                    period_name=period_name,
                    universe_mode=universe_mode,
                    round_trip_cost_bps=(
                        cost_bps
                    ),
                    period_sized_trades=(
                        universe_strategy
                    ),
                    period_fx_prices=(
                        period_fx_prices
                    ),
                    selected_relationship_count=(
                        selected_relationship_count
                    ),
                    total_relationship_count=(
                        total_relationship_count
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

    filter_deltas = build_filter_deltas(
        comparison_summary
    )

    comparison_summary.to_csv(
        OUTPUT_ROOT
        / "relationship_filter_summary.csv",
        index=False,
    )

    comparison_diagnostics.to_csv(
        OUTPUT_ROOT
        / "relationship_filter_diagnostics.csv",
        index=False,
    )

    filter_deltas.to_csv(
        OUTPUT_ROOT
        / "relationship_filter_deltas.csv",
        index=False,
    )

    print(
        f"\n{'=' * 76}\n"
        "Relationship-filter ablation complete\n"
        f"{'=' * 76}"
    )

    display_columns = [
        "period",
        "universe_mode",
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
        "\nFilter deltas "
        "(validation-filtered minus all):"
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
        "filter_improves_return",
        "filter_improves_sharpe",
        "filter_improves_drawdown",
        "filter_reduces_transaction_cost",
    ]

    print(
        filter_deltas[
            delta_columns
        ].to_string(index=False)
    )

    print(
        "\nSaved outputs to:"
        f"\n{OUTPUT_ROOT}"
    )


if __name__ == "__main__":
    main()