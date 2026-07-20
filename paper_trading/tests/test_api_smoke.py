from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from paper_trading.api.app import app
from paper_trading.api.database import DEFAULT_DATABASE_PATH


ENDPOINTS = [
    "/",
    "/health",
    "/services",
    "/strategy",
    "/runs/current",
    "/relationships",
    "/features/latest",
    "/signals/latest",
    "/positions?status=all",
    "/orders",
    "/fills",
    "/equity",
    "/alerts",
    "/summary",
]


def table_counts(path: Path) -> dict[str, int]:
    tables = [
        "paper_runs",
        "feature_snapshots",
        "signal_decisions",
        "orders",
        "fills",
        "positions",
        "equity_snapshots",
        "system_alerts",
    ]
    with sqlite3.connect(path) as connection:
        return {
            table: int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            )
            for table in tables
        }


def main() -> None:
    source = DEFAULT_DATABASE_PATH.resolve()
    if not source.exists():
        raise FileNotFoundError(
            f"Source paper-trading database not found: {source}"
        )

    temp_dir = Path(tempfile.mkdtemp(prefix="paper-api-test-"))
    test_database = temp_dir / "paper_trading.db"
    shutil.copy2(source, test_database)
    os.environ["PAPER_TRADING_DATABASE_PATH"] = str(test_database)

    before = table_counts(test_database)

    with TestClient(app) as client:
        for endpoint in ENDPOINTS:
            response = client.get(endpoint)
            if response.status_code != 200:
                raise AssertionError(
                    f"{endpoint} returned {response.status_code}: "
                    f"{response.text}"
                )
            payload = response.json()
            if not isinstance(payload, dict):
                raise AssertionError(
                    f"{endpoint} did not return a JSON object."
                )
            print(endpoint, "OK")

        missing_run = client.get(
            "/runs/current",
            params={"run_id": "does-not-exist"},
        )
        if missing_run.status_code != 404:
            raise AssertionError(
                "Unknown run_id did not return HTTP 404."
            )

    after = table_counts(test_database)
    if after != before:
        raise AssertionError(
            f"Read-only API changed database counts: {before} -> {after}"
        )

    with sqlite3.connect(test_database) as connection:
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

    if foreign_keys:
        raise AssertionError(
            f"Foreign-key errors found: {foreign_keys}"
        )

    print("Database counts unchanged:", before)
    print("Foreign-key errors: []")
    print("API SMOKE TEST PASSED")


if __name__ == "__main__":
    main()
