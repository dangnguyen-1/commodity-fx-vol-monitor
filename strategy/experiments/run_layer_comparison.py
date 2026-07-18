from pathlib import Path

import pandas as pd

from strategy.backtest.run_backtest import (
    INITIAL_CAPITAL,
    build_group_reports,
    calculate_summary,
    load_fx_prices,
    run_event_backtest,
)
from strategy.divergence.build_divergence import build_divergence
from strategy.rules.build_trade_rules import build_trade_rules
from strategy.signals.build_signals import build_signals
from strategy.sizing.build_position_sizes import build_position_sizes


FEATURES_PATH = Path(
    "strategy/output/daily_features.csv"
)

OUTPUT_ROOT = Path(
    "strategy/output/experiments/layer_comparison"
)


VARIANTS = {
    "market_only": {
        "use_sentiment": False,
        "use_fundamentals": False,
    },
    "market_fundamentals": {
        "use_sentiment": False,
        "use_fundamentals": True,
    },
    "market_fundamentals_news": {
        "use_sentiment": True,
        "use_fundamentals": True,
    },
}


SIZING_MODES = [
    "equal_weight",
    "signal_volatility",
]

EQUAL_POSITION_PCT = 0.01


def apply_experiment_sizing(
    sized_trades: pd.DataFrame,
    sizing_mode: str,
) -> pd.DataFrame:
    df = sized_trades.copy()

    if sizing_mode == "signal_volatility":
        return df

    if sizing_mode != "equal_weight":
        raise ValueError(
            f"Unknown sizing mode: {sizing_mode}"
        )

    trade_mask = (
        df["trade_candidate"].eq(1)
        & df["trade_direction"].isin([-1, 1])
    )

    # Every valid candidate receives the same percentage of current equity.
    # run_event_backtest() converts this percentage into current-dollar
    # notional when the entry is considered.
    df["has_position"] = trade_mask.astype(int)

    df["position_size_pct"] = (
        trade_mask.astype(float)
        * EQUAL_POSITION_PCT
    )

    # These dollar fields are diagnostics only. The backtester uses
    # position_size_pct and current portfolio equity.
    df["position_size_usd"] = (
        df["position_size_pct"]
        * INITIAL_CAPITAL
    )

    df["signed_position_pct"] = (
        df["trade_direction"]
        * df["position_size_pct"]
    )

    df["signed_position_usd"] = (
        df["trade_direction"]
        * df["position_size_usd"]
    )

    return df


def validate_variant_output(
    variant_name: str,
    features: pd.DataFrame,
    signals: pd.DataFrame,
    divergence: pd.DataFrame,
    trade_rules: pd.DataFrame,
    sized_trades: pd.DataFrame,
) -> None:
    expected_rows = len(features)

    stage_frames = {
        "signals": signals,
        "divergence": divergence,
        "trade_rules": trade_rules,
        "sized_trades": sized_trades,
    }

    for stage_name, stage_df in stage_frames.items():
        if len(stage_df) != expected_rows:
            raise ValueError(
                f"{variant_name}: {stage_name} has "
                f"{len(stage_df)} rows; expected {expected_rows}."
            )

        duplicate_count = stage_df.duplicated(
            ["relationship_id", "date"]
        ).sum()

        if duplicate_count != 0:
            raise ValueError(
                f"{variant_name}: {stage_name} contains "
                f"{duplicate_count} duplicate relationship/date rows."
            )

    divergence_entries = int(
        trade_rules[
            "confirmed_divergence_entry"
        ].sum()
    )

    if divergence_entries != 0:
        raise ValueError(
            f"{variant_name}: divergence should be disabled, "
            f"but {divergence_entries} divergence entries exist."
        )

    divergence_primary = int(
        trade_rules[
            "primary_trade_rule"
        ]
        .eq("confirmed_divergence")
        .sum()
    )

    if divergence_primary != 0:
        raise ValueError(
            f"{variant_name}: found {divergence_primary} "
            "primary divergence trades."
        )

    expected_sentiment = int(
        VARIANTS[variant_name]["use_sentiment"]
    )

    expected_fundamentals = int(
        VARIANTS[variant_name]["use_fundamentals"]
    )

    if not signals[
        "uses_sentiment_layer"
    ].eq(expected_sentiment).all():
        raise ValueError(
            f"{variant_name}: incorrect sentiment metadata."
        )

    if not signals[
        "uses_fundamental_layer"
    ].eq(expected_fundamentals).all():
        raise ValueError(
            f"{variant_name}: incorrect fundamental metadata."
        )

    if not VARIANTS[variant_name]["use_sentiment"]:
        nonzero_sentiment = int(
            signals[
                "sentiment_layer_score"
            ]
            .ne(0)
            .sum()
        )

        if nonzero_sentiment != 0:
            raise ValueError(
                f"{variant_name}: disabled sentiment layer "
                f"has {nonzero_sentiment} nonzero rows."
            )

    if not VARIANTS[variant_name]["use_fundamentals"]:
        nonzero_fundamentals = int(
            signals[
                "fundamental_layer_score"
            ]
            .ne(0)
            .sum()
        )

        if nonzero_fundamentals != 0:
            raise ValueError(
                f"{variant_name}: disabled fundamental layer "
                f"has {nonzero_fundamentals} nonzero rows."
            )


