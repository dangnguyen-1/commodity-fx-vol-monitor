from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from strategy.backtest.run_backtest import (
    INITIAL_CAPITAL,
    calculate_summary,
    load_candidates,
    load_fx_prices,
    run_event_backtest,
)


OUTPUT_DIR = Path("strategy/output/backtest")

BASE_ROUND_TRIP_COST_BPS = 2.0
EXIT_BUFFER_CALENDAR_DAYS = 30
EQUAL_POSITION_SIZE_PCT = 0.01

PERIODS = {
    "train_2010_2018": ("2010-01-01", "2018-12-31"),
    "validation_2019_2022": ("2019-01-01", "2022-12-31"),
    "test_2023_2026": ("2023-01-01", "2026-12-31"),
    "full_2010_2026": ("2010-01-01", "2026-12-31"),
}


@dataclass(frozen=True)
class ExperimentSpec:
    name: str
    family: str
    subset: str = "all"
    direction_mode: str = "original"
    sizing_mode: str = "current"
    holding_period_days: int | None = None
    round_trip_cost_bps: float = BASE_ROUND_TRIP_COST_BPS
    random_seed: int | None = None
    posthoc_exploratory: bool = False


def candidate_universe(candidates: pd.DataFrame) -> pd.DataFrame:
    """Keep rows that represent a directional trade rule before sizing filters."""
    mask = (
        (candidates["trade_candidate"] == 1)
        & (candidates["trade_direction"].isin([-1, 1]))
        & (candidates["default_holding_period_days"] >= 1)
    )
    return candidates.loc[mask].copy()


def apply_subset(df: pd.DataFrame, subset: str) -> pd.DataFrame:
    if subset == "all":
        mask = pd.Series(True, index=df.index)
    elif subset == "primary_only":
        mask = df["priority"].eq("primary")
    elif subset == "no_cad_brl":
        mask = ~df["currency"].isin(["CAD", "BRL"])
    elif subset == "aud_only":
        mask = df["currency"].eq("AUD")
    elif subset == "industrial_metals_only":
        mask = df["relationship_type"].eq("industrial_metal")
    elif subset == "no_generic_exporter":
        mask = ~df["relationship_type"].eq("exporter")
    elif subset == "industrial_or_china_demand":
        mask = df["relationship_type"].isin(
            ["industrial_metal", "exporter_china_demand"]
        )
    else:
        raise ValueError(f"Unknown subset: {subset}")

    return df.loc[mask].copy()


def apply_direction_mode(
    df: pd.DataFrame,
    mode: str,
    random_seed: int | None,
) -> pd.DataFrame:
    df = df.copy()

    if mode == "original":
        return df

    if mode == "long_only":
        return df[df["trade_direction"] == 1].copy()

    if mode == "short_only":
        return df[df["trade_direction"] == -1].copy()

    if mode == "force_long":
        df["trade_direction"] = 1
        return df

    if mode == "force_short":
        df["trade_direction"] = -1
        return df

    if random_seed is None:
        raise ValueError(f"A random seed is required for direction mode {mode!r}.")

    rng = np.random.default_rng(random_seed)

    if mode == "random":
        df["trade_direction"] = rng.choice([-1, 1], size=len(df))
        return df

    if mode == "shuffle":
        df["trade_direction"] = rng.permutation(
            df["trade_direction"].to_numpy()
        )
        return df

    raise ValueError(f"Unknown direction mode: {mode}")


def apply_sizing_mode(df: pd.DataFrame, mode: str) -> pd.DataFrame:
    df = df.copy()

    if mode == "current":
        return df[
            (df["has_position"] == 1)
            & (df["position_size_pct"] > 0)
        ].copy()

    if mode == "equal_1pct":
        df["has_position"] = 1
        df["position_size_pct"] = EQUAL_POSITION_SIZE_PCT
        return df

    raise ValueError(f"Unknown sizing mode: {mode}")


