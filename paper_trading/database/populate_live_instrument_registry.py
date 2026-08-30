from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from paper_trading.database.init_database import (
    DEFAULT_DATABASE_PATH,
    configure_connection,
    initialize_database,
)
from strategy.config.asset_fx_mapping import (
    CANDIDATE_ASSET_FX_MAPPINGS,
)
from strategy.config.market_symbols import (
    COMMODITY_MARKET_SYMBOLS,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]

REGISTRY_PATH = (
    PROJECT_ROOT
    / "strategy"
    / "config"
    / "intraday"
    / "live_instrument_registry.csv"
)

COLLECTOR_SYMBOL_PATH = (
    PROJECT_ROOT
    / "data_collector"
    / "market_data"
    / "config"
    / "symbols.js"
)


def utc_now_iso() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def parse_venue(symbol: str) -> str:
    if ":" not in symbol:
        return "UNKNOWN"

    return symbol.split(
        ":",
        maxsplit=1,
    )[0]


def load_collector_symbols(
    path: Path,
) -> set[str]:
    if not path.exists():
        raise FileNotFoundError(
            "Collector symbol configuration "
            f"does not exist: {path}"
        )

    text = path.read_text(
        encoding="utf-8"
    )

    symbols = set(
        re.findall(
            r"['\"]([^'\"]+:[^'\"]+)['\"]",
            text,
        )
    )

    if not symbols:
        raise ValueError(
            "No provider symbols were found in "
            f"{path}."
        )

    return symbols


def build_mapping_lookup() -> dict[
    tuple[str, str, str],
    dict[str, Any],
]:
    lookup: dict[
        tuple[str, str, str],
        dict[str, Any],
    ] = {}

    for mapping in (
        CANDIDATE_ASSET_FX_MAPPINGS
    ):
        key = (
            str(mapping["commodity"]).strip(),
            str(mapping["currency"]).strip(),
            str(mapping["fx_symbol"]).strip(),
        )

        if key in lookup:
            raise ValueError(
                "Duplicate research mapping: "
                f"{key}"
            )

        lookup[key] = mapping

    return lookup


def resolve_live_fx_symbol(
    canonical_fx_symbol: str,
) -> tuple[str, str, str]:
    """
    Return:

    live symbol,
    price transform,
    explanatory note
    """
    if canonical_fx_symbol == (
        "DERIVED:CADUSD"
    ):
        return (
            "FX:USDCAD",
            "inverse",
            (
                "Normalize collected USDCAD "
                "to canonical CADUSD using 1 / price."
            ),
        )

    return (
        canonical_fx_symbol,
        "identity",
        "",
    )


def contract_mode(
    symbol: str,
) -> str:
    if symbol.endswith("1!"):
        return "provider_continuous"

    return "proxy"


