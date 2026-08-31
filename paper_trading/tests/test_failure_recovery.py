"""Checks the engine survives being interrupted and partially broken.

The original handoff listed "no restart/failure-recovery testing" as an
outstanding gap before formal paper trading, and it has stayed outstanding.
Restarts have happened incidentally during deploys and one VM reboot, but
nothing has deliberately broken the system to see what it does.

Three properties are worth pinning down, because each has already had a
near-miss in this codebase:

  1. IDEMPOTENCE. Re-running a cycle for a timestamp already processed must
     not duplicate orders, fills or positions. The whole restart story rests
     on this: a process killed mid-cycle will redo work on the way back up.

  2. STAGE ISOLATION. The orchestrator runs execution even when the feature
     or signal stage has failed, deliberately -- failing to enter a trade is
     a missed opportunity, failing to exit one is open risk, and the
     no-overnight rule depends on execution getting a turn regardless. That
     is only a guarantee if it is tested.

  3. SURVIVING SIGKILL. SIGTERM is handled gracefully and already covered.
     SIGKILL is not catchable, so the question is whether the database is
     left consistent when the process dies mid-write.

Run:  python3 -m paper_trading.tests.test_failure_recovery
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from paper_trading.execution.run_paper_execution import (
    parse_utc_iso,
    relationship_venues,
    run_paper_execution,
)
from paper_trading.features.build_feature_snapshots import (
    ensure_strategy_spec,
)
from paper_trading.sessions.session_calendar import SessionCalendar
from paper_trading.tests.test_paper_execution_lifecycle import (
    configure,
    copy_feature_for_test_run,
    create_entry_decision,
    select_candidate,
    session_safe_timestamp,
)
from strategy.config.intraday.load_intraday_spec import (
    DEFAULT_SPEC_PATH,
    load_intraday_spec,
)


DEFAULT_SOURCE_DB = Path("paper_trading/data/paper_trading.db")
DEFAULT_TEST_DB = Path("/tmp/failure_recovery.db")
RUN_ID = "commodity_fx_intraday-failure-recovery-test"

FAILURES: list[str] = []


def check(name: str, actual: object, expected: object) -> None:
    if actual == expected:
        print(f"  ok    {name}")
        return
    FAILURES.append(name)
    print(
        f"  FAIL  {name}\n"
        f"          expected {expected!r}\n"
        f"          actual   {actual!r}"
    )


def counts(database: Path) -> dict[str, int]:
    connection = sqlite3.connect(database, timeout=10.0)
    try:
        return {
            table: int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE run_id = ?",
                    (RUN_ID,),
                ).fetchone()[0]
            )
            for table in (
                "orders",
                "positions",
                "equity_snapshots",
                "signal_decisions",
            )
        }
    finally:
        connection.close()


def seed_open_position(database: Path, spec_path: Path) -> str:
    """Build a run with one open position, using the shared fixtures."""
    spec = load_intraday_spec(spec_path)
    equity = float(spec["capital"]["initial_equity_usd"])

    with sqlite3.connect(database, timeout=10.0) as connection:
        configure(connection)
        (
            source_feature_id,
            _run,
            _hist,
            relationship_id,
            _ts,
            fx_symbol,
        ) = select_candidate(connection)
        spec_id = ensure_strategy_spec(connection, spec)
        entry = session_safe_timestamp(
            connection,
            relationship_id=str(relationship_id),
            fx_symbol=str(fx_symbol),
            spec=spec,
        )
        connection.execute(
            """
            INSERT INTO paper_runs (
                run_id, spec_id, run_mode, status,
                initial_equity_usd, started_at_utc, stopped_at_utc, notes
            ) VALUES (?, ?, 'local_replay', 'running', ?, ?, NULL, ?)
            """,
            (RUN_ID, spec_id, equity, entry, "Failure-recovery test."),
        )
        feature_id = copy_feature_for_test_run(
            connection,
            source_feature_id=int(source_feature_id),
            run_id=RUN_ID,
            timestamp=entry,
            divergence_score=2.0,
        )
        create_entry_decision(
            connection, run_id=RUN_ID, spec_id=spec_id,
            feature_id=feature_id,
            relationship_id=str(relationship_id), timestamp=entry,
        )
        fill_bar = connection.execute(
            """
            SELECT bar_timestamp_utc FROM market_bars_1m
            WHERE symbol = ? AND is_complete = 1 AND bar_timestamp_utc > ?
            ORDER BY bar_timestamp_utc LIMIT 1
            """,
            (str(fx_symbol), entry),
        ).fetchone()[0]
        connection.commit()

    run_paper_execution(
        database_path=database, spec_path=spec_path,
        run_id=RUN_ID, run_mode="local_replay",
        as_of=parse_utc_iso(str(fill_bar)),
    )
    return str(fill_bar)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-database", type=Path, default=DEFAULT_SOURCE_DB)
    parser.add_argument("--test-database", type=Path, default=DEFAULT_TEST_DB)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC_PATH)
    args = parser.parse_args()

    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(args.test_database) + suffix)
        if candidate.exists():
            candidate.unlink()
    shutil.copy2(args.source_database, args.test_database)

    print("\n1. A position is open before anything is broken")
    fill_bar = seed_open_position(args.test_database, args.spec)
    with sqlite3.connect(args.test_database) as connection:
        open_positions = connection.execute(
            "SELECT COUNT(*) FROM positions WHERE run_id=? AND status='open'",
            (RUN_ID,),
        ).fetchone()[0]
    check("position is open", int(open_positions), 1)
    baseline = counts(args.test_database)

    print("\n2. Re-running the same cycle does not duplicate anything")
    for _ in range(3):
        run_paper_execution(
            database_path=args.test_database, spec_path=args.spec,
            run_id=RUN_ID, run_mode="local_replay",
            as_of=parse_utc_iso(fill_bar),
        )
    repeated = counts(args.test_database)
    for table in ("orders", "positions"):
        check(
            f"{table} unchanged after 3 replays",
            repeated[table],
            baseline[table],
        )

    print("\n3. Execution still runs when the feature stage is broken")
    # Actually break the feature stage rather than asserting against an
    # unbroken system. news_classification_assets is required by
    # build_feature_snapshots and unused by run_paper_execution, so dropping
    # it fails features and signals while leaving execution a clear path.
    # The table definition is captured first so it can be put back.
    connection = sqlite3.connect(args.test_database, timeout=10.0)
    try:
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?",
            ("news_classification_assets",),
        ).fetchone()[0]
        connection.execute("DROP TABLE news_classification_assets")
        connection.commit()
    finally:
        connection.close()

    broken = subprocess.run(
        [
            sys.executable,
            "-m",
            "paper_trading.orchestrator.run_strategy_cycle",
            "--once",
            "--database",
            str(args.test_database),
            "--run-mode",
            "local_replay",
            # Point it at the seeded run. Without this the orchestrator uses
            # its own default run id, finds no paper run, and execution fails
            # for a reason that has nothing to do with stage isolation.
            "--run-id",
            RUN_ID,
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "PYTHONPATH": "."},
    )
    output = broken.stdout + broken.stderr

    connection = sqlite3.connect(args.test_database, timeout=10.0)
    try:
        connection.execute(definition)
        connection.commit()
    finally:
        connection.close()

    check(
        "orchestrator did not crash outright",
        broken.returncode in (0, 1),
        True,
    )
    # The point of the test: the stage that was broken reports failure, and
    # execution still ran. Both halves matter -- without the first this
    # asserts nothing.
    check(
        "feature stage actually failed",
        "features=FAIL" in output,
        True,
    )
    check(
        "execution ran anyway",
        "execution=ok" in output,
        True,
    )
    # The first version of this test uncovered a heartbeat status of
    # "unhealthy" that the schema's CHECK constraint rejected, so the
    # orchestrator went silent exactly when it was in trouble.
    check(
        "heartbeat was still written while degraded",
        "could not write heartbeat" not in output,
        True,
    )

    print("\n4. Surviving SIGKILL mid-cycle")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "paper_trading.orchestrator.run_strategy_cycle",
            "--database",
            str(args.test_database),
            "--run-mode",
            "local_replay",
            "--run-id",
            RUN_ID,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "PYTHONPATH": "."},
    )
    # Long enough to be inside a cycle, short enough to be mid-work.
    time.sleep(4)
    os.kill(process.pid, signal.SIGKILL)
    process.wait(timeout=30)
    check("process was killed uncatchably", process.returncode != 0, True)

    connection = sqlite3.connect(args.test_database, timeout=15.0)
    try:
        integrity = connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
    finally:
        connection.close()
    check("database intact after SIGKILL", integrity, "ok")
    check("no foreign key violations", len(foreign_keys), 0)

    print("\n5. It picks up cleanly afterwards")
    after_kill = counts(args.test_database)
    recovered = subprocess.run(
        [
            sys.executable,
            "-m",
            "paper_trading.orchestrator.run_strategy_cycle",
            "--once",
            "--database",
            str(args.test_database),
            "--run-mode",
            "local_replay",
            "--run-id",
            RUN_ID,
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "PYTHONPATH": "."},
    )
    check(
        "restarted cycle completed",
        "single cycle" in (recovered.stdout + recovered.stderr),
        True,
    )
    final = counts(args.test_database)
    check(
        "no duplicate positions after recovery",
        final["positions"],
        after_kill["positions"],
    )

    print()
    if FAILURES:
        print(f"FAILURE RECOVERY TESTS FAILED: {len(FAILURES)}")
        for name in FAILURES:
            print(f"  - {name}")
        raise SystemExit(1)
    print("FAILURE RECOVERY TESTS PASSED")


if __name__ == "__main__":
    main()