def prepare_experiment_candidates(
    candidates: pd.DataFrame,
    spec: ExperimentSpec,
) -> pd.DataFrame:
    prepared = candidate_universe(candidates)
    prepared = apply_subset(prepared, spec.subset)
    prepared = apply_direction_mode(
        prepared,
        spec.direction_mode,
        spec.random_seed,
    )
    prepared = apply_sizing_mode(prepared, spec.sizing_mode)

    if spec.holding_period_days is not None:
        prepared["default_holding_period_days"] = int(
            spec.holding_period_days
        )

    prepared["trade_candidate"] = 1

    return prepared.sort_values(
        ["date", "relationship_id"]
    ).reset_index(drop=True)


def slice_period_inputs(
    candidates: pd.DataFrame,
    fx_prices: pd.DataFrame,
    start_date: str,
    end_date: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]:
    start = pd.Timestamp(start_date)
    requested_end = pd.Timestamp(end_date)
    available_fx_end = pd.Timestamp(fx_prices["date"].max())
    nominal_end = min(requested_end, available_fx_end)

    period_candidates = candidates[
        (candidates["date"] >= start)
        & (candidates["date"] <= requested_end)
    ].copy()

    fx_slice_end = nominal_end + pd.Timedelta(
        days=EXIT_BUFFER_CALENDAR_DAYS
    )

    period_fx = fx_prices[
        (fx_prices["date"] >= start)
        & (fx_prices["date"] <= fx_slice_end)
    ].copy()

    if period_fx.empty:
        raise ValueError(
            f"No FX prices available for period {start_date} to {end_date}."
        )

    return period_candidates, period_fx, nominal_end


def trim_equity_to_relevant_end(
    equity: pd.DataFrame,
    trades: pd.DataFrame,
    nominal_end: pd.Timestamp,
) -> pd.DataFrame:
    if equity.empty:
        return equity

    relevant_end = nominal_end

    if not trades.empty:
        relevant_end = max(
            relevant_end,
            pd.Timestamp(trades["exit_date"].max()),
        )

    return equity[equity["date"] <= relevant_end].copy()


def run_one_experiment(
    base_candidates: pd.DataFrame,
    fx_prices: pd.DataFrame,
    spec: ExperimentSpec,
    period_name: str,
    start_date: str,
    end_date: str,
) -> dict:
    prepared = prepare_experiment_candidates(base_candidates, spec)
    period_candidates, period_fx, nominal_end = slice_period_inputs(
        prepared,
        fx_prices,
        start_date,
        end_date,
    )

    trades, equity, decisions = run_event_backtest(
        period_candidates,
        period_fx,
        initial_capital=INITIAL_CAPITAL,
        round_trip_cost_bps=spec.round_trip_cost_bps,
    )

    equity = trim_equity_to_relevant_end(
        equity,
        trades,
        nominal_end,
    )

    summary = calculate_summary(
        trades,
        equity,
        decisions,
        initial_capital=INITIAL_CAPITAL,
        round_trip_cost_bps=spec.round_trip_cost_bps,
    ).iloc[0].to_dict()

    approved_entries = (
        int((decisions["entry_decision"] == "approved").sum())
        if not decisions.empty
        else 0
    )

    summary.update(
        {
            "experiment": spec.name,
            "family": spec.family,
            "period": period_name,
            "requested_start_date": start_date,
            "requested_end_date": end_date,
            "subset": spec.subset,
            "direction_mode": spec.direction_mode,
            "sizing_mode": spec.sizing_mode,
            "holding_period_override": spec.holding_period_days,
            "round_trip_cost_bps": spec.round_trip_cost_bps,
            "random_seed": spec.random_seed,
            "posthoc_exploratory": spec.posthoc_exploratory,
            "candidate_rows": len(period_candidates),
            "approved_entries": approved_entries,
            "test_is_untouched": False,
        }
    )

    return summary