def build_variant_diagnostics(
    variant_name: str,
    signals: pd.DataFrame,
    trade_rules: pd.DataFrame,
    sized_trades: pd.DataFrame,
) -> pd.DataFrame:
    primary_counts = (
        trade_rules[
            "primary_trade_rule"
        ]
        .value_counts()
    )

    return pd.DataFrame(
        [
            {
                "variant": variant_name,
                "rows": len(signals),
                "relationships": (
                    signals[
                        "relationship_id"
                    ].nunique()
                ),
                "uses_sentiment": int(
                    VARIANTS[
                        variant_name
                    ]["use_sentiment"]
                ),
                "uses_fundamentals": int(
                    VARIANTS[
                        variant_name
                    ]["use_fundamentals"]
                ),
                "max_layers_triggered": int(
                    signals[
                        "layers_triggered"
                    ].max()
                ),
                "confirmed_setup_rows": int(
                    signals[
                        "is_confirmed_setup"
                    ].sum()
                ),
                "baseline_primary_rows": int(
                    primary_counts.get(
                        "baseline",
                        0,
                    )
                ),
                "confirmed_primary_rows": int(
                    primary_counts.get(
                        "confirmed",
                        0,
                    )
                ),
                "divergence_primary_rows": int(
                    primary_counts.get(
                        "confirmed_divergence",
                        0,
                    )
                ),
                "no_trade_rows": int(
                    primary_counts.get(
                        "no_trade",
                        0,
                    )
                ),
                "trade_candidate_rows": int(
                    trade_rules[
                        "trade_candidate"
                    ].sum()
                ),
                "sized_position_rows": int(
                    sized_trades[
                        "has_position"
                    ].sum()
                ),
            }
        ]
    )


