from pathlib import Path

import numpy as np
import pandas as pd


EXPERIMENT_ROOT = Path(
    "strategy/output/experiments/"
    "final_layer_ablation"
)

OUTPUT_ROOT = Path(
    "strategy/output/experiments/"
    "final_yearly_stability"
)


VARIANTS = [
    "market_fundamentals",
    "market_fundamentals_news",
]


PERIODS = [
    "validation",
    "research_holdout",
]


REQUIRED_COLUMNS = [
    "position_id",
    "entry_date",
    "exit_date",
    "relationship_id",
    "primary_trade_rule",
    "exit_reason",
    "notional_usd",
    "gross_pnl_usd",
    "transaction_cost_usd",
    "net_pnl_usd",
    "winning_trade",
    "trade_return_on_notional",
]


def calculate_profit_factor(
    pnl: pd.Series,
) -> float:
    gains = float(
        pnl.loc[pnl > 0].sum()
    )

    losses = float(
        -pnl.loc[pnl < 0].sum()
    )

    if losses == 0:
        if gains > 0:
            return np.inf

        return np.nan

    return gains / losses


def load_final_trades() -> pd.DataFrame:
    frames: list[pd.DataFrame] = []

    for variant in VARIANTS:
        for period in PERIODS:
            path = (
                EXPERIMENT_ROOT
                / variant
                / period
                / "2_bps"
                / "trades.csv"
            )

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
                    f"{path} is missing columns: "
                    f"{missing_columns}"
                )

            trades["entry_date"] = pd.to_datetime(
                trades["entry_date"]
            )

            trades["exit_date"] = pd.to_datetime(
                trades["exit_date"]
            )

            trades["variant"] = variant
            trades["period"] = period
            trades["calendar_year"] = (
                trades["entry_date"].dt.year
            )

            frames.append(trades)

    combined = pd.concat(
        frames,
        ignore_index=True,
    )

    duplicate_count = combined.duplicated(
        [
            "variant",
            "period",
            "position_id",
        ]
    ).sum()

    if duplicate_count != 0:
        raise ValueError(
            "Combined trades contain "
            f"{duplicate_count} duplicate rows."
        )

    return combined


def summarize_groups(
    trades: pd.DataFrame,
    group_columns: list[str],
) -> pd.DataFrame:
    records: list[dict] = []

    for keys, group in trades.groupby(
        group_columns,
        dropna=False,
        sort=True,
    ):
        if not isinstance(keys, tuple):
            keys = (keys,)

        record = dict(
            zip(group_columns, keys)
        )

        total_notional = float(
            group["notional_usd"].abs().sum()
        )

        gross_pnl = float(
            group["gross_pnl_usd"].sum()
        )

        costs = float(
            group["transaction_cost_usd"].sum()
        )

        net_pnl = float(
            group["net_pnl_usd"].sum()
        )

        record.update(
            {
                "total_trades": len(group),
                "winning_trades": int(
                    group["winning_trade"].sum()
                ),
                "win_rate_pct": (
                    100.0
                    * group["winning_trade"].mean()
                ),
                "total_notional_usd": total_notional,
                "gross_pnl_usd": gross_pnl,
                "transaction_cost_usd": costs,
                "net_pnl_usd": net_pnl,
                "gross_return_on_notional_pct": (
                    100.0
                    * gross_pnl
                    / total_notional
                    if total_notional != 0
                    else np.nan
                ),
                "net_return_on_notional_pct": (
                    100.0
                    * net_pnl
                    / total_notional
                    if total_notional != 0
                    else np.nan
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
                        group["gross_pnl_usd"]
                    )
                ),
                "net_profit_factor": (
                    calculate_profit_factor(
                        group["net_pnl_usd"]
                    )
                ),
                "relationships_traded": int(
                    group[
                        "relationship_id"
                    ].nunique()
                ),
                "divergence_trade_share_pct": (
                    100.0
                    * group[
                        "primary_trade_rule"
                    ]
                    .eq("confirmed_divergence")
                    .mean()
                ),
                "holding_period_exit_share_pct": (
                    100.0
                    * group["exit_reason"]
                    .eq("holding_period")
                    .mean()
                ),
                "signal_flip_exit_share_pct": (
                    100.0
                    * group["exit_reason"]
                    .eq("signal_flip")
                    .mean()
                ),
                "divergence_close_exit_share_pct": (
                    100.0
                    * group["exit_reason"]
                    .eq("divergence_close")
                    .mean()
                ),
            }
        )

        records.append(record)

    return pd.DataFrame(records)


