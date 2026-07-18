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
    validate_variant_output,
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
    "period_comparison"
)


# Fixed chronological evaluation periods.
#
# These are independent backtests. Each period starts with
# a fresh $100,000 portfolio and no open positions.
PERIODS = {
    "train": {
        "start": "2010-01-01",
        "end": "2018-12-31",
    },
    "validation": {
        "start": "2019-01-01",
        "end": "2022-12-31",
    },
    "test": {
        "start": "2023-01-01",
        "end": None,
    },
}


def slice_period(
    df: pd.DataFrame,
    *,
    start_date: str,
    end_date: str | None,
    date_column: str = "date",
) -> pd.DataFrame:
    result = df.copy()

    result[date_column] = pd.to_datetime(
        result[date_column]
    )

    mask = (
        result[date_column]
        >= pd.Timestamp(start_date)
    )

    if end_date is not None:
        mask &= (
            result[date_column]
            <= pd.Timestamp(end_date)
        )

    return (
        result.loc[mask]
        .copy()
        .reset_index(drop=True)
    )


def build_full_variant(
    *,
    variant_name: str,
    config: dict[str, bool],
    sizing_mode: str,
    features: pd.DataFrame,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
]:
    """
    Build the variant over the full causal history before
    slicing it into evaluation periods.

    This preserves historical rolling information used by
    feature and position-size normalization at the beginning
    of each evaluation period.
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

    return (
        signals,
        divergence,
        trade_rules,
        sized_trades,
    )


def add_period_summary_metadata(
    summary: pd.DataFrame,
    *,
    period_name: str,
    requested_start: str,
    requested_end: str | None,
    effective_start: pd.Timestamp,
    effective_end: pd.Timestamp,
    variant_name: str,
    sizing_mode: str,
    config: dict[str, bool],
) -> pd.DataFrame:
    summary = summary.copy()

    metadata = [
        (
            "period",
            period_name,
        ),
        (
            "requested_period_start",
            requested_start,
        ),
        (
            "requested_period_end",
            (
                requested_end
                if requested_end is not None
                else "latest"
            ),
        ),
        (
            "effective_period_start",
            effective_start.date(),
        ),
        (
            "effective_period_end",
            effective_end.date(),
        ),
        (
            "variant",
            variant_name,
        ),
        (
            "sizing_mode",
            sizing_mode,
        ),
        (
            "uses_sentiment",
            int(config["use_sentiment"]),
        ),
        (
            "uses_fundamentals",
            int(
                config["use_fundamentals"]
            ),
        ),
        (
            "uses_divergence",
            0,
        ),
    ]

    for position, (
        column,
        value,
    ) in enumerate(metadata):
        summary.insert(
            position,
            column,
            value,
        )

    return summary


def add_period_diagnostic_metadata(
    diagnostics: pd.DataFrame,
    *,
    period_name: str,
    requested_start: str,
    requested_end: str | None,
    effective_start: pd.Timestamp,
    effective_end: pd.Timestamp,
    sizing_mode: str,
) -> pd.DataFrame:
    diagnostics = diagnostics.copy()

    diagnostics.insert(
        0,
        "period",
        period_name,
    )

    diagnostics.insert(
        1,
        "requested_period_start",
        requested_start,
    )

    diagnostics.insert(
        2,
        "requested_period_end",
        (
            requested_end
            if requested_end is not None
            else "latest"
        ),
    )

    diagnostics.insert(
        3,
        "effective_period_start",
        effective_start.date(),
    )

    diagnostics.insert(
        4,
        "effective_period_end",
        effective_end.date(),
    )

    diagnostics.insert(
        6,
        "sizing_mode",
        sizing_mode,
    )

    return diagnostics


def run_period_variant(
    *,
    period_name: str,
    period_config: dict[str, str | None],
    variant_name: str,
    variant_config: dict[str, bool],
    sizing_mode: str,
    features: pd.DataFrame,
    fx_prices: pd.DataFrame,
    signals: pd.DataFrame,
    divergence: pd.DataFrame,
    trade_rules: pd.DataFrame,
    sized_trades: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    start_date = period_config["start"]
    end_date = period_config["end"]

    period_features = slice_period(
        features,
        start_date=start_date,
        end_date=end_date,
    )

    period_signals = slice_period(
        signals,
        start_date=start_date,
        end_date=end_date,
    )

    period_divergence = slice_period(
        divergence,
        start_date=start_date,
        end_date=end_date,
    )

    period_trade_rules = slice_period(
        trade_rules,
        start_date=start_date,
        end_date=end_date,
    )

    period_sized_trades = slice_period(
        sized_trades,
        start_date=start_date,
        end_date=end_date,
    )

    period_fx_prices = slice_period(
        fx_prices,
        start_date=start_date,
        end_date=end_date,
    )

    if period_features.empty:
        raise ValueError(
            f"{period_name}: no feature rows "
            "exist in the requested period."
        )

    if period_fx_prices.empty:
        raise ValueError(
            f"{period_name}: no FX prices "
            "exist in the requested period."
        )

    validate_variant_output(
        variant_name=variant_name,
        features=period_features,
        signals=period_signals,
        divergence=period_divergence,
        trade_rules=period_trade_rules,
        sized_trades=period_sized_trades,
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
        f"\n{'=' * 72}\n"
        f"Period: {period_name}\n"
        f"Variant: {variant_name}\n"
        f"Sizing: {sizing_mode}\n"
        f"Dates: {effective_start.date()} "
        f"through {effective_end.date()}\n"
        f"{'=' * 72}"
    )

    trades, equity, decisions = (
        run_event_backtest(
            period_sized_trades,
            period_fx_prices,
        )
    )

    summary = calculate_summary(
        trades,
        equity,
        decisions,
    )

    summary = add_period_summary_metadata(
        summary,
        period_name=period_name,
        requested_start=start_date,
        requested_end=end_date,
        effective_start=effective_start,
        effective_end=effective_end,
        variant_name=variant_name,
        sizing_mode=sizing_mode,
        config=variant_config,
    )

    diagnostics = build_variant_diagnostics(
        variant_name=variant_name,
        signals=period_signals,
        trade_rules=period_trade_rules,
        sized_trades=period_sized_trades,
    )

    diagnostics = (
        add_period_diagnostic_metadata(
            diagnostics,
            period_name=period_name,
            requested_start=start_date,
            requested_end=end_date,
            effective_start=effective_start,
            effective_end=effective_end,
            sizing_mode=sizing_mode,
        )
    )

    reports = build_group_reports(
        trades
    )

    output_dir = (
        OUTPUT_ROOT
        / period_name
        / sizing_mode
        / variant_name
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

    return summary, diagnostics


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
            variant_config,
        ) in VARIANTS.items():
            (
                signals,
                divergence,
                trade_rules,
                sized_trades,
            ) = build_full_variant(
                variant_name=variant_name,
                config=variant_config,
                sizing_mode=sizing_mode,
                features=features,
            )

            for (
                period_name,
                period_config,
            ) in PERIODS.items():
                (
                    summary,
                    diagnostic,
                ) = run_period_variant(
                    period_name=period_name,
                    period_config=period_config,
                    variant_name=variant_name,
                    variant_config=variant_config,
                    sizing_mode=sizing_mode,
                    features=features,
                    fx_prices=fx_prices,
                    signals=signals,
                    divergence=divergence,
                    trade_rules=trade_rules,
                    sized_trades=sized_trades,
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
        / "period_comparison_summary.csv",
        index=False,
    )

    comparison_diagnostics.to_csv(
        OUTPUT_ROOT
        / "period_comparison_diagnostics.csv",
        index=False,
    )

    print(
        f"\n{'=' * 72}\n"
        "Chronological period comparison complete\n"
        f"{'=' * 72}"
    )

    display_columns = [
        "period",
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

    print(
        comparison_summary[
            display_columns
        ].to_string(index=False)
    )

    print(
        "\nSaved outputs to:"
        f"\n{OUTPUT_ROOT}"
    )


if __name__ == "__main__":
    main()