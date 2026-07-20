from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from paper_trading.database.init_database import (
    DEFAULT_DATABASE_PATH,
    configure_connection,
    initialize_database,
)
from strategy.config.intraday.load_intraday_spec import (
    load_intraday_spec,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


REQUIRED_SCHEDULE_COLUMNS = [
    "selection_year",
    "relationship_id",
    "commodity",
    "currency",
    "fx_symbol",
    "relationship_type",
    "selected",
    "rolling_soft_weight_weight",
    "trailing_trades",
    "trailing_net_return_on_notional_pct",
    "trailing_net_profit_factor",
]


RELATIONSHIP_METADATA_COLUMNS = [
    "relationship_id",
    "commodity",
    "currency",
    "fx_symbol",
    "relationship_type",
]


def utc_now_iso() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def project_relative_path(
    path: Path,
) -> str:
    try:
        return str(
            path.resolve().relative_to(
                PROJECT_ROOT.resolve()
            )
        )
    except ValueError:
        return str(path.resolve())


def parse_fx_symbol(
    symbol: str,
) -> tuple[
    str | None,
    str | None,
    str | None,
]:
    """
    Parse symbols such as:

    FX:AUDUSD
    FX:EURUSD
    FX_IDC:BRLUSD
    DERIVED:CADUSD
    """
    if ":" in symbol:
        venue, pair = symbol.split(
            ":",
            maxsplit=1,
        )
    else:
        venue = None
        pair = symbol

    clean_pair = "".join(
        character
        for character in pair.upper()
        if character.isalpha()
    )

    if len(clean_pair) == 6:
        base_currency = clean_pair[:3]
        quote_currency = clean_pair[3:]
    else:
        base_currency = None
        quote_currency = None

    return (
        venue,
        base_currency,
        quote_currency,
    )


def load_schedule(
    schedule_path: Path,
) -> pd.DataFrame:
    if not schedule_path.exists():
        raise FileNotFoundError(
            "Frozen relationship schedule does "
            f"not exist: {schedule_path}"
        )

    schedule = pd.read_csv(
        schedule_path
    )

    missing_columns = sorted(
        set(REQUIRED_SCHEDULE_COLUMNS)
        - set(schedule.columns)
    )

    if missing_columns:
        raise ValueError(
            "Frozen relationship schedule is "
            f"missing columns: {missing_columns}"
        )

    schedule = schedule[
        REQUIRED_SCHEDULE_COLUMNS
    ].copy()

    schedule[
        "selection_year"
    ] = pd.to_numeric(
        schedule["selection_year"],
        errors="raise",
    ).astype(int)

    schedule[
        "selected"
    ] = pd.to_numeric(
        schedule["selected"],
        errors="raise",
    ).astype(int)

    schedule[
        "rolling_soft_weight_weight"
    ] = pd.to_numeric(
        schedule[
            "rolling_soft_weight_weight"
        ],
        errors="raise",
    ).astype(float)

    schedule[
        "trailing_trades"
    ] = pd.to_numeric(
        schedule["trailing_trades"],
        errors="raise",
    ).astype(int)

    for column in [
        "trailing_net_return_on_notional_pct",
        "trailing_net_profit_factor",
    ]:
        schedule[column] = pd.to_numeric(
            schedule[column],
            errors="coerce",
        )

    duplicate_count = schedule.duplicated(
        [
            "selection_year",
            "relationship_id",
        ]
    ).sum()

    if duplicate_count != 0:
        raise ValueError(
            "Frozen schedule contains "
            f"{duplicate_count} duplicate "
            "year/relationship rows."
        )

    if not schedule[
        "selected"
    ].isin([0, 1]).all():
        raise ValueError(
            "selected must contain only 0 or 1."
        )

    invalid_weights = (
        schedule[
            "rolling_soft_weight_weight"
        ].lt(0)
        | schedule[
            "rolling_soft_weight_weight"
        ].gt(1)
    )

    if invalid_weights.any():
        raise ValueError(
            "Selection weights must remain "
            "between 0 and 1."
        )

    for column in [
        "relationship_id",
        "commodity",
        "currency",
        "fx_symbol",
        "relationship_type",
    ]:
        if schedule[column].isna().any():
            raise ValueError(
                f"{column} contains missing values."
            )

        schedule[column] = (
            schedule[column]
            .astype(str)
            .str.strip()
        )

        if schedule[column].eq("").any():
            raise ValueError(
                f"{column} contains empty values."
            )

    return schedule


def build_relationship_metadata(
    schedule: pd.DataFrame,
) -> pd.DataFrame:
    consistency = (
        schedule.groupby(
            "relationship_id"
        )[
            [
                "commodity",
                "currency",
                "fx_symbol",
                "relationship_type",
            ]
        ]
        .nunique(
            dropna=False
        )
    )

    inconsistent = consistency.loc[
        consistency.gt(1).any(axis=1)
    ]

    if not inconsistent.empty:
        raise ValueError(
            "Relationship metadata changes "
            "across schedule years for: "
            + ", ".join(
                inconsistent.index.astype(str)
            )
        )

    metadata = (
        schedule[
            RELATIONSHIP_METADATA_COLUMNS
        ]
        .drop_duplicates(
            subset=["relationship_id"]
        )
        .sort_values(
            "relationship_id"
        )
        .reset_index(
            drop=True
        )
    )

    return metadata


def upsert_fx_instruments(
    connection: sqlite3.Connection,
    metadata: pd.DataFrame,
    *,
    now: str,
) -> int:
    records = (
        metadata[
            [
                "fx_symbol",
                "currency",
            ]
        ]
        .drop_duplicates(
            subset=["fx_symbol"]
        )
        .sort_values(
            "fx_symbol"
        )
    )

    for row in records.itertuples(
        index=False
    ):
        (
            venue,
            base_currency,
            quote_currency,
        ) = parse_fx_symbol(
            row.fx_symbol
        )

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
                ?, ?, 'fx', ?, ?, ?, ?, ?, 1, ?, ?
            )
            ON CONFLICT(symbol) DO UPDATE SET
                instrument_name = excluded.instrument_name,
                instrument_type = excluded.instrument_type,
                venue = excluded.venue,
                base_currency = excluded.base_currency,
                quote_currency = excluded.quote_currency,
                source_name = excluded.source_name,
                active = 1,
                updated_at_utc = excluded.updated_at_utc
            """,
            (
                row.fx_symbol,
                row.fx_symbol,
                venue,
                base_currency,
                quote_currency,
                "UTC",
                "frozen_relationship_schedule",
                now,
                now,
            ),
        )

    return len(records)


def upsert_relationships(
    connection: sqlite3.Connection,
    metadata: pd.DataFrame,
    *,
    now: str,
) -> int:
    for row in metadata.itertuples(
        index=False
    ):
        connection.execute(
            """
            INSERT INTO relationships (
                relationship_id,
                commodity,
                currency,
                commodity_symbol,
                fx_symbol,
                relationship_type,
                fx_direction_multiplier,
                active,
                created_at_utc,
                updated_at_utc
            )
            VALUES (
                ?, ?, ?, NULL, ?, ?, NULL, 1, ?, ?
            )
            ON CONFLICT(relationship_id) DO UPDATE SET
                commodity = excluded.commodity,
                currency = excluded.currency,
                fx_symbol = excluded.fx_symbol,
                relationship_type = excluded.relationship_type,
                active = 1,
                updated_at_utc = excluded.updated_at_utc
            """,
            (
                row.relationship_id,
                row.commodity,
                row.currency,
                row.fx_symbol,
                row.relationship_type,
                now,
                now,
            ),
        )

    return len(metadata)


def upsert_relationship_weights(
    connection: sqlite3.Connection,
    schedule: pd.DataFrame,
    *,
    schedule_path: Path,
    now: str,
) -> int:
    stored_schedule_path = (
        project_relative_path(
            schedule_path
        )
    )

    for row in schedule.itertuples(
        index=False
    ):
        trailing_return = (
            None
            if pd.isna(
                row.trailing_net_return_on_notional_pct
            )
            else float(
                row.trailing_net_return_on_notional_pct
            )
        )

        trailing_profit_factor = (
            None
            if pd.isna(
                row.trailing_net_profit_factor
            )
            else float(
                row.trailing_net_profit_factor
            )
        )

        connection.execute(
            """
            INSERT INTO relationship_weights (
                selection_year,
                relationship_id,
                selected,
                selection_weight,
                trailing_trades,
                trailing_net_return_pct,
                trailing_profit_factor,
                source_schedule_path,
                loaded_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                selection_year,
                relationship_id
            ) DO UPDATE SET
                selected = excluded.selected,
                selection_weight = excluded.selection_weight,
                trailing_trades = excluded.trailing_trades,
                trailing_net_return_pct =
                    excluded.trailing_net_return_pct,
                trailing_profit_factor =
                    excluded.trailing_profit_factor,
                source_schedule_path =
                    excluded.source_schedule_path,
                loaded_at_utc =
                    excluded.loaded_at_utc
            """,
            (
                int(row.selection_year),
                row.relationship_id,
                int(row.selected),
                float(
                    row.rolling_soft_weight_weight
                ),
                int(row.trailing_trades),
                trailing_return,
                trailing_profit_factor,
                stored_schedule_path,
                now,
            ),
        )

    return len(schedule)


def load_relationships(
    database_path: Path = (
        DEFAULT_DATABASE_PATH
    ),
) -> None:
    initialize_database(
        database_path=database_path
    )

    spec = load_intraday_spec()

    schedule_path = Path(
        spec["_runtime"][
            "relationship_metadata_path"
        ]
    )

    schedule = load_schedule(
        schedule_path
    )

    metadata = (
        build_relationship_metadata(
            schedule
        )
    )

    now = utc_now_iso()

    with sqlite3.connect(
        database_path
    ) as connection:
        configure_connection(
            connection
        )

        fx_instrument_count = (
            upsert_fx_instruments(
                connection,
                metadata,
                now=now,
            )
        )

        relationship_count = (
            upsert_relationships(
                connection,
                metadata,
                now=now,
            )
        )

        weight_count = (
            upsert_relationship_weights(
                connection,
                schedule,
                schedule_path=schedule_path,
                now=now,
            )
        )

        connection.commit()

        foreign_key_errors = (
            connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
        )

        if foreign_key_errors:
            raise RuntimeError(
                "Foreign-key validation failed: "
                f"{foreign_key_errors[:10]}"
            )

    latest_year = int(
        schedule[
            "selection_year"
        ].max()
    )

    latest = schedule.loc[
        schedule[
            "selection_year"
        ].eq(latest_year)
    ]

    print(
        "Frozen relationship schedule loaded "
        "successfully."
    )

    print(
        f"Database: {database_path}"
    )

    print(
        f"Schedule: {schedule_path}"
    )

    print(
        f"FX instruments: "
        f"{fx_instrument_count}"
    )

    print(
        f"Relationships: "
        f"{relationship_count}"
    )

    print(
        f"Annual weight rows: "
        f"{weight_count}"
    )

    print(
        f"Schedule years: "
        f"{schedule['selection_year'].min()}-"
        f"{schedule['selection_year'].max()}"
    )

    print(
        f"Latest year: {latest_year}"
    )

    print(
        "Latest-year qualified "
        "relationships: "
        f"{int(latest['selected'].sum())}"
    )

    print(
        "Latest-year weak relationships: "
        f"{int(latest['selected'].eq(0).sum())}"
    )

    print(
        "\nPending live metadata:"
    )

    print(
        f"  Commodity symbols: "
        f"{relationship_count}"
    )

    print(
        f"  FX direction multipliers: "
        f"{relationship_count}"
    )


def main() -> None:
    load_relationships()


if __name__ == "__main__":
    main()
