from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

import psycopg2
import psycopg2.extras


class MarketDatabaseUnavailable(RuntimeError):
    """Raised when the raw market/fundamental Postgres database can't be reached."""


def get_database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise MarketDatabaseUnavailable("DATABASE_URL is not configured.")
    return url


@contextmanager
def read_connection() -> Iterator[psycopg2.extensions.cursor]:
    """Read-only cursor onto the market_data/fundamental_trade_data/news_*
    tables collected by data_collector/ — a different database from the
    paper-trading SQLite one in database.py (that one holds the strategy's
    own derived state; this one holds the raw collected data it's built
    from). Session is set read-only at the Postgres level, mirroring the
    `PRAGMA query_only = ON` guarantee database.py gives the SQLite side.
    """
    try:
        connection = psycopg2.connect(get_database_url(), connect_timeout=10)
    except psycopg2.Error as exc:
        raise MarketDatabaseUnavailable(f"Could not open market database: {exc}") from exc

    try:
        connection.set_session(readonly=True, autocommit=True)
        with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            yield cursor
    finally:
        connection.close()


def rows_to_dicts(rows: Sequence[Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
