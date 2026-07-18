from __future__ import annotations

import hashlib
import json
from datetime import date
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
    "frozen_date": date.today().isoformat(),

    "information_layers": {
        "market": True,
        "fundamentals": True,
        "news_sentiment": True,
    },

    "divergence_enabled": True,
    "position_sizing": "signal_volatility",

    "relationship_selection": {
        "method": "rolling_soft_weight",
        "lookback_years": 3,
        "update_frequency": "annual",
        "minimum_trailing_trades": 20,
        "minimum_trailing_net_return_pct": 0.0,
        "minimum_trailing_profit_factor": 1.0,
        "qualified_relationship_weight": 1.0,
        "weak_relationship_weight": 0.5,
        "selection_round_trip_cost_bps": 2.0,
    },

    "backtest_assumptions": {
        "round_trip_cost_bps": 2.0,
    },

    "research_status": {
        "primary_model": "market_fundamentals_news",
        "benchmark_model": "market_fundamentals",
        "historical_tuning_complete": True,
        "next_stage": "paper_trading",
        "research_holdout_is_no_longer_untouched": True,
    },
}


SOURCE_FILES = [
    "strategy/signals/build_signals.py",
    "strategy/divergence/build_divergence.py",
    "strategy/rules/build_trade_rules.py",
    "strategy/sizing/build_position_sizes.py",
    "strategy/backtest/run_backtest.py",
    "strategy/experiments/run_rolling_relationship_selection.py",
    "strategy/experiments/run_final_layer_ablation.py",
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
                f"Missing frozen source file: {absolute_path}"
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