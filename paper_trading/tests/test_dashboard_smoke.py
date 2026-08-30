from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import uvicorn

from paper_trading.api.database import DEFAULT_DATABASE_PATH
from paper_trading.dashboard.api_client import ApiClient, ApiNotFoundError


TABLES = [
    "paper_runs",
    "feature_snapshots",
    "signal_decisions",
    "orders",
    "fills",
    "positions",
    "equity_snapshots",
    "news_classification_assets",
    "system_alerts",
]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _table_counts(database_path: Path) -> dict[str, int]:
    with sqlite3.connect(database_path) as connection:
        return {
            table: int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in TABLES
        }


def main() -> None:
    source = DEFAULT_DATABASE_PATH.resolve()
    if not source.exists():
        raise FileNotFoundError(
            f"Source paper-trading database not found: {source}"
        )

    temp_dir = Path(tempfile.mkdtemp(prefix="dashboard-smoke-"))
    test_database = temp_dir / "paper_trading.db"
    shutil.copy2(source, test_database)
    os.environ["PAPER_TRADING_DATABASE_PATH"] = str(test_database)

    before = _table_counts(test_database)

    from paper_trading.api.app import app

    port = _free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.1)
        if not server.started:
            raise RuntimeError("API server did not start in time.")

        client = ApiClient(base_url=f"http://127.0.0.1:{port}")

        client.health()
        client.services()
        client.strategy()
        try:
            client.current_run()
        except ApiNotFoundError:
            pass
        client.relationships()
        client.features_latest()
        client.signals_latest()
        client.positions(status="all")
        client.orders()
        client.fills()
        client.equity()
        client.news_latest()
        client.alerts(resolved=None)
        client.summary()
        print("All dashboard API client calls succeeded.")
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    after = _table_counts(test_database)
    if before != after:
        raise AssertionError(
            "Dashboard read traffic changed database state: "
            f"{before} -> {after}"
        )
    print("Database counts unchanged:", after)
    print("DASHBOARD SMOKE TEST PASSED")


if __name__ == "__main__":
    main()
