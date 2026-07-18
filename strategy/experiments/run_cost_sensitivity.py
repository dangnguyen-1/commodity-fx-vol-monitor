from pathlib import Path

import numpy as np
import pandas as pd

from strategy.backtest.run_backtest import (
    build_group_reports,
    calculate_summary,
    load_fx_prices,
    run_event_backtest,
)
from strategy.experiments.run_layer_comparison import (
    SIZING_MODES,
    VARIANTS,
    build_variant_diagnostics,
    save_variant_outputs,
    validate_variant_output,
)
from strategy.experiments.run_period_comparison import (
    build_full_variant,
    slice_period,
)


FEATURES_PATH = Path(
    "strategy/output/daily_features.csv"
)

OUTPUT_ROOT = Path(
    "strategy/output/experiments/"
    "cost_sensitivity"
)


# We care most about model-selection and out-of-sample behavior.
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


ROUND_TRIP_COSTS_BPS = [
    0.0,
    1.0,
    2.0,
    5.0,
]


def validate_period_frames(
    *,
    period_name: str,
    variant_name: str,
    features: pd.DataFrame,
    signals: pd.DataFrame,
    divergence: pd.DataFrame,
    trade_rules: pd.DataFrame,
    sized_trades: pd.DataFrame,
    fx_prices: pd.DataFrame,
) -> None:
    if features.empty:
        raise ValueError(
            f"{period_name}: no feature rows."
        )

    if fx_prices.empty:
        raise ValueError(
            f"{period_name}: no FX price rows."
        )

    validate_variant_output(
        variant_name=variant_name,
        features=features,
        signals=signals,
        divergence=divergence,
        trade_rules=trade_rules,
        sized_trades=sized_trades,
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
    variant_config: dict[str, bool],
    sizing_mode: str,
) -> pd.DataFrame:
    summary = summary.copy()

    metadata = pd.DataFrame(
        [
            {
                "period": period_name,
                "requested_period_start": period_start,
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
                "uses_sentiment": int(
                    variant_config[
                        "use_sentiment"
                    ]
                ),
                "uses_fundamentals": int(
                    variant_config[
                        "use_fundamentals"
                    ]
                ),
                "uses_divergence": 0,
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


def add_diagnostic_metadata(
    diagnostics: pd.DataFrame,
    *,
    period_name: str,
    effective_start: pd.Timestamp,
    effective_end: pd.Timestamp,
    sizing_mode: str,
    round_trip_cost_bps: float,
) -> pd.DataFrame:
    diagnostics = diagnostics.copy()

    diagnostics.insert(
        0,
        "period",
        period_name,
    )

    diagnostics.insert(
        1,
        "effective_period_start",
        effective_start.date(),
    )

    diagnostics.insert(
        2,
        "effective_period_end",
        effective_end.date(),
    )

    diagnostics.insert(
        4,
        "sizing_mode",
        sizing_mode,
    )

    diagnostics.insert(
        5,
        "round_trip_cost_bps",
        round_trip_cost_bps,
    )

    return diagnostics


def run_cost_case(
    *,
    period_name: str,
    period_config: dict[str, str | None],
    variant_name: str,
    variant_config: dict[str, bool],
    sizing_mode: str,
    round_trip_cost_bps: float,
    period_features: pd.DataFrame,
    period_signals: pd.DataFrame,
    period_divergence: pd.DataFrame,
    period_trade_rules: pd.DataFrame,
    period_sized_trades: pd.DataFrame,
    period_fx_prices: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    effective_start = max(
        period_features["date"].min(),
        period_fx_prices["date"].min(),
    )

    effective_end = min(
        period_features["date"].max(),
        period_fx_prices["date"].max(),
    )

    print(
        f"\n{'=' * 74}\n"
        f"Period: {period_name}\n"
        f"Variant: {variant_name}\n"
        f"Sizing: {sizing_mode}\n"
        f"Round-trip cost: "
        f"{round_trip_cost_bps:.1f} bps\n"
        f"Dates: {effective_start.date()} "
        f"through {effective_end.date()}\n"
        f"{'=' * 74}"
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
        period_start=period_config["start"],
        period_end=period_config["end"],
        effective_start=effective_start,
        effective_end=effective_end,
        variant_name=variant_name,
        variant_config=variant_config,
        sizing_mode=sizing_mode,
    )

    diagnostics = build_variant_diagnostics(
        variant_name=variant_name,
        signals=period_signals,
        trade_rules=period_trade_rules,
        sized_trades=period_sized_trades,
    )

    diagnostics = add_diagnostic_metadata(
        diagnostics,
        period_name=period_name,
        effective_start=effective_start,
        effective_end=effective_end,
        sizing_mode=sizing_mode,
        round_trip_cost_bps=(
            round_trip_cost_bps
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
        / cost_folder
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


def estimate_break_even_costs(
    summaries: pd.DataFrame,
) -> pd.DataFrame:
    """
    Estimate the cost at which total return reaches zero.

    The interpolation is approximate because transaction costs
    slightly change portfolio equity and later position notionals.
    """
    rows = []

    group_columns = [
        "period",
        "sizing_mode",
        "variant",
    ]

    for keys, group in summaries.groupby(
        group_columns,
        sort=False,
    ):
        group = group.sort_values(
            "round_trip_cost_bps"
        )

        costs = group[
            "round_trip_cost_bps"
        ].to_numpy(dtype=float)

        returns = group[
            "total_return_pct"
        ].to_numpy(dtype=float)

        period, sizing_mode, variant = keys

        estimate = np.nan
        status = "not_determined"

        zero_mask = np.isclose(
            returns,
            0.0,
            atol=1e-12,
        )

        if zero_mask.any():
            estimate = float(
                costs[
                    np.flatnonzero(
                        zero_mask
                    )[0]
                ]
            )
            status = "exact_grid_point"

        elif returns[0] < 0:
            estimate = 0.0
            status = "negative_at_zero_cost"

        else:
            negative_indices = np.flatnonzero(
                returns < 0
            )

            if len(negative_indices) == 0:
                status = "above_max_tested_cost"
            else:
                upper_index = int(
                    negative_indices[0]
                )

                lower_index = (
                    upper_index - 1
                )

                lower_cost = costs[
                    lower_index
                ]
                upper_cost = costs[
                    upper_index
                ]

                lower_return = returns[
                    lower_index
                ]
                upper_return = returns[
                    upper_index
                ]

                return_change = (
                    upper_return
                    - lower_return
                )

                if return_change == 0:
                    estimate = float(
                        lower_cost
                    )
                else:
                    estimate = float(
                        lower_cost
                        + (
                            -lower_return
                            * (
                                upper_cost
                                - lower_cost
                            )
                            / return_change
                        )
                    )

                status = "interpolated"

        rows.append(
            {
                "period": period,
                "sizing_mode": sizing_mode,
                "variant": variant,
                "zero_cost_return_pct": (
                    returns[0]
                ),
                "max_tested_cost_bps": (
                    costs.max()
                ),
                "estimated_break_even_cost_bps": (
                    estimate
                ),
                "break_even_status": status,
            }
        )

    return pd.DataFrame(rows)


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
            ) in EVALUATION_PERIODS.items():
                start_date = (
                    period_config["start"]
                )
                end_date = (
                    period_config["end"]
                )

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

                validate_period_frames(
                    period_name=period_name,
                    variant_name=variant_name,
                    features=period_features,
                    signals=period_signals,
                    divergence=period_divergence,
                    trade_rules=period_trade_rules,
                    sized_trades=period_sized_trades,
                    fx_prices=period_fx_prices,
                )

                for cost_bps in (
                    ROUND_TRIP_COSTS_BPS
                ):
                    (
                        summary,
                        diagnostic,
                    ) = run_cost_case(
                        period_name=period_name,
                        period_config=(
                            period_config
                        ),
                        variant_name=(
                            variant_name
                        ),
                        variant_config=(
                            variant_config
                        ),
                        sizing_mode=sizing_mode,
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

    break_even = estimate_break_even_costs(
        comparison_summary
    )

    comparison_summary.to_csv(
        OUTPUT_ROOT
        / "cost_sensitivity_summary.csv",
        index=False,
    )

    comparison_diagnostics.to_csv(
        OUTPUT_ROOT
        / "cost_sensitivity_diagnostics.csv",
        index=False,
    )

    break_even.to_csv(
        OUTPUT_ROOT
        / "break_even_cost_estimates.csv",
        index=False,
    )

    print(
        f"\n{'=' * 74}\n"
        "Transaction-cost sensitivity complete\n"
        f"{'=' * 74}"
    )

    display_columns = [
        "period",
        "sizing_mode",
        "variant",
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
        "\nEstimated break-even costs:"
    )

    print(
        break_even.to_string(
            index=False
        )
    )

    print(
        "\nSaved outputs to:"
        f"\n{OUTPUT_ROOT}"
    )


if __name__ == "__main__":
    main()