def build_completed_registry(
    registry: pd.DataFrame,
    collector_symbols: set[str],
) -> pd.DataFrame:
    required_columns = {
        "relationship_id",
        "commodity",
        "currency",
        "relationship_type",
        "historical_fx_symbol",
    }

    missing = sorted(
        required_columns
        - set(registry.columns)
    )

    if missing:
        raise ValueError(
            "Registry template is missing "
            f"columns: {missing}"
        )

    mapping_lookup = (
        build_mapping_lookup()
    )

    completed_rows: list[
        dict[str, Any]
    ] = []

    for row in registry.itertuples(
        index=False
    ):
        key = (
            str(row.commodity).strip(),
            str(row.currency).strip(),
            str(
                row.historical_fx_symbol
            ).strip(),
        )

        mapping = mapping_lookup.get(key)

        if mapping is None:
            raise ValueError(
                "No research mapping exists for "
                f"{key}."
            )

        if row.commodity not in (
            COMMODITY_MARKET_SYMBOLS
        ):
            raise ValueError(
                "No commodity market symbol "
                f"exists for {row.commodity}."
            )

        commodity_symbol = str(
            COMMODITY_MARKET_SYMBOLS[
                row.commodity
            ]
        ).strip()

        (
            live_fx_symbol,
            fx_transform,
            fx_note,
        ) = resolve_live_fx_symbol(
            row.historical_fx_symbol
        )

        if commodity_symbol not in (
            collector_symbols
        ):
            raise ValueError(
                "Commodity symbol is not present "
                "in the one-minute collector: "
                f"{commodity_symbol}"
            )

        if live_fx_symbol not in (
            collector_symbols
        ):
            raise ValueError(
                "FX symbol is not present in the "
                "one-minute collector: "
                f"{live_fx_symbol}"
            )

        direction = int(
            mapping["expected_sign"]
        )

        if direction not in (-1, 1):
            raise ValueError(
                "Invalid expected_sign for "
                f"{row.relationship_id}: "
                f"{direction}"
            )

        mode = contract_mode(
            commodity_symbol
        )

        notes = fx_note

        if mode == "proxy":
            proxy_note = (
                "Commodity exposure uses a "
                "non-continuous proxy instrument."
            )

            notes = (
                f"{notes} {proxy_note}"
            ).strip()

        completed_rows.append(
            {
                "relationship_id":
                    row.relationship_id,
                "commodity":
                    row.commodity,
                "currency":
                    row.currency,
                "relationship_type":
                    row.relationship_type,
                "historical_fx_symbol":
                    row.historical_fx_symbol,
                "live_commodity_symbol":
                    commodity_symbol,
                "commodity_venue":
                    parse_venue(
                        commodity_symbol
                    ),
                "commodity_timezone":
                    "UTC",
                "commodity_contract_mode":
                    mode,
                "live_fx_symbol":
                    live_fx_symbol,
                "fx_venue":
                    parse_venue(
                        live_fx_symbol
                    ),
                "fx_timezone":
                    "UTC",
                "fx_price_transform":
                    fx_transform,
                "fx_direction_multiplier":
                    direction,
                "market_source_name":
                    "tradingview",
                "active":
                    int(row.active),
                "notes":
                    notes,
            }
        )

    completed = pd.DataFrame(
        completed_rows
    )

    if len(completed) != len(registry):
        raise RuntimeError(
            "Completed registry row count "
            "does not match the template."
        )

    duplicate_ids = completed[
        "relationship_id"
    ].duplicated().sum()

    if duplicate_ids:
        raise ValueError(
            "Completed registry contains "
            f"{duplicate_ids} duplicate IDs."
        )

    return completed


