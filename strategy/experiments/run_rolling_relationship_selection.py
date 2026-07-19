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

OUTPUT_ROOT = Path(
    "strategy/output/experiments/"
    "rolling_relationship_selection"
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
    "rolling_hard_filter",
    "rolling_soft_weight",
]


ROUND_TRIP_COSTS_BPS = [
    0.0,
    2.0,
]


# Selection metrics are always calculated after this
# realistic round-trip transaction-cost assumption.
SELECTION_COST_BPS = 2.0

LOOKBACK_YEARS = 2

MIN_TRAILING_TRADES = 20

MIN_TRAILING_NET_RETURN_PCT = 0.0

MIN_TRAILING_PROFIT_FACTOR = 1.0

WEAK_RELATIONSHIP_WEIGHT = 0.50


RELATIONSHIP_COLUMNS = [
    "relationship_id",
    "commodity",
    "currency",
    "fx_symbol",
    "relationship_type",
]


SIZE_COLUMNS = [
    "position_size_pct",
    "position_size_usd",
    "signed_position_pct",
    "signed_position_usd",
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


def build_candidate_strategy(
    features: pd.DataFrame,
) -> pd.DataFrame:
    """
    Build the current strongest candidate branch:

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
            "Candidate strategy contains "
            f"{duplicate_count} duplicate "
            "relationship/date rows."
        )

    missing_size_columns = sorted(
        set(SIZE_COLUMNS)
        - set(sized_trades.columns)
    )

    if missing_size_columns:
        raise ValueError(
            "Candidate strategy is missing "
            f"sizing columns: "
            f"{missing_size_columns}"
        )

    return sized_trades


def run_selection_history(
    candidate_strategy: pd.DataFrame,
    fx_prices: pd.DataFrame,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
]:
    """
    Run one fixed all-relationship reference strategy.

    Its completed trades are used to create every yearly
    relationship-selection schedule. This prevents the
    selection history from changing across universe modes.
    """
    print(
        f"\n{'=' * 78}\n"
        "Building causal relationship-selection history\n"
        f"Round-trip cost: "
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
            "Selection-history backtest "
            "produced no completed trades."
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


def summarize_trailing_relationships(
    history_trades: pd.DataFrame,
    *,
    selection_year: int,
    relationship_metadata: pd.DataFrame,
) -> pd.DataFrame:
    year_start = pd.Timestamp(
        year=selection_year,
        month=1,
        day=1,
    )

    lookback_start = (
        year_start
        - pd.DateOffset(
            years=LOOKBACK_YEARS
        )
    )

    # Use only trades completed before the upcoming year.
    trailing = history_trades.loc[
        history_trades["exit_date"].ge(
            lookback_start
        )
        & history_trades["exit_date"].lt(
            year_start
        )
    ].copy()

    records: list[dict] = []

    grouped = trailing.groupby(
        RELATIONSHIP_COLUMNS,
        dropna=False,
        sort=True,
    )

    for keys, group in grouped:
        record = dict(
            zip(
                RELATIONSHIP_COLUMNS,
                keys,
            )
        )

        total_notional = float(
            group["notional_usd"]
            .abs()
            .sum()
        )

        net_pnl = float(
            group["net_pnl_usd"].sum()
        )

        if total_notional == 0:
            net_return_pct = np.nan
        else:
            net_return_pct = (
                100.0
                * net_pnl
                / total_notional
            )

        record.update(
            {
                "selection_year": (
                    selection_year
                ),
                "lookback_start": (
                    lookback_start.date()
                ),
                "lookback_end": (
                    (
                        year_start
                        - pd.Timedelta(days=1)
                    ).date()
                ),
                "trailing_trades": len(group),
                "trailing_winning_trades": int(
                    group["winning_trade"].sum()
                ),
                "trailing_win_rate_pct": (
                    100.0
                    * group[
                        "winning_trade"
                    ].mean()
                ),
                "trailing_total_notional_usd": (
                    total_notional
                ),
                "trailing_gross_pnl_usd": float(
                    group[
                        "gross_pnl_usd"
                    ].sum()
                ),
                "trailing_transaction_cost_usd": float(
                    group[
                        "transaction_cost_usd"
                    ].sum()
                ),
                "trailing_net_pnl_usd": (
                    net_pnl
                ),
                "trailing_net_return_on_notional_pct": (
                    net_return_pct
                ),
                "trailing_net_profit_factor": (
                    calculate_profit_factor(
                        group["net_pnl_usd"]
                    )
                ),
            }
        )

        records.append(record)

    if records:
        metrics = pd.DataFrame(records)
    else:
        metrics = pd.DataFrame(
            columns=(
                RELATIONSHIP_COLUMNS
                + [
                    "selection_year",
                    "lookback_start",
                    "lookback_end",
                    "trailing_trades",
                    "trailing_winning_trades",
                    "trailing_win_rate_pct",
                    "trailing_total_notional_usd",
                    "trailing_gross_pnl_usd",
                    "trailing_transaction_cost_usd",
                    "trailing_net_pnl_usd",
                    "trailing_net_return_on_notional_pct",
                    "trailing_net_profit_factor",
                ]
            )
        )

    schedule = relationship_metadata.copy()

    schedule["selection_year"] = (
        selection_year
    )

    schedule["lookback_start"] = (
        lookback_start.date()
    )

    schedule["lookback_end"] = (
        (
            year_start
            - pd.Timedelta(days=1)
        ).date()
    )

    metric_columns = [
        "selection_year",
        "relationship_id",
        "trailing_trades",
        "trailing_winning_trades",
        "trailing_win_rate_pct",
        "trailing_total_notional_usd",
        "trailing_gross_pnl_usd",
        "trailing_transaction_cost_usd",
        "trailing_net_pnl_usd",
        "trailing_net_return_on_notional_pct",
        "trailing_net_profit_factor",
    ]

    schedule = schedule.merge(
        metrics[metric_columns],
        on=[
            "selection_year",
            "relationship_id",
        ],
        how="left",
        validate="one_to_one",
    )

    zero_fill_columns = [
        "trailing_trades",
        "trailing_winning_trades",
        "trailing_win_rate_pct",
        "trailing_total_notional_usd",
        "trailing_gross_pnl_usd",
        "trailing_transaction_cost_usd",
        "trailing_net_pnl_usd",
        "trailing_net_return_on_notional_pct",
        "trailing_net_profit_factor",
    ]

    schedule[zero_fill_columns] = (
        schedule[zero_fill_columns]
        .fillna(0.0)
    )

    schedule["passes_trade_count"] = (
        schedule["trailing_trades"].ge(
            MIN_TRAILING_TRADES
        )
    ).astype(int)

    schedule["passes_net_return"] = (
        schedule[
            "trailing_net_return_on_notional_pct"
        ].gt(
            MIN_TRAILING_NET_RETURN_PCT
        )
    ).astype(int)

    schedule["passes_profit_factor"] = (
        schedule[
            "trailing_net_profit_factor"
        ].gt(
            MIN_TRAILING_PROFIT_FACTOR
        )
    ).astype(int)

    schedule["selected"] = (
        schedule["passes_trade_count"].eq(1)
        & schedule["passes_net_return"].eq(1)
        & schedule[
            "passes_profit_factor"
        ].eq(1)
    ).astype(int)

    schedule[
        "all_relationships_weight"
    ] = 1.0

    schedule[
        "rolling_hard_filter_weight"
    ] = schedule["selected"].astype(float)

    schedule[
        "rolling_soft_weight_weight"
    ] = np.where(
        schedule["selected"].eq(1),
        1.0,
        WEAK_RELATIONSHIP_WEIGHT,
    )

    return schedule


def build_rolling_selection_schedule(
    history_trades: pd.DataFrame,
    candidate_strategy: pd.DataFrame,
) -> pd.DataFrame:
    relationship_metadata = (
        candidate_strategy[
            RELATIONSHIP_COLUMNS
        ]
        .drop_duplicates(
            subset=["relationship_id"]
        )
        .sort_values("relationship_id")
        .reset_index(drop=True)
    )

    latest_year = int(
        candidate_strategy["date"].dt.year.max()
    )

    selection_years = range(
        2019,
        latest_year + 1,
    )

    schedules = []

    for selection_year in selection_years:
        schedule = (
            summarize_trailing_relationships(
                history_trades,
                selection_year=selection_year,
                relationship_metadata=(
                    relationship_metadata
                ),
            )
        )

        schedules.append(schedule)

    combined = pd.concat(
        schedules,
        ignore_index=True,
    )

    duplicate_count = combined.duplicated(
        [
            "selection_year",
            "relationship_id",
        ]
    ).sum()

    if duplicate_count != 0:
        raise ValueError(
            "Rolling selection schedule "
            f"contains {duplicate_count} duplicate "
            "year/relationship rows."
        )

    return combined


def apply_rolling_selection(
    period_strategy: pd.DataFrame,
    *,
    universe_mode: str,
    selection_schedule: pd.DataFrame,
) -> pd.DataFrame:
    result = period_strategy.copy()

    result["selection_year"] = (
        result["date"].dt.year
    )

    selection_columns = [
        "selection_year",
        "relationship_id",
        "trailing_trades",
        "trailing_net_return_on_notional_pct",
        "trailing_net_profit_factor",
        "selected",
        "all_relationships_weight",
        "rolling_hard_filter_weight",
        "rolling_soft_weight_weight",
    ]

    result = result.merge(
        selection_schedule[
            selection_columns
        ],
        on=[
            "selection_year",
            "relationship_id",
        ],
        how="left",
        validate="many_to_one",
    )

    missing_schedule_rows = int(
        result["selected"].isna().sum()
    )

    if missing_schedule_rows != 0:
        raise ValueError(
            f"{universe_mode}: "
            f"{missing_schedule_rows} strategy "
            "rows have no yearly selection record."
        )

    weight_column_map = {
        "all_relationships": (
            "all_relationships_weight"
        ),
        "rolling_hard_filter": (
            "rolling_hard_filter_weight"
        ),
        "rolling_soft_weight": (
            "rolling_soft_weight_weight"
        ),
    }

    if universe_mode not in weight_column_map:
        raise ValueError(
            f"Unknown universe mode: "
            f"{universe_mode}"
        )

    weight_column = weight_column_map[
        universe_mode
    ]

    result["selection_weight"] = (
        result[weight_column].astype(float)
    )

    for column in SIZE_COLUMNS:
        result[
            f"base_{column}"
        ] = result[column]

        result[column] = (
            result[column]
            * result["selection_weight"]
        )

    result["has_position"] = (
        result["has_position"].eq(1)
        & result["selection_weight"].gt(0)
        & result["position_size_pct"].gt(0)
    ).astype(int)

    zero_position_mask = (
        result["has_position"].eq(0)
    )

    result.loc[
        zero_position_mask,
        SIZE_COLUMNS,
    ] = 0.0

    return result


def add_summary_metadata(
    summary: pd.DataFrame,
    *,
    period_name: str,
    universe_mode: str,
    effective_start: pd.Timestamp,
    effective_end: pd.Timestamp,
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
            metadata.reset_index(drop=True),
            summary.reset_index(drop=True),
        ],
        axis=1,
    )


def build_run_diagnostics(
    *,
    period_name: str,
    universe_mode: str,
    round_trip_cost_bps: float,
    weighted_strategy: pd.DataFrame,
) -> pd.DataFrame:
    yearly = (
        weighted_strategy.groupby(
            "selection_year"
        )
        .agg(
            qualified_relationships=(
                "selected",
                lambda series: int(
                    weighted_strategy.loc[
                        series.index
                    ]
                    .loc[
                        lambda frame:
                        frame["selected"].eq(1)
                    ][
                        "relationship_id"
                    ]
                    .nunique()
                ),
            ),
            traded_relationships=(
                "relationship_id",
                lambda series: int(
                    weighted_strategy.loc[
                        series.index
                    ]
                    .loc[
                        lambda frame:
                        frame["has_position"].eq(1)
                    ][
                        "relationship_id"
                    ]
                    .nunique()
                ),
            ),
        )
        .reset_index()
    )

    return pd.DataFrame(
        [
            {
                "period": period_name,
                "universe_mode": universe_mode,
                "round_trip_cost_bps": (
                    round_trip_cost_bps
                ),
                "rows": len(weighted_strategy),
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


def run_period_case(
    *,
    period_name: str,
    universe_mode: str,
    round_trip_cost_bps: float,
    weighted_strategy: pd.DataFrame,
    period_fx_prices: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if weighted_strategy.empty:
        raise ValueError(
            f"{period_name}/{universe_mode}: "
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
        f"Universe mode: {universe_mode}\n"
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
        universe_mode=universe_mode,
        effective_start=effective_start,
        effective_end=effective_end,
    )

    diagnostics = build_run_diagnostics(
        period_name=period_name,
        universe_mode=universe_mode,
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


def build_selection_counts(
    selection_schedule: pd.DataFrame,
) -> pd.DataFrame:
    return (
        selection_schedule.groupby(
            "selection_year"
        )
        .agg(
            total_relationships=(
                "relationship_id",
                "nunique",
            ),
            selected_relationships=(
                "selected",
                "sum",
            ),
            average_trailing_trades=(
                "trailing_trades",
                "mean",
            ),
            median_trailing_net_return_pct=(
                "trailing_net_return_on_notional_pct",
                "median",
            ),
            median_trailing_profit_factor=(
                "trailing_net_profit_factor",
                "median",
            ),
        )
        .reset_index()
    )


def build_mode_deltas(
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

    benchmark = summaries.loc[
        summaries["universe_mode"].eq(
            "all_relationships"
        ),
        key_columns + metric_columns,
    ].copy()

    comparisons = []

    for comparison_mode in [
        "rolling_hard_filter",
        "rolling_soft_weight",
    ]:
        selected = summaries.loc[
            summaries["universe_mode"].eq(
                comparison_mode
            ),
            key_columns + metric_columns,
        ].copy()

        comparison = benchmark.merge(
            selected,
            on=key_columns,
            how="inner",
            validate="one_to_one",
            suffixes=(
                "_all_relationships",
                f"_{comparison_mode}",
            ),
        )

        comparison.insert(
            2,
            "comparison_mode",
            comparison_mode,
        )

        for metric in metric_columns:
            comparison[
                f"delta_{metric}"
            ] = (
                comparison[
                    f"{metric}_{comparison_mode}"
                ]
                - comparison[
                    f"{metric}_all_relationships"
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
            "reduces_transaction_cost"
        ] = (
            comparison[
                "delta_total_transaction_cost_usd"
            ] < 0
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

    candidate_strategy = (
        build_candidate_strategy(
            features
        )
    )

    fx_prices = load_fx_prices()

    (
        selection_history_trades,
        selection_history_equity,
        selection_history_decisions,
    ) = run_selection_history(
        candidate_strategy,
        fx_prices,
    )

    selection_schedule = (
        build_rolling_selection_schedule(
            selection_history_trades,
            candidate_strategy,
        )
    )

    selection_counts = (
        build_selection_counts(
            selection_schedule
        )
    )

    OUTPUT_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    selection_history_dir = (
        OUTPUT_ROOT
        / "selection_history"
    )

    selection_history_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    selection_history_trades.to_csv(
        selection_history_dir
        / "trades_2bps.csv",
        index=False,
    )

    selection_history_equity.to_csv(
        selection_history_dir
        / "equity_curve_2bps.csv",
        index=False,
    )

    selection_history_decisions.to_csv(
        selection_history_dir
        / "entry_decisions_2bps.csv",
        index=False,
    )

    selection_schedule.to_csv(
        OUTPUT_ROOT
        / "rolling_selection_schedule.csv",
        index=False,
    )

    selection_counts.to_csv(
        OUTPUT_ROOT
        / "rolling_selection_counts.csv",
        index=False,
    )

    selected_rows = selection_schedule.loc[
        selection_schedule["selected"].eq(1)
    ].copy()

    selected_rows.to_csv(
        OUTPUT_ROOT
        / "selected_relationships_by_year.csv",
        index=False,
    )

    print(
        "\nAnnual relationship-selection counts:"
    )

    print(
        selection_counts.to_string(
            index=False
        )
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
            weighted_strategy = (
                apply_rolling_selection(
                    period_strategy,
                    universe_mode=(
                        universe_mode
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
                ) = run_period_case(
                    period_name=period_name,
                    universe_mode=universe_mode,
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

    mode_deltas = build_mode_deltas(
        comparison_summary
    )

    comparison_summary.to_csv(
        OUTPUT_ROOT
        / "rolling_selection_summary.csv",
        index=False,
    )

    comparison_diagnostics.to_csv(
        OUTPUT_ROOT
        / "rolling_selection_diagnostics.csv",
        index=False,
    )

    mode_deltas.to_csv(
        OUTPUT_ROOT
        / "rolling_selection_deltas.csv",
        index=False,
    )

    print(
        f"\n{'=' * 78}\n"
        "Rolling relationship selection complete\n"
        f"{'=' * 78}"
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
        "\nRolling-selection deltas "
        "(selection mode minus all relationships):"
    )

    delta_columns = [
        "period",
        "round_trip_cost_bps",
        "comparison_mode",
        "delta_total_return_pct",
        "delta_sharpe_ratio",
        "delta_max_drawdown_pct",
        "delta_profit_factor",
        "delta_total_trades",
        "delta_total_transaction_cost_usd",
        "improves_return",
        "improves_sharpe",
        "improves_drawdown",
        "reduces_transaction_cost",
    ]

    print(
        mode_deltas[
            delta_columns
        ].to_string(index=False)
    )

    print(
        "\nSaved outputs to:"
        f"\n{OUTPUT_ROOT}"
    )


if __name__ == "__main__":
    main()