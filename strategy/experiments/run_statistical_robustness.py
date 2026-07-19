from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import numpy as np
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
    "statistical_robustness"
)


VARIANT_NAME = "market_fundamentals_news"

UNIVERSE_MODE = "rolling_soft_weight"

ROUND_TRIP_COST_BPS = 2.0

ADDITIONAL_ENTRY_DELAY_DAYS = 0

ANNUALIZATION_DAYS = 252


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


SLEEVES = [
    "full",
    "brl_only",
    "ex_brl",
]

PLACEBO_SLEEVES = [
    "full",
    "brl_only",
]


BOOTSTRAP_REPLICATIONS = int(
    os.getenv(
        "STRATEGY_BOOTSTRAP_REPLICATIONS",
        "2000",
    )
)

PLACEBO_REPLICATIONS = int(
    os.getenv(
        "STRATEGY_PLACEBO_REPLICATIONS",
        "50",
    )
)

DAILY_BLOCK_LENGTH = int(
    os.getenv(
        "STRATEGY_DAILY_BLOCK_LENGTH",
        "20",
    )
)

TRADE_BLOCK_LENGTH = int(
    os.getenv(
        "STRATEGY_TRADE_BLOCK_LENGTH",
        "50",
    )
)

PLACEBO_MIN_SHIFT_OBSERVATIONS = int(
    os.getenv(
        "STRATEGY_PLACEBO_MIN_SHIFT",
        "63",
    )
)

RANDOM_SEED = int(
    os.getenv(
        "STRATEGY_RANDOM_SEED",
        "20260718",
    )
)


CORE_SHIFT_COLUMNS = [
    "primary_trade_rule",
    "trade_candidate",
    "trade_direction",
    "signal_direction",
    "price_layer_direction",
    "has_position",
    "position_size_pct",
    "combined_trade_score",
    "confirmation_score",
    "divergence_score",
    "is_divergence_opportunity",
    "exit_on_signal_flip",
    "exit_on_divergence_close",
    "default_holding_period_days",
    "layers_triggered",
    "priority",
]


def stable_seed(
    *parts: str,
    base_seed: int = RANDOM_SEED,
) -> int:
    text = "|".join(
        [
            str(base_seed),
            *map(str, parts),
        ]
    )

    digest = hashlib.sha256(
        text.encode("utf-8")
    ).digest()

    return int.from_bytes(
        digest[:8],
        byteorder="little",
        signed=False,
    ) % (2**32 - 1)


def safe_divide(
    numerator: float,
    denominator: float,
) -> float:
    if (
        denominator == 0
        or pd.isna(denominator)
    ):
        return np.nan

    return numerator / denominator


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
    Disable new entries while retaining daily signal
    rows needed to manage existing open positions.
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

    available_size_columns = [
        column
        for column in SIZE_COLUMNS
        if column in result.columns
    ]

    if (
        "position_size_pct"
        not in available_size_columns
        and "position_size_pct"
        in result.columns
    ):
        available_size_columns.append(
            "position_size_pct"
        )

    if available_size_columns:
        result.loc[
            mask,
            available_size_columns,
        ] = 0.0

    return result


def build_sleeve_strategy(
    weighted_strategy: pd.DataFrame,
    sleeve: str,
) -> pd.DataFrame:
    if sleeve == "full":
        return weighted_strategy.copy()

    if sleeve == "brl_only":
        exclusion_mask = (
            weighted_strategy["currency"]
            .astype(str)
            .ne("BRL")
        )

        return disable_entries(
            weighted_strategy,
            exclusion_mask,
        )

    if sleeve == "ex_brl":
        exclusion_mask = (
            weighted_strategy["currency"]
            .astype(str)
            .eq("BRL")
        )

        return disable_entries(
            weighted_strategy,
            exclusion_mask,
        )

    raise ValueError(
        f"Unknown sleeve: {sleeve}"
    )