def upsert_instrument(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    instrument_name: str,
    instrument_type: str,
    venue: str,
    source_name: str,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO instruments (
            symbol,
            instrument_name,
            instrument_type,
            venue,
            base_currency,
            quote_currency,
            timezone_name,
            source_name,
            active,
            created_at_utc,
            updated_at_utc
        )
        VALUES (
            ?, ?, ?, ?, NULL, NULL,
            'UTC', ?, 1, ?, ?
        )
        ON CONFLICT(symbol) DO UPDATE SET
            instrument_name =
                excluded.instrument_name,
            instrument_type =
                excluded.instrument_type,
            venue =
                excluded.venue,
            timezone_name =
                excluded.timezone_name,
            source_name =
                excluded.source_name,
            active = 1,
            updated_at_utc =
                excluded.updated_at_utc
        """,
        (
            symbol,
            instrument_name,
            instrument_type,
            venue,
            source_name,
            now,
            now,
        ),
    )


def load_completed_registry(
    completed: pd.DataFrame,
    *,
    database_path: Path,
) -> None:
    initialize_database(
        database_path=database_path
    )

    now = utc_now_iso()

    with sqlite3.connect(
        database_path
    ) as connection:
        configure_connection(
            connection
        )

        for row in completed.itertuples(
            index=False
        ):
            upsert_instrument(
                connection,
                symbol=(
                    row.live_commodity_symbol
                ),
                instrument_name=row.commodity,
                instrument_type="commodity",
                venue=row.commodity_venue,
                source_name=(
                    row.market_source_name
                ),
                now=now,
            )

            upsert_instrument(
                connection,
                symbol=row.live_fx_symbol,
                instrument_name=(
                    row.live_fx_symbol
                ),
                instrument_type="fx",
                venue=row.fx_venue,
                source_name=(
                    row.market_source_name
                ),
                now=now,
            )

            connection.execute(
                """
                UPDATE relationships
                SET
                    commodity_symbol = ?,
                    fx_direction_multiplier = ?,
                    updated_at_utc = ?
                WHERE relationship_id = ?
                """,
                (
                    row.live_commodity_symbol,
                    int(
                        row.fx_direction_multiplier
                    ),
                    now,
                    row.relationship_id,
                ),
            )

            connection.execute(
                """
                INSERT INTO
                    live_instrument_registry (
                        relationship_id,
                        live_commodity_symbol,
                        commodity_venue,
                        commodity_timezone,
                        commodity_contract_mode,
                        live_fx_symbol,
                        fx_venue,
                        fx_timezone,
                        fx_price_transform,
                        fx_direction_multiplier,
                        market_source_name,
                        active,
                        notes,
                        updated_at_utc
                    )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(
                    relationship_id
                ) DO UPDATE SET
                    live_commodity_symbol =
                        excluded.live_commodity_symbol,
                    commodity_venue =
                        excluded.commodity_venue,
                    commodity_timezone =
                        excluded.commodity_timezone,
                    commodity_contract_mode =
                        excluded.commodity_contract_mode,
                    live_fx_symbol =
                        excluded.live_fx_symbol,
                    fx_venue =
                        excluded.fx_venue,
                    fx_timezone =
                        excluded.fx_timezone,
                    fx_price_transform =
                        excluded.fx_price_transform,
                    fx_direction_multiplier =
                        excluded.fx_direction_multiplier,
                    market_source_name =
                        excluded.market_source_name,
                    active =
                        excluded.active,
                    notes =
                        excluded.notes,
                    updated_at_utc =
                        excluded.updated_at_utc
                """,
                (
                    row.relationship_id,
                    row.live_commodity_symbol,
                    row.commodity_venue,
                    row.commodity_timezone,
                    row.commodity_contract_mode,
                    row.live_fx_symbol,
                    row.fx_venue,
                    row.fx_timezone,
                    row.fx_price_transform,
                    int(
                        row.fx_direction_multiplier
                    ),
                    row.market_source_name,
                    int(row.active),
                    row.notes,
                    now,
                ),
            )

        connection.commit()

        unresolved = connection.execute(
            """
            SELECT
                SUM(
                    commodity_symbol IS NULL
                ),
                SUM(
                    fx_direction_multiplier IS NULL
                )
            FROM relationships
            """
        ).fetchone()

        registry_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM live_instrument_registry
            """
        ).fetchone()[0]

        foreign_key_errors = (
            connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
        )

        if unresolved != (0, 0):
            raise RuntimeError(
                "Relationship metadata remains "
                f"unresolved: {unresolved}"
            )

        if registry_count != len(completed):
            raise RuntimeError(
                "Stored live-registry count does "
                "not match the CSV."
            )

        if foreign_key_errors:
            raise RuntimeError(
                "Foreign-key validation failed: "
                f"{foreign_key_errors[:10]}"
            )


def main() -> None:
    if not REGISTRY_PATH.exists():
        raise FileNotFoundError(
            "Missing live registry template: "
            f"{REGISTRY_PATH}"
        )

    registry = pd.read_csv(
        REGISTRY_PATH
    )

    collector_symbols = (
        load_collector_symbols(
            COLLECTOR_SYMBOL_PATH
        )
    )

    completed = (
        build_completed_registry(
            registry,
            collector_symbols,
        )
    )

    completed.to_csv(
        REGISTRY_PATH,
        index=False,
    )

    load_completed_registry(
        completed,
        database_path=(
            DEFAULT_DATABASE_PATH
        ),
    )

    print(
        "Live-instrument registry populated "
        "successfully."
    )

    print(
        f"Registry: {REGISTRY_PATH}"
    )

    print(
        f"Relationships: {len(completed)}"
    )

    unique_commodity_symbols = completed[
        "live_commodity_symbol"
    ].nunique()
    print(
        "Unique commodity symbols: "
        f"{unique_commodity_symbols}"
    )

    unique_fx_symbols = completed[
        "live_fx_symbol"
    ].nunique()
    print(
        "Unique live FX symbols: "
        f"{unique_fx_symbols}"
    )

    inverse_fx_transforms = completed[
        "fx_price_transform"
    ].eq("inverse").sum()
    print(
        "Inverse FX transforms: "
        f"{inverse_fx_transforms}"
    )

    positive_direction_multipliers = completed[
        "fx_direction_multiplier"
    ].eq(1).sum()
    print(
        "Positive direction multipliers: "
        f"{positive_direction_multipliers}"
    )

    print(
        "Unresolved relationship metadata: 0"
    )


if __name__ == "__main__":
    main()
