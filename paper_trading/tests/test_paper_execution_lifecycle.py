from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from paper_trading.execution.run_paper_execution import (
    parse_utc_iso,
    run_paper_execution,
)
from strategy.config.intraday.load_intraday_spec import (
    DEFAULT_SPEC_PATH,
    load_intraday_spec,
)


DEFAULT_SOURCE_DB = Path("paper_trading/data/paper_trading.db")
DEFAULT_TEST_DB = Path("/tmp/paper_execution_lifecycle.db")
TEST_RUN_ID = "commodity_fx_intraday-lifecycle-test-v0.1.0"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def configure(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")


def select_candidate(
    connection: sqlite3.Connection,
) -> tuple:
    row = connection.execute(
        """
        SELECT
            f.feature_id,
            f.run_id,
            f.spec_id,
            f.relationship_id,
            f.feature_timestamp_utc,
            r.fx_symbol
        FROM feature_snapshots f
        JOIN relationships r
          ON r.relationship_id = f.relationship_id
        WHERE f.market_data_complete = 1
          AND f.realized_volatility_60m IS NOT NULL
          AND f.divergence_score IS NOT NULL
          AND (
              SELECT COUNT(*)
              FROM market_bars_1m b
              WHERE b.symbol = r.fx_symbol
                AND b.is_complete = 1
                AND b.bar_timestamp_utc >
                    f.feature_timestamp_utc
          ) >= 3
        ORDER BY
            f.feature_timestamp_utc DESC,
            ABS(f.divergence_score) DESC
        LIMIT 1
        """
    ).fetchone()

    if row is None:
        raise RuntimeError(
            "No complete feature has at least three later FX bars."
        )

    return row


def next_three_bars(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    after_timestamp: str,
) -> list[tuple[str, float, float]]:
    rows = connection.execute(
        """
        SELECT
            bar_timestamp_utc,
            open_price,
            close_price
        FROM market_bars_1m
        WHERE symbol = ?
          AND is_complete = 1
          AND bar_timestamp_utc > ?
        ORDER BY bar_timestamp_utc
        LIMIT 3
        """,
        (symbol, after_timestamp),
    ).fetchall()

    if len(rows) != 3:
        raise RuntimeError(
            f"Expected three later bars for {symbol}, found {len(rows)}."
        )

    return [
        (str(row[0]), float(row[1]), float(row[2]))
        for row in rows
    ]


def copy_feature_for_test_run(
    connection: sqlite3.Connection,
    *,
    source_feature_id: int,
    run_id: str,
    timestamp: str,
    divergence_score: float,
) -> int:
    source = connection.execute(
        """
        SELECT
            spec_id,
            relationship_id,
            commodity_return_15m,
            commodity_return_60m,
            commodity_return_240m,
            fx_return_15m,
            realized_volatility_60m,
            normalized_commodity_return_15m,
            normalized_commodity_return_60m,
            normalized_commodity_return_240m,
            normalized_fx_return_15m,
            commodity_impulse,
            news_impulse,
            expected_fx_impulse,
            observed_fx_impulse,
            relevant_news_count,
            market_window_coverage_pct
        FROM feature_snapshots
        WHERE feature_id = ?
        """,
        (source_feature_id,),
    ).fetchone()

    if source is None:
        raise RuntimeError("Source feature disappeared.")

    now = utc_now_iso()
    connection.execute(
        """
        INSERT INTO feature_snapshots (
            run_id,
            spec_id,
            relationship_id,
            feature_timestamp_utc,
            commodity_return_15m,
            commodity_return_60m,
            commodity_return_240m,
            fx_return_15m,
            realized_volatility_60m,
            normalized_commodity_return_15m,
            normalized_commodity_return_60m,
            normalized_commodity_return_240m,
            normalized_fx_return_15m,
            commodity_impulse,
            news_impulse,
            expected_fx_impulse,
            observed_fx_impulse,
            divergence_score,
            relevant_news_count,
            market_window_coverage_pct,
            market_data_complete,
            created_at_utc
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, 1, ?
        )
        """,
        (
            run_id,
            int(source[0]),
            str(source[1]),
            timestamp,
            source[2],
            source[3],
            source[4],
            source[5],
            source[6],
            source[7],
            source[8],
            source[9],
            source[10],
            source[11],
            source[12],
            source[13],
            source[14],
            divergence_score,
            int(source[15]),
            float(source[16]),
            now,
        ),
    )

    return int(connection.execute(
        "SELECT last_insert_rowid()"
    ).fetchone()[0])


def create_entry_decision(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    spec_id: int,
    feature_id: int,
    relationship_id: str,
    timestamp: str,
) -> str:
    decision_id = "synthetic-lifecycle-entry"
    connection.execute(
        """
        INSERT INTO signal_decisions (
            decision_id,
            run_id,
            spec_id,
            feature_id,
            relationship_id,
            decision_timestamp_utc,
            decision_type,
            signal_mode,
            signal_strength,
            approved,
            reason_code,
            reason_detail,
            decision_snapshot_json,
            created_at_utc
        )
        VALUES (
            ?, ?, ?, ?, ?, ?,
            'enter_long',
            'divergence',
            2.0,
            1,
            'synthetic_lifecycle_entry',
            'Controlled temporary-database lifecycle test.',
            ?,
            ?
        )
        """,
        (
            decision_id,
            run_id,
            spec_id,
            feature_id,
            relationship_id,
            timestamp,
            json.dumps(
                {
                    "synthetic": True,
                    "purpose": "paper execution lifecycle test",
                },
                sort_keys=True,
            ),
            utc_now_iso(),
        ),
    )
    return decision_id


def setup_test_database(
    *,
    source_db: Path,
    test_db: Path,
    spec_path: Path,
) -> dict[str, str | int | float]:
    source_db = source_db.resolve()
    test_db = test_db.resolve()

    if not source_db.exists():
        raise FileNotFoundError(source_db)

    test_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_db, test_db)

    spec = load_intraday_spec(spec_path)
    initial_equity = float(spec["capital"]["initial_equity_usd"])

    with sqlite3.connect(test_db, timeout=5.0) as connection:
        configure(connection)

        candidate = select_candidate(connection)
        (
            source_feature_id,
            _source_run_id,
            spec_id,
            relationship_id,
            feature_timestamp,
            fx_symbol,
        ) = candidate

        bars = next_three_bars(
            connection,
            symbol=str(fx_symbol),
            after_timestamp=str(feature_timestamp),
        )
        entry_fill_timestamp = bars[0][0]
        convergence_timestamp = bars[1][0]
        exit_fill_timestamp = bars[2][0]

        connection.execute(
            """
            INSERT INTO paper_runs (
                run_id,
                spec_id,
                run_mode,
                status,
                initial_equity_usd,
                started_at_utc,
                stopped_at_utc,
                notes
            )
            VALUES (?, ?, 'local_replay', 'running', ?, ?, NULL, ?)
            """,
            (
                TEST_RUN_ID,
                int(spec_id),
                initial_equity,
                str(feature_timestamp),
                "Synthetic lifecycle test in a temporary database.",
            ),
        )

        entry_feature_id = copy_feature_for_test_run(
            connection,
            source_feature_id=int(source_feature_id),
            run_id=TEST_RUN_ID,
            timestamp=str(feature_timestamp),
            divergence_score=2.0,
        )
        create_entry_decision(
            connection,
            run_id=TEST_RUN_ID,
            spec_id=int(spec_id),
            feature_id=entry_feature_id,
            relationship_id=str(relationship_id),
            timestamp=str(feature_timestamp),
        )

        connection.commit()

    return {
        "test_database": str(test_db),
        "run_id": TEST_RUN_ID,
        "spec_id": int(spec_id),
        "relationship_id": str(relationship_id),
        "fx_symbol": str(fx_symbol),
        "source_feature_timestamp": str(feature_timestamp),
        "entry_fill_timestamp": entry_fill_timestamp,
        "convergence_timestamp": convergence_timestamp,
        "exit_fill_timestamp": exit_fill_timestamp,
        "source_feature_id": int(source_feature_id),
    }