def core_specs() -> list[ExperimentSpec]:
    return [
        ExperimentSpec(
            name="v1_all_baseline",
            family="core",
        ),
        ExperimentSpec(
            name="v1_long_only",
            family="core",
            direction_mode="long_only",
            posthoc_exploratory=True,
        ),
        ExperimentSpec(
            name="v1_short_only",
            family="core",
            direction_mode="short_only",
            posthoc_exploratory=True,
        ),
        ExperimentSpec(
            name="v1_primary_only",
            family="core",
            subset="primary_only",
            posthoc_exploratory=True,
        ),
        ExperimentSpec(
            name="v1_no_cad_brl",
            family="core",
            subset="no_cad_brl",
            posthoc_exploratory=True,
        ),
        ExperimentSpec(
            name="v1_no_generic_exporter",
            family="core",
            subset="no_generic_exporter",
            posthoc_exploratory=True,
        ),
        ExperimentSpec(
            name="v1_industrial_metals_only",
            family="core",
            subset="industrial_metals_only",
            posthoc_exploratory=True,
        ),
        ExperimentSpec(
            name="v1_industrial_or_china_demand",
            family="core",
            subset="industrial_or_china_demand",
            posthoc_exploratory=True,
        ),
    ]


def sensitivity_specs() -> list[ExperimentSpec]:
    specs: list[ExperimentSpec] = []

    for cost_bps in [0.0, 2.0, 5.0, 10.0]:
        specs.append(
            ExperimentSpec(
                name=f"cost_{cost_bps:g}bps",
                family="cost_sensitivity",
                round_trip_cost_bps=cost_bps,
            )
        )

    for holding_days in [1, 3, 5]:
        specs.append(
            ExperimentSpec(
                name=f"hold_{holding_days}d",
                family="holding_sensitivity",
                holding_period_days=holding_days,
            )
        )

    specs.extend(
        [
            ExperimentSpec(
                name="sizing_current_signal_vol",
                family="sizing_sensitivity",
                sizing_mode="current",
            ),
            ExperimentSpec(
                name="sizing_equal_1pct",
                family="sizing_sensitivity",
                sizing_mode="equal_1pct",
            ),
        ]
    )

    return specs


def control_specs() -> list[ExperimentSpec]:
    specs = [
        ExperimentSpec(
            name="control_force_long_same_candidates",
            family="control",
            direction_mode="force_long",
        ),
        ExperimentSpec(
            name="control_force_short_same_candidates",
            family="control",
            direction_mode="force_short",
        ),
    ]

    for seed in [1, 2, 3]:
        specs.append(
            ExperimentSpec(
                name=f"control_random_direction_seed_{seed}",
                family="control",
                direction_mode="random",
                random_seed=seed,
            )
        )
        specs.append(
            ExperimentSpec(
                name=f"control_shuffled_direction_seed_{seed}",
                family="control",
                direction_mode="shuffle",
                random_seed=seed,
            )
        )

    return specs


def build_core_comparison(all_runs: pd.DataFrame) -> pd.DataFrame:
    core = all_runs[all_runs["family"] == "core"].copy()

    metrics = [
        "total_trades",
        "total_pnl_usd",
        "sharpe_ratio",
        "max_drawdown_pct",
        "win_rate_pct",
        "profit_factor",
    ]

    wide_parts = []

    for metric in metrics:
        pivot = core.pivot(
            index="experiment",
            columns="period",
            values=metric,
        )
        pivot.columns = [f"{period}_{metric}" for period in pivot.columns]
        wide_parts.append(pivot)

    comparison = pd.concat(wide_parts, axis=1).reset_index()

    metadata = (
        core[
            [
                "experiment",
                "subset",
                "direction_mode",
                "sizing_mode",
                "posthoc_exploratory",
                "test_is_untouched",
            ]
        ]
        .drop_duplicates("experiment")
    )

    comparison = comparison.merge(metadata, on="experiment", how="left")

    pnl_cols = [
        "train_2010_2018_total_pnl_usd",
        "validation_2019_2022_total_pnl_usd",
        "test_2023_2026_total_pnl_usd",
    ]

    comparison["positive_all_periods"] = (
        comparison[pnl_cols].gt(0).all(axis=1)
    )
    comparison["min_period_pnl_usd"] = comparison[pnl_cols].min(axis=1)

    full_pnl_col = "full_2010_2026_total_pnl_usd"
    test_pnl_col = "test_2023_2026_total_pnl_usd"

    comparison = comparison.sort_values(
        ["positive_all_periods", full_pnl_col, test_pnl_col],
        ascending=[False, False, False],
    )

    return comparison


