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
    SIZING_MODES,
    VARIANTS,
    apply_experiment_sizing,
    build_variant_diagnostics,
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
    "divergence_ablation"
)


# Market-only setups cannot become confirmed-divergence trades,
# so they are excluded from this experiment.
ABLATION_VARIANTS = {
    "market_fundamentals": (
        VARIANTS["market_fundamentals"]
    ),
    "market_fundamentals_news": (
        VARIANTS[
            "market_fundamentals_news"
        ]
    ),
}


EVALUATION_PERIODS = {
    "validation": {
        "start": "2019-01-01",
        "end": "2022-12-31",
    },
    "test": {
        "start": "2023-01-01",
        "end": None,
    },
}


DIVERGENCE_SETTINGS = {
    "without_divergence": False,
    "with_divergence": True,
}


ROUND_TRIP_COSTS_BPS = [
    0.0,
    2.0,
]


def validate_ablation_output(
    *,
    variant_name: str,
    config: dict[str, bool],
    enable_divergence: bool,
    features: pd.DataFrame,
    signals: pd.DataFrame,
    divergence: pd.DataFrame,
    trade_rules: pd.DataFrame,
    sized_trades: pd.DataFrame,
    require_divergence_rows: bool,
) -> None:
    expected_rows = len(features)

    stage_frames = {
        "signals": signals,
        "divergence": divergence,
        "trade_rules": trade_rules,
        "sized_trades": sized_trades,
    }

    for stage_name, stage_df in (
        stage_frames.items()
    ):
        if len(stage_df) != expected_rows:
            raise ValueError(
                f"{variant_name}: {stage_name} "
                f"contains {len(stage_df)} rows; "
                f"expected {expected_rows}."
            )

        duplicate_count = (
            stage_df.duplicated(
                [
                    "relationship_id",
                    "date",
                ]
            ).sum()
        )

        if duplicate_count != 0:
            raise ValueError(
                f"{variant_name}: {stage_name} "
                f"contains {duplicate_count} "
                "duplicate relationship/date rows."
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

    divergence_entry_rows = int(
        trade_rules[
            "confirmed_divergence_entry"
        ].sum()
    )

    divergence_primary_rows = int(
        trade_rules[
            "primary_trade_rule"
        ]
        .eq("confirmed_divergence")
        .sum()
    )

    expected_rule_metadata = int(
        enable_divergence
    )

    if (
        "divergence_rule_enabled"
        in trade_rules.columns
        and not trade_rules[
            "divergence_rule_enabled"
        ].eq(expected_rule_metadata).all()
    ):
        raise ValueError(
            f"{variant_name}: incorrect "
            "divergence-rule metadata."
        )

    if not enable_divergence:
        if divergence_entry_rows != 0:
            raise ValueError(
                f"{variant_name}: divergence "
                "is disabled but "
                f"{divergence_entry_rows} "
                "divergence entries exist."
            )

        if divergence_primary_rows != 0:
            raise ValueError(
                f"{variant_name}: divergence "
                "is disabled but "
                f"{divergence_primary_rows} "
                "primary divergence rows exist."
            )

    elif (
        require_divergence_rows
        and divergence_primary_rows == 0
    ):
        raise ValueError(
            f"{variant_name}: divergence "
            "is enabled but no primary "
            "divergence rows were created."
        )


def build_full_ablation_variant(
    *,
    variant_name: str,
    config: dict[str, bool],
    sizing_mode: str,
    enable_divergence: bool,
    features: pd.DataFrame,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
]:
    """
    Build each strategy over the complete causal history
    before slicing validation and test periods.
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
        enable_divergence=(
            enable_divergence
        ),
    )

    sized_trades = build_position_sizes(
        trade_rules
    )

    sized_trades = apply_experiment_sizing(
        sized_trades,
        sizing_mode=sizing_mode,
    )

    validate_ablation_output(
        variant_name=variant_name,
        config=config,
        enable_divergence=(
            enable_divergence
        ),
        features=features,
        signals=signals,
        divergence=divergence,
        trade_rules=trade_rules,
        sized_trades=sized_trades,
        require_divergence_rows=True,
    )

    return (
        signals,
        divergence,
        trade_rules,
        sized_trades,
    )


def add_summary_metadata(
    summary: pd.DataFrame,
    *,
    period_name: str,
    period_start: str,
    period_end: str | None,
    effective_start: pd.Timestamp,
    effective_end: pd.Timestamp,
    variant_name: str,
    config: dict[str, bool],
    sizing_mode: str,
    divergence_mode: str,
    enable_divergence: bool,
) -> pd.DataFrame:
    summary = summary.copy()

    metadata = pd.DataFrame(
        [
            {
                "period": period_name,
                "requested_period_start": (
                    period_start
                ),
                "requested_period_end": (
                    period_end
                    if period_end is not None
                    else "latest"
                ),
                "effective_period_start": (
                    effective_start.date()
                ),
                "effective_period_end": (
                    effective_end.date()
                ),
                "variant": variant_name,
                "sizing_mode": sizing_mode,
                "divergence_mode": (
                    divergence_mode
                ),
                "divergence_enabled": int(
                    enable_divergence
                ),
                "uses_sentiment": int(
                    config["use_sentiment"]
                ),
                "uses_fundamentals": int(
                    config[
                        "use_fundamentals"
                    ]
                ),
                "uses_divergence": int(
                    enable_divergence
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


def build_ablation_diagnostics(
    *,
    period_name: str,
    effective_start: pd.Timestamp,
    effective_end: pd.Timestamp,
    variant_name: str,
    sizing_mode: str,
    divergence_mode: str,
    enable_divergence: bool,
    round_trip_cost_bps: float,
    signals: pd.DataFrame,
    trade_rules: pd.DataFrame,
    sized_trades: pd.DataFrame,
) -> pd.DataFrame:
    diagnostics = (
        build_variant_diagnostics(
            variant_name=variant_name,
            signals=signals,
            trade_rules=trade_rules,
            sized_trades=sized_trades,
        )
    )

    active_mask = sized_trades[
        "has_position"
    ].eq(1)

    if active_mask.any():
        mean_position_size_pct = float(
            sized_trades.loc[
                active_mask,
                "position_size_pct",
            ].mean()
        )
    else:
        mean_position_size_pct = 0.0

    extra = pd.DataFrame(
        [
            {
                "period": period_name,
                "effective_period_start": (
                    effective_start.date()
                ),
                "effective_period_end": (
                    effective_end.date()
                ),
                "sizing_mode": sizing_mode,
                "divergence_mode": (
                    divergence_mode
                ),
                "divergence_enabled": int(
                    enable_divergence
                ),
                "round_trip_cost_bps": (
                    round_trip_cost_bps
                ),
                "confirmed_divergence_entry_rows": int(
                    trade_rules[
                        "confirmed_divergence_entry"
                    ].sum()
                ),
                "mean_active_position_size_pct": (
                    mean_position_size_pct
                ),
            }
        ]
    )

    return pd.concat(
        [
            extra.reset_index(drop=True),
            diagnostics.reset_index(
                drop=True
            ),
        ],
        axis=1,
    )


def run_ablation_case(
    *,
    period_name: str,
    period_config: dict[str, str | None],
    variant_name: str,
    config: dict[str, bool],
    sizing_mode: str,
    divergence_mode: str,
    enable_divergence: bool,
    round_trip_cost_bps: float,
    period_features: pd.DataFrame,
    period_signals: pd.DataFrame,
    period_divergence: pd.DataFrame,
    period_trade_rules: pd.DataFrame,
    period_sized_trades: pd.DataFrame,
    period_fx_prices: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    validate_ablation_output(
        variant_name=variant_name,
        config=config,
        enable_divergence=(
            enable_divergence
        ),
        features=period_features,
        signals=period_signals,
        divergence=period_divergence,
        trade_rules=period_trade_rules,
        sized_trades=period_sized_trades,
        require_divergence_rows=False,
    )

    if period_features.empty:
        raise ValueError(
            f"{period_name}: no feature rows."
        )

    if period_fx_prices.empty:
        raise ValueError(
            f"{period_name}: no FX prices."
        )

    effective_start = max(
        period_features["date"].min(),
        period_fx_prices["date"].min(),
    )

    effective_end = min(
        period_features["date"].max(),
        period_fx_prices["date"].max(),
    )

    print(
        f"\n{'=' * 76}\n"
        f"Period: {period_name}\n"
        f"Variant: {variant_name}\n"
        f"Sizing: {sizing_mode}\n"
        f"Divergence: {divergence_mode}\n"
        f"Round-trip cost: "
        f"{round_trip_cost_bps:.1f} bps\n"
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
        period_start=(
            period_config["start"]
        ),
        period_end=(
            period_config["end"]
        ),
        effective_start=effective_start,
        effective_end=effective_end,
        variant_name=variant_name,
        config=config,
        sizing_mode=sizing_mode,
        divergence_mode=divergence_mode,
        enable_divergence=(
            enable_divergence
        ),
    )

    diagnostics = (
        build_ablation_diagnostics(
            period_name=period_name,
            effective_start=effective_start,
            effective_end=effective_end,
            variant_name=variant_name,
            sizing_mode=sizing_mode,
            divergence_mode=(
                divergence_mode
            ),
            enable_divergence=(
                enable_divergence
            ),
            round_trip_cost_bps=(
                round_trip_cost_bps
            ),
            signals=period_signals,
            trade_rules=period_trade_rules,
            sized_trades=period_sized_trades,
        )
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
        / sizing_mode
        / variant_name
        / divergence_mode
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
                "sizing_mode",
                "variant",
                "divergence_mode",
                "round_trip_cost_bps",
                "total_return_pct",
                "annualized_return_pct",
                "sharpe_ratio",
                "max_drawdown_pct",
                "profit_factor",
                "total_trades",
                "total_transaction_cost_usd",
            ]
        ].to_string(index=False)
    )

    return summary, diagnostics


def build_ablation_deltas(
    summaries: pd.DataFrame,
) -> pd.DataFrame:
    key_columns = [
        "period",
        "sizing_mode",
        "variant",
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

    without_divergence = summaries.loc[
        summaries["divergence_mode"].eq(
            "without_divergence"
        ),
        key_columns + metric_columns,
    ].copy()

    with_divergence = summaries.loc[
        summaries["divergence_mode"].eq(
            "with_divergence"
        ),
        key_columns + metric_columns,
    ].copy()

    comparison = without_divergence.merge(
        with_divergence,
        on=key_columns,
        how="inner",
        validate="one_to_one",
        suffixes=(
            "_without_divergence",
            "_with_divergence",
        ),
    )

    for metric in metric_columns:
        comparison[
            f"delta_{metric}"
        ] = (
            comparison[
                f"{metric}_with_divergence"
            ]
            - comparison[
                f"{metric}_without_divergence"
            ]
        )

    comparison[
        "divergence_improves_return"
    ] = (
        comparison[
            "delta_total_return_pct"
        ] > 0
    ).astype(int)

    comparison[
        "divergence_improves_sharpe"
    ] = (
        comparison[
            "delta_sharpe_ratio"
        ] > 0
    ).astype(int)

    comparison[
        "divergence_improves_drawdown"
    ] = (
        comparison[
            "delta_max_drawdown_pct"
        ] > 0
    ).astype(int)

    return comparison


def main() -> None:
    if not FEATURES_PATH.exists():
        raise FileNotFoundError(
            f"Missing input file: "
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

    summaries: list[pd.DataFrame] = []
    diagnostics: list[pd.DataFrame] = []

    for sizing_mode in SIZING_MODES:
        for (
            variant_name,
            config,
        ) in ABLATION_VARIANTS.items():
            for (
                divergence_mode,
                enable_divergence,
            ) in DIVERGENCE_SETTINGS.items():
                (
                    signals,
                    divergence,
                    trade_rules,
                    sized_trades,
                ) = build_full_ablation_variant(
                    variant_name=variant_name,
                    config=config,
                    sizing_mode=sizing_mode,
                    enable_divergence=(
                        enable_divergence
                    ),
                    features=features,
                )

                for (
                    period_name,
                    period_config,
                ) in EVALUATION_PERIODS.items():
                    start_date = (
                        period_config["start"]
                    )

                    end_date = (
                        period_config["end"]
                    )

                    period_features = (
                        slice_period(
                            features,
                            start_date=start_date,
                            end_date=end_date,
                        )
                    )

                    period_signals = (
                        slice_period(
                            signals,
                            start_date=start_date,
                            end_date=end_date,
                        )
                    )

                    period_divergence = (
                        slice_period(
                            divergence,
                            start_date=start_date,
                            end_date=end_date,
                        )
                    )

                    period_trade_rules = (
                        slice_period(
                            trade_rules,
                            start_date=start_date,
                            end_date=end_date,
                        )
                    )

                    period_sized_trades = (
                        slice_period(
                            sized_trades,
                            start_date=start_date,
                            end_date=end_date,
                        )
                    )

                    period_fx_prices = (
                        slice_period(
                            fx_prices,
                            start_date=start_date,
                            end_date=end_date,
                        )
                    )

                    for cost_bps in (
                        ROUND_TRIP_COSTS_BPS
                    ):
                        (
                            summary,
                            diagnostic,
                        ) = run_ablation_case(
                            period_name=(
                                period_name
                            ),
                            period_config=(
                                period_config
                            ),
                            variant_name=(
                                variant_name
                            ),
                            config=config,
                            sizing_mode=(
                                sizing_mode
                            ),
                            divergence_mode=(
                                divergence_mode
                            ),
                            enable_divergence=(
                                enable_divergence
                            ),
                            round_trip_cost_bps=(
                                cost_bps
                            ),
                            period_features=(
                                period_features
                            ),
                            period_signals=(
                                period_signals
                            ),
                            period_divergence=(
                                period_divergence
                            ),
                            period_trade_rules=(
                                period_trade_rules
                            ),
                            period_sized_trades=(
                                period_sized_trades
                            ),
                            period_fx_prices=(
                                period_fx_prices
                            ),
                        )

                        summaries.append(
                            summary
                        )

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

    ablation_deltas = (
        build_ablation_deltas(
            comparison_summary
        )
    )

    comparison_summary.to_csv(
        OUTPUT_ROOT
        / "divergence_ablation_summary.csv",
        index=False,
    )

    comparison_diagnostics.to_csv(
        OUTPUT_ROOT
        / "divergence_ablation_diagnostics.csv",
        index=False,
    )

    ablation_deltas.to_csv(
        OUTPUT_ROOT
        / "divergence_ablation_deltas.csv",
        index=False,
    )

    print(
        f"\n{'=' * 76}\n"
        "Divergence ablation complete\n"
        f"{'=' * 76}"
    )

    display_columns = [
        "period",
        "sizing_mode",
        "variant",
        "divergence_mode",
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
        ].to_string(index=False)
    )

    print(
        "\nDivergence deltas "
        "(with minus without):"
    )

    delta_columns = [
        "period",
        "sizing_mode",
        "variant",
        "round_trip_cost_bps",
        "delta_total_return_pct",
        "delta_sharpe_ratio",
        "delta_max_drawdown_pct",
        "delta_profit_factor",
        "delta_total_trades",
        "delta_total_transaction_cost_usd",
        "divergence_improves_return",
        "divergence_improves_sharpe",
        "divergence_improves_drawdown",
    ]

    print(
        ablation_deltas[
            delta_columns
        ].to_string(index=False)
    )

    print(
        "\nSaved outputs to:"
        f"\n{OUTPUT_ROOT}"
    )


if __name__ == "__main__":
    main()