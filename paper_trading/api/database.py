from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = (
    PROJECT_ROOT
    / "paper_trading"
    / "data"
    / "paper_trading.db"
)


class DatabaseUnavailable(RuntimeError):
    """Raised when the read-only API database cannot be opened."""


def get_database_path() -> Path:
    configured = os.getenv("PAPER_TRADING_DATABASE_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    return DEFAULT_DATABASE_PATH.resolve()


@contextmanager
def read_connection() -> Iterator[sqlite3.Connection]:
    path = get_database_path()
    if not path.exists():
        raise DatabaseUnavailable(
            f"Paper-trading database does not exist: {path}"
        )

    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=5.0,
            check_same_thread=False,
        )
    except sqlite3.Error as exc:
        raise DatabaseUnavailable(
            f"Could not open paper-trading database: {exc}"
        ) from exc

    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        yield connection
    finally:
        connection.close()


def parse_json(value: Any) -> Any:
    if value is None or not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def row_to_dict(
    row: sqlite3.Row | None,
    *,
    json_fields: Sequence[str] = (),
) -> dict[str, Any] | None:
    if row is None:
        return None

    result = dict(row)
    for field in json_fields:
        if field in result:
            result[field] = parse_json(result[field])
    return result


def rows_to_dicts(
    rows: Sequence[sqlite3.Row],
    *,
    json_fields: Sequence[str] = (),
) -> list[dict[str, Any]]:
    return [
        row_to_dict(row, json_fields=json_fields) or {}
        for row in rows
    ]


def resolve_run_id(
    connection: sqlite3.Connection,
    requested_run_id: str | None,
) -> str | None:
    if requested_run_id:
        row = connection.execute(
            "SELECT run_id FROM paper_runs WHERE run_id = ?",
            (requested_run_id,),
        ).fetchone()
        return str(row[0]) if row else None

    row = connection.execute(
        """
        SELECT run_id
        FROM paper_runs
        ORDER BY
            CASE status
                WHEN 'running' THEN 0
                WHEN 'paused' THEN 1
                WHEN 'created' THEN 2
                WHEN 'stopped' THEN 3
                ELSE 4
            END,
            started_at_utc DESC
        LIMIT 1
        """
    ).fetchone()
    return str(row[0]) if row else None
