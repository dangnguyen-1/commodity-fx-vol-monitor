from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]

OUTPUT_PATH = (
    PROJECT_ROOT
    / "strategy"
    / "config"
    / "frozen_strategy_manifest.json"
)


STRATEGY_CONFIGURATION: dict[str, Any] = {
    "strategy_name": "commodity_fx_macro",
    "strategy_version": "1.0.0",
    "frozen_date": "2026-07-18",
    "release_status": "frozen_for_paper_trading",

    "information_layers": {
        "market": True,
        "fundamentals": True,
        "news_sentiment": True,
    },

    "divergence_enabled": True,
    "position_sizing": "signal_volatility",

    "relationship_selection": {
        "method": "rolling_soft_weight",
        "schedule_path": (
            "strategy/config/v1/"
            "rolling_selection_schedule.csv"
        ),
        "lookback_years": 2,
        "update_frequency": "annual",
        "minimum_trailing_trades": 20,
        "minimum_trailing_net_return_pct": 0.0,
        "minimum_trailing_profit_factor": 1.0,
        "qualified_relationship_weight": 1.0,
        "weak_relationship_weight": 0.5,
        "selection_round_trip_cost_bps": 2.0,
    },

    "execution": {
        "signal_frequency": "daily",
        "entry_timing": "next_available_fx_open",
        "additional_entry_delay_days": 0,
        "delayed_signal_policy": (
            "revalidate_and_cancel_if_stale"
        ),
    },

    "backtest_assumptions": {
        "round_trip_cost_bps": 2.0,
        "cost_failure_scenario_bps": 5.0,
    },

    "historical_evidence": {
        "validation": {
            "period_start": "2019-01-01",
            "period_end": "2022-12-30",
            "total_return_pct": -0.076310,
            "sharpe_ratio": -0.081521,
            "max_drawdown_pct": -0.517116,
            "profit_factor": 0.983286,
            "total_trades": 1978,
        },

        "research_holdout": {
            "period_start": "2023-01-02",
            "period_end": "2026-07-13",
            "total_return_pct": 0.274088,
            "sharpe_ratio": 0.460519,
            "max_drawdown_pct": -0.267026,
            "profit_factor": 1.093159,
            "total_trades": 1783,
        },

        "block_bootstrap": {
            "replications": 2000,
            "research_holdout_return_ci_2_5_pct": (
                -0.349266
            ),
            "research_holdout_return_ci_97_5_pct": (
                0.927024
            ),
            "probability_positive_return": 0.8050,
            "probability_positive_sharpe": 0.8050,
            "probability_profit_factor_above_one": (
                0.8100
            ),
        },

        "final_timing_placebo": {
            "replications": 500,
            "minimum_shift_observations": 63,
            "return_percentile": 97.2,
            "return_empirical_p_value": 0.029940,
            "sharpe_percentile": 96.8,
            "sharpe_empirical_p_value": 0.033932,
            "profit_factor_percentile": 97.8,
            "profit_factor_empirical_p_value": (
                0.023952
            ),
        },

        "execution_stress": {
            "zero_delay_5bps_holdout_return_pct": (
                -0.033764
            ),
            "one_day_delay_2bps_holdout_return_pct": (
                -0.114868
            ),
        },

        "concentration_stress": {
            "holdout_return_without_brl_pct": (
                -0.124513
            ),
            "holdout_return_without_coffee_brl_pct": (
                -0.060159
            ),
            "holdout_return_without_corn_brl_pct": (
                -0.046004
            ),
        },
    },

    "known_limitations": [
        "Validation performance is negative after costs.",
        (
            "The historical edge disappears near "
            "5 bps round-trip transaction costs."
        ),
        (
            "The historical edge does not survive one "
            "additional trading day of entry delay."
        ),
        (
            "Research-holdout performance is materially "
            "dependent on the BRL sleeve."
        ),
        (
            "Coffee-BRL and Corn-BRL are material "
            "research-holdout return contributors."
        ),
        (
            "The research holdout has been inspected "
            "repeatedly and is not an untouched test set."
        ),
        (
            "Block-bootstrap confidence intervals for "
            "the full portfolio cross zero."
        ),
        (
            "Historical results do not constitute "
            "approval for live capital."
        ),
    ],

    "research_status": {
        "primary_model": "market_fundamentals_news",
        "benchmark_model": "market_fundamentals",
        "historical_tuning_complete": True,
        "next_stage": "paper_trading",
        "live_capital_approved": False,
        "research_holdout_is_no_longer_untouched": True,
        "post_freeze_strategy_changes_require_v2": True,
    },
}


SOURCE_FILES = [
    "strategy/signals/build_signals.py",
    "strategy/divergence/build_divergence.py",
    "strategy/rules/build_trade_rules.py",
    "strategy/sizing/build_position_sizes.py",
    "strategy/backtest/run_backtest.py",
    (
        "strategy/experiments/"
        "run_rolling_relationship_selection.py"
    ),
    "strategy/experiments/run_final_layer_ablation.py",
    "strategy/config/v1/rolling_selection_schedule.csv",
]


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()

    with path.open("rb") as file:
        for block in iter(
            lambda: file.read(1024 * 1024),
            b"",
        ):
            hasher.update(block)

    return hasher.hexdigest()


def build_source_manifest() -> list[dict[str, Any]]:
    records = []

    for relative_path in SOURCE_FILES:
        absolute_path = PROJECT_ROOT / relative_path

        if not absolute_path.exists():
            raise FileNotFoundError(
                "Missing frozen source file: "
                f"{absolute_path}"
            )

        records.append(
            {
                "path": relative_path,
                "sha256": sha256_file(
                    absolute_path
                ),
                "size_bytes": (
                    absolute_path.stat().st_size
                ),
            }
        )

    return records


def main() -> None:
    manifest = {
        "configuration": STRATEGY_CONFIGURATION,
        "source_files": build_source_manifest(),
    }

    OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    OUTPUT_PATH.write_text(
        json.dumps(
            manifest,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        "Frozen strategy manifest created:"
        f"\n{OUTPUT_PATH}"
    )

    print(
        "\nFrozen source files:"
    )

    for record in manifest["source_files"]:
        print(
            f"{record['sha256'][:12]}  "
            f"{record['path']}"
        )


if __name__ == "__main__":
    main()