def run_specs(
    candidates: pd.DataFrame,
    fx_prices: pd.DataFrame,
    specs: list[ExperimentSpec],
    periods: dict[str, tuple[str, str]],
) -> list[dict]:
    total_runs = len(specs) * len(periods)
    run_number = 0
    rows: list[dict] = []

    for spec in specs:
        for period_name, (start_date, end_date) in periods.items():
            run_number += 1
            print(
                f"[{run_number}/{total_runs}] "
                f"{spec.name} | {period_name}"
            )

            rows.append(
                run_one_experiment(
                    candidates,
                    fx_prices,
                    spec,
                    period_name,
                    start_date,
                    end_date,
                )
            )

    return rows


def main() -> None:
    candidates = load_candidates()
    fx_prices = load_fx_prices()

    rows: list[dict] = []

    rows.extend(
        run_specs(
            candidates,
            fx_prices,
            core_specs(),
            PERIODS,
        )
    )

    rows.extend(
        run_specs(
            candidates,
            fx_prices,
            sensitivity_specs(),
            {"full_2010_2026": PERIODS["full_2010_2026"]},
        )
    )

    rows.extend(
        run_specs(
            candidates,
            fx_prices,
            control_specs(),
            {"full_2010_2026": PERIODS["full_2010_2026"]},
        )
    )

    all_runs = pd.DataFrame(rows)
    core_comparison = build_core_comparison(all_runs)

    sensitivity = all_runs[
        all_runs["family"].isin(
            [
                "cost_sensitivity",
                "holding_sensitivity",
                "sizing_sensitivity",
            ]
        )
    ].copy()

    controls = all_runs[all_runs["family"] == "control"].copy()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    all_runs.to_csv(
        OUTPUT_DIR / "validation_all_runs.csv",
        index=False,
    )
    core_comparison.to_csv(
        OUTPUT_DIR / "validation_core_summary.csv",
        index=False,
    )
    sensitivity.to_csv(
        OUTPUT_DIR / "validation_sensitivity.csv",
        index=False,
    )
    controls.to_csv(
        OUTPUT_DIR / "validation_controls.csv",
        index=False,
    )

    print(f"\nSaved validation outputs to {OUTPUT_DIR}")

    display_cols = [
        "experiment",
        "full_2010_2026_total_trades",
        "full_2010_2026_total_pnl_usd",
        "train_2010_2018_total_pnl_usd",
        "validation_2019_2022_total_pnl_usd",
        "test_2023_2026_total_pnl_usd",
        "full_2010_2026_sharpe_ratio",
        "positive_all_periods",
        "posthoc_exploratory",
    ]

    print("\nCore variants:")
    print(
        core_comparison[display_cols]
        .to_string(index=False)
    )

    sensitivity_display = [
        "experiment",
        "family",
        "total_trades",
        "total_pnl_usd",
        "sharpe_ratio",
        "max_drawdown_pct",
        "win_rate_pct",
        "profit_factor",
    ]

    print("\nSensitivity tests:")
    print(
        sensitivity[sensitivity_display]
        .sort_values(["family", "experiment"])
        .to_string(index=False)
    )

    print("\nPlacebo / direction controls:")
    print(
        controls[sensitivity_display]
        .sort_values("experiment")
        .to_string(index=False)
    )

    print(
        "\nImportant: the 2023-2026 period is not an untouched test set "
        "because several variants were motivated by full-sample diagnostics."
    )


if __name__ == "__main__":
    main()