def save_variant_outputs(
    output_dir: Path,
    trades: pd.DataFrame,
    equity: pd.DataFrame,
    decisions: pd.DataFrame,
    summary: pd.DataFrame,
    diagnostics: pd.DataFrame,
    reports: dict[str, pd.DataFrame],
) -> None:
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    trades.to_csv(
        output_dir / "trades.csv",
        index=False,
    )

    equity.to_csv(
        output_dir / "equity_curve.csv",
        index=False,
    )

    decisions.to_csv(
        output_dir / "entry_decisions.csv",
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

    for report_name, report in reports.items():
        report.to_csv(
            output_dir / f"{report_name}.csv",
            index=False,
        )


def run_variant(
    variant_name: str,
    config: dict[str, bool],
    sizing_mode: str,
    features: pd.DataFrame,
    fx_prices: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    print(
        f"\n{'=' * 70}\n"
        f"Running variant: {variant_name}\n"
        f"Sizing mode: {sizing_mode}\n"
        f"{'=' * 70}"
    )

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
        enable_divergence=False,
    )

    sized_trades = build_position_sizes(
        trade_rules
    )
    
    sized_trades = apply_experiment_sizing(
        sized_trades,
        sizing_mode=sizing_mode,
    )

    validate_variant_output(
        variant_name=variant_name,
        features=features,
        signals=signals,
        divergence=divergence,
        trade_rules=trade_rules,
        sized_trades=sized_trades,
    )

    trades, equity, decisions = (
        run_event_backtest(
            sized_trades,
            fx_prices,
        )
    )

    summary = calculate_summary(
        trades,
        equity,
        decisions,
    )

    summary.insert(
        0,
        "variant",
        variant_name,
    )

    summary.insert(
        1,
        "sizing_mode",
        sizing_mode,
    )

    summary.insert(
        2,
        "uses_sentiment",
        int(config["use_sentiment"]),
    )

    summary.insert(
        3,
        "uses_fundamentals",
        int(config["use_fundamentals"]),
    )

    summary.insert(
        4,
        "uses_divergence",
        0,
    )

    diagnostics = build_variant_diagnostics(
        variant_name=variant_name,
        signals=signals,
        trade_rules=trade_rules,
        sized_trades=sized_trades,
    )

    reports = build_group_reports(
        trades
    )

    save_variant_outputs(
        output_dir=(
            OUTPUT_ROOT
            / sizing_mode
            / variant_name
        ),
        trades=trades,
        equity=equity,
        decisions=decisions,
        summary=summary,
        diagnostics=diagnostics,
        reports=reports,
    )

    print("\nSignal diagnostics:")
    print(
        diagnostics.T.to_string(
            header=False
        )
    )

    print("\nBacktest summary:")
    print(
        summary[
            [
                "variant",
                "ending_equity",
                "total_return_pct",
                "annualized_return_pct",
                "annualized_volatility_pct",
                "sharpe_ratio",
                "max_drawdown_pct",
                "total_trades",
                "win_rate_pct",
                "profit_factor",
            ]
        ].to_string(index=False)
    )

    return summary, diagnostics


def main() -> None:
    if not FEATURES_PATH.exists():
        raise FileNotFoundError(
            f"Missing input file: {FEATURES_PATH}"
        )

    features = pd.read_csv(
        FEATURES_PATH
    )

    fx_prices = load_fx_prices()

    OUTPUT_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    summaries = []
    diagnostics = []

    for sizing_mode in SIZING_MODES:
        for variant_name, config in VARIANTS.items():
            summary, diagnostic = run_variant(
                variant_name=variant_name,
                config=config,
                sizing_mode=sizing_mode,
                features=features,
                fx_prices=fx_prices,
            )

            diagnostic = diagnostic.copy()
            diagnostic.insert(
                1,
                "sizing_mode",
                sizing_mode,
            )

            summaries.append(summary)
            diagnostics.append(diagnostic)

    comparison_summary = pd.concat(
        summaries,
        ignore_index=True,
    )

    comparison_diagnostics = pd.concat(
        diagnostics,
        ignore_index=True,
    )

    comparison_summary.to_csv(
        OUTPUT_ROOT
        / "layer_comparison_summary.csv",
        index=False,
    )

    comparison_diagnostics.to_csv(
        OUTPUT_ROOT
        / "layer_comparison_diagnostics.csv",
        index=False,
    )

    print(
        f"\n{'=' * 70}\n"
        "Layer comparison complete\n"
        f"{'=' * 70}"
    )

    print(
        comparison_summary[
            [
                "sizing_mode",
                "variant",
                "total_return_pct",
                "annualized_return_pct",
                "annualized_volatility_pct",
                "sharpe_ratio",
                "max_drawdown_pct",
                "total_trades",
                "win_rate_pct",
                "profit_factor",
                "total_transaction_cost_usd",
            ]
        ].to_string(index=False)
    )

    print(
        "\nSaved outputs to:"
        f"\n{OUTPUT_ROOT}"
    )


if __name__ == "__main__":
    main()