from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from strategy.config.intraday.load_intraday_spec import (
    load_intraday_spec,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_SCHEMA_PATH = (
    PROJECT_ROOT
    / "paper_trading"
    / "database"
    / "schema.sql"
)

DEFAULT_DATABASE_PATH = (
    PROJECT_ROOT
    / "paper_trading"
    / "data"
    / "paper_trading.db"
)


SCHEMA_VERSION = "4"


def utc_now_iso() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def sha256_text(value: str) -> str:
    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()


def configure_connection(
    connection: sqlite3.Connection,
) -> None:
    connection.execute(
        "PRAGMA foreign_keys = ON"
    )

    connection.execute(
        "PRAGMA journal_mode = WAL"
    )

    connection.execute(
        "PRAGMA synchronous = NORMAL"
    )

    connection.execute(
        "PRAGMA busy_timeout = 5000"
    )


# Columns added to tables that already exist in deployed databases.
# schema.sql is all CREATE TABLE IF NOT EXISTS, which is what makes it safe
# to re-run -- but that same property means it silently does nothing to a
# table that is already there, so a new column never arrives. Each entry is
# applied only when absent, so this stays idempotent alongside the script.
COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("live_instrument_registry", "transmission_beta", "REAL"),
    ("live_instrument_registry", "transmission_beta_observations", "INTEGER"),
    ("live_instrument_registry", "transmission_beta_measured_at_utc", "TEXT"),
)


def apply_column_migrations(
    connection: sqlite3.Connection,
) -> list[str]:
    applied: list[str] = []

    for table, column, column_type in COLUMN_MIGRATIONS:
        existing = {
            str(row[1])
            for row in connection.execute(
                f"PRAGMA table_info({table})"
            )
        }
        if not existing:
            # Table does not exist in this database at all; the schema
            # script owns creating it and there is nothing to migrate.
            continue
        if column in existing:
            continue

        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"
        )
        applied.append(f"{table}.{column}")

    return applied


def initialize_database(
    database_path: Path = DEFAULT_DATABASE_PATH,
    schema_path: Path = DEFAULT_SCHEMA_PATH,
) -> Path:
    if not schema_path.exists():
        raise FileNotFoundError(
            f"Missing database schema: "
            f"{schema_path}"
        )

    spec = load_intraday_spec()

    spec_path = Path(
        spec["_runtime"][
            "specification_path"
        ]
    )

    spec_yaml = spec_path.read_text(
        encoding="utf-8"
    )

    spec_sha256 = sha256_text(
        spec_yaml
    )

    database_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    schema_sql = schema_path.read_text(
        encoding="utf-8"
    )

    with sqlite3.connect(
        database_path
    ) as connection:
        configure_connection(
            connection
        )

        connection.executescript(
            schema_sql
        )

        apply_column_migrations(
            connection
        )

        now = utc_now_iso()

        connection.execute(
            """
            INSERT INTO schema_metadata (
                key,
                value,
                updated_at_utc
            )
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at_utc = excluded.updated_at_utc
            """,
            (
                "schema_version",
                SCHEMA_VERSION,
                now,
            ),
        )

        connection.execute(
            """
            INSERT INTO strategy_specs (
                strategy_name,
                specification_version,
                status,
                spec_path,
                spec_sha256,
                spec_yaml,
                loaded_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(spec_sha256) DO NOTHING
            """,
            (
                spec["strategy"]["name"],
                spec["strategy"][
                    "specification_version"
                ],
                spec["strategy"]["status"],
                str(spec_path),
                spec_sha256,
                spec_yaml,
                now,
            ),
        )

        connection.commit()

        integrity_result = connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()

        if (
            integrity_result is None
            or integrity_result[0] != "ok"
        ):
            raise RuntimeError(
                "SQLite integrity check failed: "
                f"{integrity_result}"
            )

    return database_path


def get_table_names(
    database_path: Path,
) -> list[str]:
    with sqlite3.connect(
        database_path
    ) as connection:
        rows = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()

    return [
        row[0]
        for row in rows
    ]


def main() -> None:
    database_path = (
        initialize_database()
    )

    tables = get_table_names(
        database_path
    )

    print(
        "Paper-trading database initialized "
        "successfully."
    )

    print(
        f"Database: {database_path}"
    )

    print(
        f"Schema version: "
        f"{SCHEMA_VERSION}"
    )

    print(
        f"Tables: {len(tables)}"
    )

    for table in tables:
        print(
            f"  - {table}"
        )


if __name__ == "__main__":
    main()