def run_strategy_case(
    *,
    period_name: str,
    sleeve: str,
    strategy: pd.DataFrame,
    fx_prices: pd.DataFrame,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
]:
    trades, equity, decisions = (
        run_event_backtest(
            strategy,
            fx_prices,
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
                "sleeve": sleeve,
                "variant": VARIANT_NAME,
                "universe_mode": (
                    UNIVERSE_MODE
                ),
                "round_trip_cost_bps": (
                    ROUND_TRIP_COST_BPS
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

    return (
        trades,
        equity,
        decisions,
        summary,
    )


def circular_block_sample(
    values: np.ndarray,
    *,
    block_length: int,
    rng: np.random.Generator,
) -> np.ndarray:
    values = np.asarray(
        values,
        dtype=float,
    )

    n_observations = len(values)

    if n_observations == 0:
        return values.copy()

    if block_length <= 0:
        raise ValueError(
            "block_length must be positive."
        )

    effective_block_length = min(
        int(block_length),
        n_observations,
    )

    blocks_needed = int(
        np.ceil(
            n_observations
            / effective_block_length
        )
    )

    sampled_blocks = []

    offsets = np.arange(
        effective_block_length
    )

    for _ in range(blocks_needed):
        start = int(
            rng.integers(
                0,
                n_observations,
            )
        )

        positions = (
            start + offsets
        ) % n_observations

        sampled_blocks.append(
            values[positions]
        )

    return np.concatenate(
        sampled_blocks
    )[:n_observations]


def calculate_return_statistics(
    daily_returns: np.ndarray,
) -> dict[str, float]:
    daily_returns = np.asarray(
        daily_returns,
        dtype=float,
    )

    daily_returns = daily_returns[
        np.isfinite(daily_returns)
    ]

    if len(daily_returns) == 0:
        return {
            "total_return_pct": np.nan,
            "annualized_return_pct": np.nan,
            "sharpe_ratio": np.nan,
            "max_drawdown_pct": np.nan,
        }

    equity_path = np.cumprod(
        1.0 + daily_returns
    )

    total_return = (
        equity_path[-1] - 1.0
    )

    years = max(
        len(daily_returns)
        / ANNUALIZATION_DAYS,
        1.0 / ANNUALIZATION_DAYS,
    )

    if total_return <= -1:
        annualized_return = -1.0
    else:
        annualized_return = (
            (1.0 + total_return)
            ** (1.0 / years)
            - 1.0
        )

    daily_std = daily_returns.std(
        ddof=1
    )

    sharpe_ratio = safe_divide(
        daily_returns.mean()
        * ANNUALIZATION_DAYS,
        daily_std
        * np.sqrt(
            ANNUALIZATION_DAYS
        ),
    )

    running_max = np.maximum.accumulate(
        equity_path
    )

    drawdowns = (
        equity_path / running_max - 1.0
    )

    max_drawdown = float(
        drawdowns.min()
    )

    return {
        "total_return_pct": (
            total_return * 100.0
        ),
        "annualized_return_pct": (
            annualized_return * 100.0
        ),
        "sharpe_ratio": sharpe_ratio,
        "max_drawdown_pct": (
            max_drawdown * 100.0
        ),
    }


def calculate_trade_statistics(
    trade_pnl: np.ndarray,
) -> dict[str, float]:
    trade_pnl = np.asarray(
        trade_pnl,
        dtype=float,
    )

    trade_pnl = trade_pnl[
        np.isfinite(trade_pnl)
    ]

    if len(trade_pnl) == 0:
        return {
            "profit_factor": np.nan,
            "trade_net_pnl_usd": 0.0,
        }

    gross_profit = float(
        trade_pnl[
            trade_pnl > 0
        ].sum()
    )

    gross_loss = float(
        -trade_pnl[
            trade_pnl < 0
        ].sum()
    )

    profit_factor = safe_divide(
        gross_profit,
        gross_loss,
    )

    return {
        "profit_factor": profit_factor,
        "trade_net_pnl_usd": float(
            trade_pnl.sum()
        ),
    }


def run_block_bootstrap(
    *,
    period_name: str,
    sleeve: str,
    trades: pd.DataFrame,
    equity: pd.DataFrame,
) -> pd.DataFrame:
    if equity.empty:
        raise ValueError(
            f"{period_name}/{sleeve}: "
            "cannot bootstrap an empty "
            "equity curve."
        )

    daily_returns = (
        pd.to_numeric(
            equity["daily_return"],
            errors="coerce",
        )
        .fillna(0.0)
        .to_numpy(dtype=float)
    )

    if trades.empty:
        trade_pnl = np.array(
            [],
            dtype=float,
        )
    else:
        ordered_trades = trades.sort_values(
            [
                "exit_date",
                "position_id",
            ],
            kind="mergesort",
        )

        trade_pnl = (
            pd.to_numeric(
                ordered_trades[
                    "net_pnl_usd"
                ],
                errors="coerce",
            )
            .dropna()
            .to_numpy(dtype=float)
        )

    rng = np.random.default_rng(
        stable_seed(
            "bootstrap",
            period_name,
            sleeve,
        )
    )

    records: list[
        dict[str, Any]
    ] = []

    for replication in range(
        1,
        BOOTSTRAP_REPLICATIONS + 1,
    ):
        sampled_returns = (
            circular_block_sample(
                daily_returns,
                block_length=(
                    DAILY_BLOCK_LENGTH
                ),
                rng=rng,
            )
        )

        return_statistics = (
            calculate_return_statistics(
                sampled_returns
            )
        )

        if len(trade_pnl) > 0:
            sampled_trade_pnl = (
                circular_block_sample(
                    trade_pnl,
                    block_length=(
                        TRADE_BLOCK_LENGTH
                    ),
                    rng=rng,
                )
            )
        else:
            sampled_trade_pnl = (
                trade_pnl.copy()
            )

        trade_statistics = (
            calculate_trade_statistics(
                sampled_trade_pnl
            )
        )

        records.append(
            {
                "period": period_name,
                "sleeve": sleeve,
                "replication": replication,
                **return_statistics,
                **trade_statistics,
            }
        )

    return pd.DataFrame(records)


def percentile_or_nan(
    values: pd.Series,
    percentile: float,
) -> float:
    valid_values = pd.to_numeric(
        values,
        errors="coerce",
    ).dropna()

    if valid_values.empty:
        return np.nan

    return float(
        np.percentile(
            valid_values,
            percentile,
        )
    )


def summarize_bootstrap(
    bootstrap_draws: pd.DataFrame,
    baseline_summary: pd.DataFrame,
) -> pd.DataFrame:
    baseline_lookup = (
        baseline_summary.set_index(
            [
                "period",
                "sleeve",
            ]
        )
    )

    records = []

    for (
        period,
        sleeve,
    ), group in bootstrap_draws.groupby(
        [
            "period",
            "sleeve",
        ],
        sort=True,
    ):
        baseline = baseline_lookup.loc[
            (
                period,
                sleeve,
            )
        ]

        records.append(
            {
                "period": period,
                "sleeve": sleeve,
                "bootstrap_replications": (
                    len(group)
                ),
                "daily_block_length": (
                    DAILY_BLOCK_LENGTH
                ),
                "trade_block_length": (
                    TRADE_BLOCK_LENGTH
                ),
                "actual_total_return_pct": float(
                    baseline[
                        "total_return_pct"
                    ]
                ),
                "return_ci_2_5_pct": (
                    percentile_or_nan(
                        group[
                            "total_return_pct"
                        ],
                        2.5,
                    )
                ),
                "return_median_pct": (
                    percentile_or_nan(
                        group[
                            "total_return_pct"
                        ],
                        50.0,
                    )
                ),
                "return_ci_97_5_pct": (
                    percentile_or_nan(
                        group[
                            "total_return_pct"
                        ],
                        97.5,
                    )
                ),
                "probability_positive_return": float(
                    group[
                        "total_return_pct"
                    ]
                    .gt(0)
                    .mean()
                ),
                "actual_sharpe_ratio": float(
                    baseline[
                        "sharpe_ratio"
                    ]
                ),
                "sharpe_ci_2_5": (
                    percentile_or_nan(
                        group[
                            "sharpe_ratio"
                        ],
                        2.5,
                    )
                ),
                "sharpe_median": (
                    percentile_or_nan(
                        group[
                            "sharpe_ratio"
                        ],
                        50.0,
                    )
                ),
                "sharpe_ci_97_5": (
                    percentile_or_nan(
                        group[
                            "sharpe_ratio"
                        ],
                        97.5,
                    )
                ),
                "probability_positive_sharpe": float(
                    group[
                        "sharpe_ratio"
                    ]
                    .gt(0)
                    .mean()
                ),
                "actual_max_drawdown_pct": float(
                    baseline[
                        "max_drawdown_pct"
                    ]
                ),
                "max_drawdown_ci_2_5_pct": (
                    percentile_or_nan(
                        group[
                            "max_drawdown_pct"
                        ],
                        2.5,
                    )
                ),
                "max_drawdown_median_pct": (
                    percentile_or_nan(
                        group[
                            "max_drawdown_pct"
                        ],
                        50.0,
                    )
                ),
                "max_drawdown_ci_97_5_pct": (
                    percentile_or_nan(
                        group[
                            "max_drawdown_pct"
                        ],
                        97.5,
                    )
                ),
                "actual_profit_factor": float(
                    baseline[
                        "profit_factor"
                    ]
                ),
                "profit_factor_ci_2_5": (
                    percentile_or_nan(
                        group[
                            "profit_factor"
                        ],
                        2.5,
                    )
                ),
                "profit_factor_median": (
                    percentile_or_nan(
                        group[
                            "profit_factor"
                        ],
                        50.0,
                    )
                ),
                "profit_factor_ci_97_5": (
                    percentile_or_nan(
                        group[
                            "profit_factor"
                        ],
                        97.5,
                    )
                ),
                "probability_profit_factor_above_one": float(
                    group[
                        "profit_factor"
                    ]
                    .gt(1.0)
                    .mean()
                ),
            }
        )

    return pd.DataFrame(records)


def select_shift_columns(
    strategy: pd.DataFrame,
) -> list[str]:
    requested_columns = list(
        dict.fromkeys(
            [
                *CORE_SHIFT_COLUMNS,
                *list(SIZE_COLUMNS),
            ]
        )
    )

    shift_columns = [
        column
        for column in requested_columns
        if column in strategy.columns
    ]

    required_columns = [
        "primary_trade_rule",
        "trade_candidate",
        "trade_direction",
        "signal_direction",
        "price_layer_direction",
        "has_position",
        "position_size_pct",
        "is_divergence_opportunity",
        "exit_on_signal_flip",
        "exit_on_divergence_close",
        "default_holding_period_days",
    ]

    missing_required = sorted(
        set(required_columns)
        - set(shift_columns)
    )

    if missing_required:
        raise ValueError(
            "Cannot construct timing placebo. "
            "Missing dynamic columns: "
            f"{missing_required}"
        )

    return shift_columns


def choose_placebo_shift(
    *,
    observations: int,
    rng: np.random.Generator,
) -> int:
    if observations < 2:
        return 0

    effective_minimum = min(
        PLACEBO_MIN_SHIFT_OBSERVATIONS,
        max(
            1,
            observations // 4,
        ),
    )

    lowest_shift = (
        effective_minimum
    )

    highest_shift = (
        observations
        - effective_minimum
    )

    if highest_shift <= lowest_shift:
        return int(
            rng.integers(
                1,
                observations,
            )
        )

    return int(
        rng.integers(
            lowest_shift,
            highest_shift + 1,
        )
    )


def circular_shift_signal_paths(
    strategy: pd.DataFrame,
    *,
    rng: np.random.Generator,
    replication: int,
    period_name: str,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
]:
    result = strategy.copy()

    shift_columns = (
        select_shift_columns(result)
    )

    offset_records = []

    grouped_indices = result.groupby(
        "relationship_id",
        sort=False,
    ).groups

    for (
        relationship_id,
        index_values,
    ) in grouped_indices.items():
        ordered_index = (
            result.loc[
                list(index_values)
            ]
            .sort_values(
                "date",
                kind="mergesort",
            )
            .index
        )

        observations = len(
            ordered_index
        )

        offset = choose_placebo_shift(
            observations=observations,
            rng=rng,
        )

        if offset == 0:
            continue

        for column in shift_columns:
            original_values = (
                result.loc[
                    ordered_index,
                    column,
                ]
                .to_numpy(
                    copy=True
                )
            )

            result.loc[
                ordered_index,
                column,
            ] = np.roll(
                original_values,
                offset,
            )

        offset_records.append(
            {
                "period": period_name,
                "replication": replication,
                "relationship_id": (
                    relationship_id
                ),
                "observations": observations,
                "circular_shift_observations": (
                    offset
                ),
            }
        )

    return (
        result,
        pd.DataFrame(
            offset_records
        ),
    )


def run_placebo_distribution(
    *,
    period_name: str,
    unweighted_period_strategy: pd.DataFrame,
    period_fx_prices: pd.DataFrame,
    selection_schedule: pd.DataFrame,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
]:
    rng = np.random.default_rng(
        stable_seed(
            "placebo",
            period_name,
        )
    )

    placebo_records = []
    offset_frames = []

    for replication in range(
        1,
        PLACEBO_REPLICATIONS + 1,
    ):
        (
            shifted_strategy,
            offsets,
        ) = circular_shift_signal_paths(
            unweighted_period_strategy,
            rng=rng,
            replication=replication,
            period_name=period_name,
        )

        shifted_weighted_strategy = (
            apply_rolling_selection(
                shifted_strategy,
                universe_mode=(
                    UNIVERSE_MODE
                ),
                selection_schedule=(
                    selection_schedule
                ),
            )
        )

        for sleeve in PLACEBO_SLEEVES:
            placebo_strategy = (
                build_sleeve_strategy(
                    shifted_weighted_strategy,
                    sleeve,
                )
            )

            (
                trades,
                equity,
                decisions,
                summary,
            ) = run_strategy_case(
                period_name=period_name,
                sleeve=sleeve,
                strategy=placebo_strategy,
                fx_prices=period_fx_prices,
            )

            row = summary.iloc[0]

            placebo_records.append(
                {
                    "period": period_name,
                    "sleeve": sleeve,
                    "replication": replication,
                    "total_return_pct": float(
                        row[
                            "total_return_pct"
                        ]
                    ),
                    "annualized_return_pct": float(
                        row[
                            "annualized_return_pct"
                        ]
                    ),
                    "sharpe_ratio": float(
                        row[
                            "sharpe_ratio"
                        ]
                    ),
                    "max_drawdown_pct": float(
                        row[
                            "max_drawdown_pct"
                        ]
                    ),
                    "profit_factor": float(
                        row[
                            "profit_factor"
                        ]
                    ),
                    "total_trades": int(
                        row[
                            "total_trades"
                        ]
                    ),
                    "total_transaction_cost_usd": float(
                        row[
                            "total_transaction_cost_usd"
                        ]
                    ),
                }
            )

        offset_frames.append(
            offsets
        )

        if (
            replication == 1
            or replication
            % 10 == 0
            or replication
            == PLACEBO_REPLICATIONS
        ):
            print(
                f"{period_name}: completed "
                f"{replication}/"
                f"{PLACEBO_REPLICATIONS} "
                "placebo replications"
            )

    placebo_draws = pd.DataFrame(
        placebo_records
    )

    placebo_offsets = pd.concat(
        offset_frames,
        ignore_index=True,
    )

    return (
        placebo_draws,
        placebo_offsets,
    )


def empirical_upper_tail_p_value(
    placebo_values: pd.Series,
    actual_value: float,
) -> float:
    values = pd.to_numeric(
        placebo_values,
        errors="coerce",
    ).dropna()

    if values.empty:
        return np.nan

    exceedances = int(
        values.ge(
            actual_value
        ).sum()
    )

    return (
        exceedances + 1
    ) / (
        len(values) + 1
    )


def actual_percentile(
    placebo_values: pd.Series,
    actual_value: float,
) -> float:
    values = pd.to_numeric(
        placebo_values,
        errors="coerce",
    ).dropna()

    if values.empty:
        return np.nan

    return float(
        values.lt(
            actual_value
        ).mean()
        * 100.0
    )


def summarize_placebos(
    placebo_draws: pd.DataFrame,
    baseline_summary: pd.DataFrame,
) -> pd.DataFrame:
    baseline_lookup = (
        baseline_summary.set_index(
            [
                "period",
                "sleeve",
            ]
        )
    )

    metric_columns = [
        "total_return_pct",
        "sharpe_ratio",
        "max_drawdown_pct",
        "profit_factor",
    ]

    records = []

    for (
        period,
        sleeve,
    ), group in placebo_draws.groupby(
        [
            "period",
            "sleeve",
        ],
        sort=True,
    ):
        baseline = baseline_lookup.loc[
            (
                period,
                sleeve,
            )
        ]

        record: dict[str, Any] = {
            "period": period,
            "sleeve": sleeve,
            "placebo_replications": (
                len(group)
            ),
            "minimum_shift_observations": (
                PLACEBO_MIN_SHIFT_OBSERVATIONS
            ),
        }

        for metric in metric_columns:
            actual_value = float(
                baseline[metric]
            )

            record[
                f"actual_{metric}"
            ] = actual_value

            record[
                f"placebo_mean_{metric}"
            ] = float(
                group[metric].mean()
            )

            record[
                f"placebo_median_{metric}"
            ] = float(
                group[metric].median()
            )

            record[
                f"placebo_2_5_{metric}"
            ] = percentile_or_nan(
                group[metric],
                2.5,
            )

            record[
                f"placebo_97_5_{metric}"
            ] = percentile_or_nan(
                group[metric],
                97.5,
            )

            if metric == "max_drawdown_pct":
                # Less-negative drawdown is better.
                record[
                    f"actual_percentile_{metric}"
                ] = float(
                    group[metric]
                    .lt(actual_value)
                    .mean()
                    * 100.0
                )

                record[
                    f"empirical_p_value_{metric}"
                ] = (
                    empirical_upper_tail_p_value(
                        group[metric],
                        actual_value,
                    )
                )
            else:
                record[
                    f"actual_percentile_{metric}"
                ] = actual_percentile(
                    group[metric],
                    actual_value,
                )

                record[
                    f"empirical_p_value_{metric}"
                ] = (
                    empirical_upper_tail_p_value(
                        group[metric],
                        actual_value,
                    )
                )

        records.append(record)

    return pd.DataFrame(records)


def save_baseline_case(
    *,
    period_name: str,
    sleeve: str,
    trades: pd.DataFrame,
    equity: pd.DataFrame,
    decisions: pd.DataFrame,
    summary: pd.DataFrame,
) -> None:
    output_dir = (
        OUTPUT_ROOT
        / "baseline"
        / period_name
        / sleeve
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    trades.to_csv(
        output_dir
        / "trades.csv",
        index=False,
    )

    equity.to_csv(
        output_dir
        / "equity.csv",
        index=False,
    )

    decisions.to_csv(
        output_dir
        / "decisions.csv",
        index=False,
    )

    summary.to_csv(
        output_dir
        / "summary.csv",
        index=False,
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

    baseline_summaries = []
    bootstrap_frames = []

    period_inputs: dict[
        str,
        dict[str, pd.DataFrame],
    ] = {}

    print(
        f"Bootstrap replications: "
        f"{BOOTSTRAP_REPLICATIONS}"
    )

    print(
        f"Placebo replications per period: "
        f"{PLACEBO_REPLICATIONS}"
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

        period_inputs[
            period_name
        ] = {
            "unweighted_strategy": (
                period_strategy
            ),
            "fx_prices": (
                period_fx_prices
            ),
        }

        print(
            f"\n{'=' * 78}\n"
            f"Baseline and bootstrap: "
            f"{period_name}\n"
            f"{'=' * 78}"
        )

        for sleeve in SLEEVES:
            sleeve_strategy = (
                build_sleeve_strategy(
                    weighted_strategy,
                    sleeve,
                )
            )

            (
                trades,
                equity,
                decisions,
                summary,
            ) = run_strategy_case(
                period_name=period_name,
                sleeve=sleeve,
                strategy=sleeve_strategy,
                fx_prices=period_fx_prices,
            )

            baseline_summaries.append(
                summary
            )

            save_baseline_case(
                period_name=period_name,
                sleeve=sleeve,
                trades=trades,
                equity=equity,
                decisions=decisions,
                summary=summary,
            )

            bootstrap_draws = (
                run_block_bootstrap(
                    period_name=(
                        period_name
                    ),
                    sleeve=sleeve,
                    trades=trades,
                    equity=equity,
                )
            )

            bootstrap_frames.append(
                bootstrap_draws
            )

            row = summary.iloc[0]

            print(
                f"{sleeve:10s} | "
                f"return "
                f"{row['total_return_pct']: .6f}% | "
                f"Sharpe "
                f"{row['sharpe_ratio']: .4f} | "
                f"PF "
                f"{row['profit_factor']: .4f} | "
                f"trades "
                f"{int(row['total_trades'])}"
            )

    baseline_summary = pd.concat(
        baseline_summaries,
        ignore_index=True,
    )

    bootstrap_draws = pd.concat(
        bootstrap_frames,
        ignore_index=True,
    )

    bootstrap_summary = (
        summarize_bootstrap(
            bootstrap_draws,
            baseline_summary,
        )
    )

    placebo_draw_frames = []
    placebo_offset_frames = []

    for (
        period_name,
        inputs,
    ) in period_inputs.items():
        print(
            f"\n{'=' * 78}\n"
            f"Timing placebo: "
            f"{period_name}\n"
            f"{'=' * 78}"
        )

        (
            placebo_draws,
            placebo_offsets,
        ) = run_placebo_distribution(
            period_name=period_name,
            unweighted_period_strategy=(
                inputs[
                    "unweighted_strategy"
                ]
            ),
            period_fx_prices=(
                inputs[
                    "fx_prices"
                ]
            ),
            selection_schedule=(
                selection_schedule
            ),
        )

        placebo_draw_frames.append(
            placebo_draws
        )

        placebo_offset_frames.append(
            placebo_offsets
        )

    all_placebo_draws = pd.concat(
        placebo_draw_frames,
        ignore_index=True,
    )

    all_placebo_offsets = pd.concat(
        placebo_offset_frames,
        ignore_index=True,
    )

    placebo_summary = (
        summarize_placebos(
            all_placebo_draws,
            baseline_summary,
        )
    )

    baseline_summary.to_csv(
        OUTPUT_ROOT
        / "baseline_summary.csv",
        index=False,
    )

    bootstrap_draws.to_csv(
        OUTPUT_ROOT
        / "block_bootstrap_draws.csv",
        index=False,
    )

    bootstrap_summary.to_csv(
        OUTPUT_ROOT
        / "block_bootstrap_summary.csv",
        index=False,
    )

    all_placebo_draws.to_csv(
        OUTPUT_ROOT
        / "timing_placebo_draws.csv",
        index=False,
    )

    all_placebo_offsets.to_csv(
        OUTPUT_ROOT
        / "timing_placebo_offsets.csv",
        index=False,
    )

    placebo_summary.to_csv(
        OUTPUT_ROOT
        / "timing_placebo_summary.csv",
        index=False,
    )

    print(
        f"\n{'=' * 78}\n"
        "Statistical robustness complete\n"
        f"{'=' * 78}"
    )

    print(
        "\nBlock-bootstrap summary:"
    )

    bootstrap_display_columns = [
        "period",
        "sleeve",
        "actual_total_return_pct",
        "return_ci_2_5_pct",
        "return_median_pct",
        "return_ci_97_5_pct",
        "probability_positive_return",
        "actual_sharpe_ratio",
        "sharpe_ci_2_5",
        "sharpe_median",
        "sharpe_ci_97_5",
        "probability_positive_sharpe",
        "actual_profit_factor",
        "profit_factor_ci_2_5",
        "profit_factor_median",
        "profit_factor_ci_97_5",
        "probability_profit_factor_above_one",
    ]

    print(
        bootstrap_summary[
            bootstrap_display_columns
        ]
        .sort_values(
            [
                "period",
                "sleeve",
            ]
        )
        .to_string(index=False)
    )

    print(
        "\nTiming-placebo summary:"
    )

    placebo_display_columns = [
        "period",
        "sleeve",
        "actual_total_return_pct",
        "placebo_median_total_return_pct",
        "actual_percentile_total_return_pct",
        "empirical_p_value_total_return_pct",
        "actual_sharpe_ratio",
        "placebo_median_sharpe_ratio",
        "actual_percentile_sharpe_ratio",
        "empirical_p_value_sharpe_ratio",
        "actual_profit_factor",
        "placebo_median_profit_factor",
        "actual_percentile_profit_factor",
        "empirical_p_value_profit_factor",
    ]

    print(
        placebo_summary[
            placebo_display_columns
        ]
        .sort_values(
            [
                "period",
                "sleeve",
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