def add_convergence_feature(
    *,
    test_db: Path,
    metadata: dict[str, str | int | float],
) -> int:
    with sqlite3.connect(test_db, timeout=5.0) as connection:
        configure(connection)
        feature_id = copy_feature_for_test_run(
            connection,
            source_feature_id=int(metadata["source_feature_id"]),
            run_id=str(metadata["run_id"]),
            timestamp=str(metadata["convergence_timestamp"]),
            divergence_score=0.0,
        )
        connection.commit()
        return feature_id


def audit(
    *,
    test_db: Path,
    run_id: str,
) -> dict[str, object]:
    with sqlite3.connect(test_db, timeout=5.0) as connection:
        configure(connection)

        orders = connection.execute(
            """
            SELECT
                order_action,
                side,
                status,
                COUNT(*)
            FROM orders
            WHERE run_id = ?
            GROUP BY order_action, side, status
            ORDER BY order_action
            """,
            (run_id,),
        ).fetchall()

        fills = int(connection.execute(
            """
            SELECT COUNT(*)
            FROM fills f
            JOIN orders o ON o.order_id = f.order_id
            WHERE o.run_id = ?
            """,
            (run_id,),
        ).fetchone()[0])

        position = connection.execute(
            """
            SELECT
                status,
                direction,
                entry_price,
                exit_price,
                gross_pnl_usd,
                transaction_cost_usd,
                net_pnl_usd,
                exit_reason
            FROM positions
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()

        latest_equity = connection.execute(
            """
            SELECT
                cash_usd,
                realized_pnl_usd,
                unrealized_pnl_usd,
                transaction_cost_usd,
                total_equity_usd,
                gross_exposure_usd,
                open_positions
            FROM equity_snapshots
            WHERE run_id = ?
            ORDER BY snapshot_timestamp_utc DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()

        counts = {
            "orders": int(connection.execute(
                "SELECT COUNT(*) FROM orders WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]),
            "fills": fills,
            "positions": int(connection.execute(
                "SELECT COUNT(*) FROM positions WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]),
            "open_positions": int(connection.execute(
                """
                SELECT COUNT(*)
                FROM positions
                WHERE run_id = ?
                  AND status = 'open'
                """,
                (run_id,),
            ).fetchone()[0]),
            "position_marks": int(connection.execute(
                """
                SELECT COUNT(*)
                FROM position_marks
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()[0]),
            "equity_snapshots": int(connection.execute(
                """
                SELECT COUNT(*)
                FROM equity_snapshots
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()[0]),
        }

        foreign_keys = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

    checks = {
        "two_orders": counts["orders"] == 2,
        "two_fills": counts["fills"] == 2,
        "one_position": counts["positions"] == 1,
        "position_closed": (
            position is not None and position[0] == "closed"
        ),
        "convergence_exit": (
            position is not None
            and position[7] == "divergence_convergence"
        ),
        "no_open_positions": counts["open_positions"] == 0,
        "equity_present": latest_equity is not None,
        "foreign_keys_clean": foreign_keys == [],
    }

    return {
        "orders": orders,
        "position": position,
        "latest_equity": latest_equity,
        "counts": counts,
        "foreign_key_errors": foreign_keys,
        "checks": checks,
        "passed": all(checks.values()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run a controlled entry-to-exit paper-execution lifecycle "
            "test on a temporary SQLite database."
        )
    )
    parser.add_argument(
        "--source-database",
        type=Path,
        default=DEFAULT_SOURCE_DB,
    )
    parser.add_argument(
        "--test-database",
        type=Path,
        default=DEFAULT_TEST_DB,
    )
    parser.add_argument(
        "--spec",
        type=Path,
        default=DEFAULT_SPEC_PATH,
    )
    args = parser.parse_args()

    metadata = setup_test_database(
        source_db=args.source_database,
        test_db=args.test_database,
        spec_path=args.spec,
    )

    print("Temporary lifecycle database created.")
    for key, value in metadata.items():
        print(f"{key}: {value}")

    entry_result = run_paper_execution(
        database_path=args.test_database,
        spec_path=args.spec,
        run_id=TEST_RUN_ID,
        run_mode="local_replay",
        as_of=parse_utc_iso(
            str(metadata["entry_fill_timestamp"])
        ),
    )
    print("\nEntry execution:")
    print(json.dumps(entry_result, indent=2, sort_keys=True))

    convergence_feature_id = add_convergence_feature(
        test_db=args.test_database,
        metadata=metadata,
    )
    print(
        "\nSynthetic convergence feature_id:",
        convergence_feature_id,
    )

    exit_result = run_paper_execution(
        database_path=args.test_database,
        spec_path=args.spec,
        run_id=TEST_RUN_ID,
        run_mode="local_replay",
        as_of=parse_utc_iso(
            str(metadata["exit_fill_timestamp"])
        ),
    )
    print("\nExit execution:")
    print(json.dumps(exit_result, indent=2, sort_keys=True))

    rerun_result = run_paper_execution(
        database_path=args.test_database,
        spec_path=args.spec,
        run_id=TEST_RUN_ID,
        run_mode="local_replay",
        as_of=parse_utc_iso(
            str(metadata["exit_fill_timestamp"])
        ),
    )
    print("\nIdempotent rerun:")
    print(json.dumps(rerun_result, indent=2, sort_keys=True))

    result = audit(
        test_db=args.test_database,
        run_id=TEST_RUN_ID,
    )
    print("\nLifecycle audit:")
    print(json.dumps(result, indent=2, sort_keys=True))

    if not result["passed"]:
        raise SystemExit("LIFECYCLE TEST FAILED")

    print("\nLIFECYCLE TEST PASSED")


if __name__ == "__main__":
    main()