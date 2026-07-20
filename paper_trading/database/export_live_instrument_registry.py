from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from paper_trading.database.init_database import (
    DEFAULT_DATABASE_PATH,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]

OUTPUT_PATH = (
    PROJECT_ROOT
    / "strategy"
    / "config"
    / "intraday"
    / "live_instrument_registry.csv"
)


def parse_venue(
    symbol: str,
) -> str:
    if ":" not in symbol:
        return ""

    return symbol.split(
        ":",
        maxsplit=1,
    )[0]


def export_registry_template(
    database_path: Path = DEFAULT_DATABASE_PATH,
    output_path: Path = OUTPUT_PATH,
) -> pd.DataFrame:
    if not database_path.exists():
        raise FileNotFoundError(
            f"Missing paper-trading database: "
            f"{database_path}"
        )

    with sqlite3.connect(
        database_path
    ) as connection:
        relationships = pd.read_sql_query(
            """
            SELECT
                relationship_id,
                commodity,
                currency,
                relationship_type,
                fx_symbol AS historical_fx_symbol
            FROM relationships
            ORDER BY
                currency,
                commodity,
                relationship_id
            """,
            connection,
        )

    if relationships.empty:
        raise ValueError(
            "No relationships were found in "
            "the paper-trading database."
        )

    duplicate_count = (
        relationships[
            "relationship_id"
        ].duplicated().sum()
    )

    if duplicate_count != 0:
        raise ValueError(
            "Relationship registry contains "
            f"{duplicate_count} duplicate IDs."
        )

    registry = relationships.copy()

    # These fields must be connected to the exact
    # one-minute symbols used by the live collector.
    registry[
        "live_commodity_symbol"
    ] = ""

    registry[
        "commodity_venue"
    ] = ""

    registry[
        "commodity_timezone"
    ] = ""

    registry[
        "commodity_contract_mode"
    ] = "provider_continuous"

    # Default to the frozen FX symbol. This may be
    # replaced when the live collector uses a
    # different provider-specific symbol.
    registry[
        "live_fx_symbol"
    ] = registry[
        "historical_fx_symbol"
    ]

    registry[
        "fx_venue"
    ] = registry[
        "historical_fx_symbol"
    ].map(parse_venue)

    registry[
        "fx_timezone"
    ] = "UTC"

    # Use "inverse" when the collected pair is the
    # reciprocal of the strategy representation,
    # for example USDCAD instead of CADUSD.
    registry[
        "fx_price_transform"
    ] = "identity"

    # Must be explicitly confirmed for every
    # commodity-to-FX relationship.
    registry[
        "fx_direction_multiplier"
    ] = pd.Series(
        [pd.NA] * len(registry),
        dtype="Int64",
    )

    registry[
        "market_source_name"
    ] = ""

    registry[
        "active"
    ] = 1

    registry[
        "notes"
    ] = ""

    column_order = [
        "relationship_id",
        "commodity",
        "currency",
        "relationship_type",
        "historical_fx_symbol",
        "live_commodity_symbol",
        "commodity_venue",
        "commodity_timezone",
        "commodity_contract_mode",
        "live_fx_symbol",
        "fx_venue",
        "fx_timezone",
        "fx_price_transform",
        "fx_direction_multiplier",
        "market_source_name",
        "active",
        "notes",
    ]

    registry = registry[
        column_order
    ]

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    registry.to_csv(
        output_path,
        index=False,
    )

    return registry


def main() -> None:
    registry = (
        export_registry_template()
    )

    print(
        "Live-instrument registry template "
        "created successfully."
    )

    print(
        f"Output: {OUTPUT_PATH}"
    )

    print(
        f"Relationships: {len(registry)}"
    )

    print(
        "Unique commodities: "
        f"{registry['commodity'].nunique()}"
    )

    print(
        "Unique FX symbols: "
        f"{registry['historical_fx_symbol'].nunique()}"
    )

    print(
        "Commodity symbols requiring mapping: "
        f"{registry['live_commodity_symbol'].eq('').sum()}"
    )

    print(
        "Direction multipliers requiring mapping: "
        f"{registry['fx_direction_multiplier'].isna().sum()}"
    )


if __name__ == "__main__":
    main()