def build_yearly_comparison(
    yearly: pd.DataFrame,
) -> pd.DataFrame:
    metrics = [
        "total_trades",
        "net_pnl_usd",
        "net_return_on_notional_pct",
        "win_rate_pct",
        "net_profit_factor",
        "transaction_cost_usd",
        "relationships_traded",
    ]

    fundamentals = yearly.loc[
        yearly["variant"].eq(
            "market_fundamentals"
        ),
        [
            "period",
            "calendar_year",
        ]
        + metrics,
    ].copy()

    full_model = yearly.loc[
        yearly["variant"].eq(
            "market_fundamentals_news"
        ),
        [
            "period",
            "calendar_year",
        ]
        + metrics,
    ].copy()

    comparison = fundamentals.merge(
        full_model,
        on=[
            "period",
            "calendar_year",
        ],
        how="outer",
        validate="one_to_one",
        suffixes=(
            "_fundamentals",
            "_full_model",
        ),
    )

    for metric in metrics:
        comparison[
            f"delta_{metric}"
        ] = (
            comparison[
                f"{metric}_full_model"
            ]
            - comparison[
                f"{metric}_fundamentals"
            ]
        )

    comparison[
        "full_model_profitable"
    ] = (
        comparison[
            "net_pnl_usd_full_model"
        ] > 0
    ).astype(int)

    comparison[
        "fundamentals_profitable"
    ] = (
        comparison[
            "net_pnl_usd_fundamentals"
        ] > 0
    ).astype(int)

    comparison[
        "news_improves_year"
    ] = (
        comparison[
            "delta_net_pnl_usd"
        ] > 0
    ).astype(int)

    return comparison


def build_stability_summary(
    yearly: pd.DataFrame,
) -> pd.DataFrame:
    records = []

    for variant, group in yearly.groupby(
        "variant",
        sort=True,
    ):
        profitable_years = int(
            group["net_pnl_usd"].gt(0).sum()
        )

        total_years = len(group)

        records.append(
            {
                "variant": variant,
                "total_years": total_years,
                "profitable_years": profitable_years,
                "losing_years": (
                    total_years
                    - profitable_years
                ),
                "profitable_year_share_pct": (
                    100.0
                    * profitable_years
                    / total_years
                ),
                "average_yearly_net_pnl_usd": float(
                    group["net_pnl_usd"].mean()
                ),
                "median_yearly_net_pnl_usd": float(
                    group["net_pnl_usd"].median()
                ),
                "worst_year_net_pnl_usd": float(
                    group["net_pnl_usd"].min()
                ),
                "best_year_net_pnl_usd": float(
                    group["net_pnl_usd"].max()
                ),
                "average_yearly_net_return_pct": float(
                    group[
                        "net_return_on_notional_pct"
                    ].mean()
                ),
                "median_yearly_profit_factor": float(
                    group[
                        "net_profit_factor"
                    ].median()
                ),
            }
        )

    return pd.DataFrame(records)


def main() -> None:
    trades = load_final_trades()

    OUTPUT_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    yearly = summarize_groups(
        trades,
        [
            "variant",
            "period",
            "calendar_year",
        ],
    )

    yearly_rule = summarize_groups(
        trades,
        [
            "variant",
            "period",
            "calendar_year",
            "primary_trade_rule",
        ],
    )

    yearly_exit = summarize_groups(
        trades,
        [
            "variant",
            "period",
            "calendar_year",
            "exit_reason",
        ],
    )

    yearly_comparison = (
        build_yearly_comparison(yearly)
    )

    stability_summary = (
        build_stability_summary(yearly)
    )

    yearly.to_csv(
        OUTPUT_ROOT
        / "yearly_performance.csv",
        index=False,
    )

    yearly_rule.to_csv(
        OUTPUT_ROOT
        / "yearly_rule_performance.csv",
        index=False,
    )

    yearly_exit.to_csv(
        OUTPUT_ROOT
        / "yearly_exit_performance.csv",
        index=False,
    )

    yearly_comparison.to_csv(
        OUTPUT_ROOT
        / "yearly_variant_comparison.csv",
        index=False,
    )

    stability_summary.to_csv(
        OUTPUT_ROOT
        / "yearly_stability_summary.csv",
        index=False,
    )

    print(
        f"\n{'=' * 78}\n"
        "Final yearly stability analysis complete\n"
        f"{'=' * 78}"
    )

    print("\nYearly performance:")
    print(
        yearly[
            [
                "variant",
                "period",
                "calendar_year",
                "total_trades",
                "net_pnl_usd",
                "net_return_on_notional_pct",
                "win_rate_pct",
                "net_profit_factor",
                "transaction_cost_usd",
                "relationships_traded",
            ]
        ].to_string(index=False)
    )

    print("\nYearly news-layer comparison:")
    print(
        yearly_comparison[
            [
                "period",
                "calendar_year",
                "net_pnl_usd_fundamentals",
                "net_pnl_usd_full_model",
                "delta_net_pnl_usd",
                "net_profit_factor_fundamentals",
                "net_profit_factor_full_model",
                "fundamentals_profitable",
                "full_model_profitable",
                "news_improves_year",
            ]
        ].to_string(index=False)
    )

    print("\nStability summary:")
    print(
        stability_summary.to_string(
            index=False
        )
    )

    print(
        "\nSaved outputs to:"
        f"\n{OUTPUT_ROOT}"
    )


if __name__ == "__main__":
    main()