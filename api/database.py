"""Read-only access to the collected data in Postgres.

Everything the dashboard shows comes from here: market_data,
fundamental_trade_data and the news tables, all written by data_collector/.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

import psycopg2
import psycopg2.extras


class DatabaseUnavailable(RuntimeError):
    """Raised when the collected-data database cannot be reached."""


def get_database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise DatabaseUnavailable("DATABASE_URL is not configured.")
    return url


@contextmanager
def read_connection() -> Iterator[psycopg2.extensions.cursor]:
    """A cursor with the session set read-only at the Postgres level.

    Read-only is enforced by the server rather than by convention, so a
    mistake in a query here cannot write to the collectors' data.
    """
    try:
        connection = psycopg2.connect(get_database_url(), connect_timeout=10)
    except psycopg2.Error as exc:
        raise DatabaseUnavailable(f"Could not open database: {exc}") from exc

    try:
        connection.set_session(readonly=True, autocommit=True)
        with connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cursor:
            yield cursor
    finally:
        connection.close()


def rows_to_dicts(rows: Sequence[Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
