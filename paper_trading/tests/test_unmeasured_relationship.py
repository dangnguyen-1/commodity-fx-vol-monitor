"""A relationship with no measured beta must be skipped, not crash the stage.

Since v0.3.0 a relationship without a `transmission_beta` produces no
expected impulse and therefore no divergence score. That is deliberate:
falling back to a direction of 1 would reintroduce the error the measured
coefficient replaced.

The signal builder then called finite_number() on those NULLs and raised
`TypeError: float() argument must be a string or a real number, not
'NoneType'`, taking the whole signals stage down with it.

It stayed hidden for a day because it needs a *complete* feature snapshot
for an unmeasured relationship, and coverage was too poor to produce one.
When coverage improved overnight the stage failed every cycle for five
hours until the offending snapshot aged out and it silently recovered.

Run:  python3 -m paper_trading.tests.test_unmeasured_relationship
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from pathlib import Path

from paper_trading.features.build_feature_snapshots import (
    ensure_strategy_spec,
)
from paper_trading.signals.build_signal_decisions import (
    build_signal_decisions,
)
from paper_trading.tests.test_paper_execution_lifecycle import (
    configure,
    select_candidate,
)
from strategy.config.intraday.load_intraday_spec import (
    DEFAULT_SPEC_PATH,
    load_intraday_spec,
)


DEFAULT_SOURCE_DB = Path("paper_trading/data/paper_trading.db")
DEFAULT_TEST_DB = Path("/tmp/unmeasured_relationship.db")
RUN_ID = "commodity_fx_intraday-unmeasured-test"

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

    spec = load_intraday_spec(args.spec)
    equity = float(spec["capital"]["initial_equity_usd"])

    with sqlite3.connect(args.test_database, timeout=10.0) as connection:
        configure(connection)
        (
            source_feature_id,
            _run,
            _hist,
            relationship_id,
            timestamp,
            _fx,
        ) = select_candidate(connection)
        spec_id = ensure_strategy_spec(connection, spec)

        # Strip the beta from this relationship, exactly as production looks
        # for the eighteen that have never been measured.
        connection.execute(
            """
            UPDATE live_instrument_registry
            SET transmission_beta = NULL
            WHERE relationship_id = ?
            """,
            (str(relationship_id),),
        )

        connection.execute(
            """
            INSERT INTO paper_runs (
                run_id, spec_id, run_mode, status,
                initial_equity_usd, started_at_utc, stopped_at_utc, notes
            ) VALUES (?, ?, 'local_replay', 'running', ?, ?, NULL, ?)
            """,
            (RUN_ID, spec_id, equity, str(timestamp), "Unmeasured-beta test."),
        )

        # A complete snapshot carrying no expected impulse and no divergence:
        # the exact shape that broke production.
        connection.execute(
            """
            INSERT INTO feature_snapshots (
                run_id, spec_id, relationship_id, feature_timestamp_utc,
                commodity_return_15m, commodity_return_60m,
                commodity_return_240m, fx_return_15m,
                realized_volatility_60m,
                normalized_commodity_return_15m,
                normalized_commodity_return_60m,
                normalized_commodity_return_240m,
                normalized_fx_return_15m, commodity_impulse,
                news_impulse, expected_fx_impulse, observed_fx_impulse,
                divergence_score, relevant_news_count,
                market_window_coverage_pct, market_data_complete,
                created_at_utc
            )
            SELECT ?, ?, relationship_id, ?,
                   commodity_return_15m, commodity_return_60m,
                   commodity_return_240m, fx_return_15m,
                   realized_volatility_60m,
                   normalized_commodity_return_15m,
                   normalized_commodity_return_60m,
                   normalized_commodity_return_240m,
                   normalized_fx_return_15m, commodity_impulse,
                   news_impulse,
                   NULL, observed_fx_impulse, NULL,
                   relevant_news_count, market_window_coverage_pct, 1,
                   created_at_utc
            FROM feature_snapshots WHERE feature_id = ?
            """,
            (RUN_ID, spec_id, str(timestamp), int(source_feature_id)),
        )
        connection.commit()

        stored = connection.execute(
            """
            SELECT market_data_complete, expected_fx_impulse, divergence_score
            FROM feature_snapshots WHERE run_id = ?
            """,
            (RUN_ID,),
        ).fetchone()

    print(f"\nrelationship under test: {relationship_id}")
    check("snapshot is complete", stored[0], 1)
    check("expected impulse is null", stored[1], None)
    check("divergence is null", stored[2], None)

    print("\nSignal stage against an unmeasured relationship:")
    try:
        details = build_signal_decisions(
            database_path=args.test_database,
            spec_path=args.spec,
            decision_timestamp=None,
            run_id=RUN_ID,
            run_mode="local_replay",
        )
        check("stage completed without raising", True, True)
        # It should evaluate nothing rather than inventing a signal from a
        # relationship whose transmission has never been measured.
        check(
            "no entries approved",
            int(details.get("approved_entries", 0)),
            0,
        )
        print(
            f"        complete_features_evaluated="
            f"{details.get('complete_features_evaluated')}"
        )
    except Exception as error:  # noqa: BLE001
        check(
            f"stage completed without raising ({type(error).__name__}: {error})",
            False,
            True,
        )

    print()
    if FAILURES:
        print(f"UNMEASURED RELATIONSHIP TESTS FAILED: {len(FAILURES)}")
        for name in FAILURES:
            print(f"  - {name}")
        raise SystemExit(1)
    print("UNMEASURED RELATIONSHIP TESTS PASSED")


if __name__ == "__main__":
    main()
