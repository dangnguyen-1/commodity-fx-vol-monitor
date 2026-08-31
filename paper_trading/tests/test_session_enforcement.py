"""End-to-end proof that session rules are actually enforced.

The spec has always said:

    sessions.allow_overnight_positions: false
    sessions.block_new_entries_before_market_close_minutes: 30

Nothing enforced either one until now, and the execution engine's heartbeat
openly reported the gap. Unit tests on the calendar arithmetic live in
test_session_calendar.py; this one drives the real execution engine against a
copy of the database and checks two things it must never get wrong:

  1. an entry inside the pre-close blackout is rejected, and
  2. a position still open at its flat deadline is closed, with the exit
     recorded as `session_close`.

Run:  python3 -m paper_trading.tests.test_session_enforcement
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path

from paper_trading.execution.run_paper_execution import (
    parse_utc_iso,
    relationship_venues,
    run_paper_execution,
    utc_iso,
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
)
from strategy.config.intraday.load_intraday_spec import (
    DEFAULT_SPEC_PATH,
    load_intraday_spec,
)


DEFAULT_SOURCE_DB = Path("paper_trading/data/paper_trading.db")
DEFAULT_TEST_DB = Path("/tmp/session_enforcement.db")
BLACKOUT_RUN_ID = "commodity_fx_intraday-session-blackout-test-v0.1.0"
FLATTEN_RUN_ID = "commodity_fx_intraday-session-flatten-test-v0.1.0"

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


def create_run(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    spec_id: int,
    started_at: str,
    equity: float,
) -> None:
    connection.execute(
        """
        INSERT INTO paper_runs (
            run_id, spec_id, run_mode, status,
            initial_equity_usd, started_at_utc, stopped_at_utc, notes
        )
        VALUES (?, ?, 'local_replay', 'running', ?, ?, NULL, ?)
        """,
        (
            run_id,
            spec_id,
            equity,
            started_at,
            "Synthetic session-enforcement test.",
        ),
    )


def find_crossing_bars(
    connection: sqlite3.Connection,
    *,
    fx_symbol: str,
    relationship_id: str,
    blackout_minutes: float,
) -> tuple[str, str, str, str]:
    """Find a day whose bars straddle a flat deadline.

    Returns (entry_bar, entry_fill_bar, blackout_bar, bar_after_deadline).
    The engine can only act where completed bars exist, so all four have to
    be real — including the fill bar, since an entry fills against the bar
    *after* its decision, never the decision's own bar.
    """
    calendar = SessionCalendar()
    commodity_venue, fx_venue = relationship_venues(
        connection, relationship_id=relationship_id
    )

    rows = connection.execute(
        """
        SELECT bar_timestamp_utc
        FROM market_bars_1m
        WHERE symbol = ? AND is_complete = 1
        ORDER BY bar_timestamp_utc
        """,
        (fx_symbol,),
    ).fetchall()
    stamps = [str(row[0]) for row in rows]

    # Newest first: the dense weekday sessions are at the end of the dataset,
    # while the start of it is the thin Sunday FX week open.
    for stamp in reversed(stamps):
        as_of = parse_utc_iso(stamp)
        deadline = calendar.flat_deadline(
            commodity_venue=commodity_venue,
            fx_venue=fx_venue,
            as_of=as_of,
        )
        remaining = deadline.minutes_remaining(as_of)
        if remaining < blackout_minutes + 20:
            continue

        # Margin alone is not enough: the first bar of the dataset is the
        # Sunday FX week open, where the following bars are sparse and the
        # entry expires before any fill bar arrives. Require a bar close
        # behind this one so the entry has something to fill against.
        follow_on = next(
            (
                s
                for s in stamps
                if 0
                < (
                    parse_utc_iso(s) - as_of
                ).total_seconds()
                <= 180
            ),
            None,
        )
        if follow_on is None:
            continue

        # A bar inside the blackout, and one after the deadline, both on the
        # same session as this entry.
        blackout_bar = next(
            (
                s
                for s in stamps
                if 0
                < (
                    deadline.deadline_utc - parse_utc_iso(s)
                ).total_seconds()
                <= blackout_minutes * 60
            ),
            None,
        )
        after_bar = next(
            (
                s
                for s in stamps
                if parse_utc_iso(s) > deadline.deadline_utc
                and parse_utc_iso(s)
                < deadline.deadline_utc + timedelta(hours=2)
            ),
            None,
        )
        if blackout_bar and after_bar:
            return stamp, follow_on, blackout_bar, after_bar

    raise RuntimeError(
        "No session in the dataset has bars on both sides of a deadline."
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-database", type=Path, default=DEFAULT_SOURCE_DB)
    parser.add_argument("--test-database", type=Path, default=DEFAULT_TEST_DB)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC_PATH)
    args = parser.parse_args()

    if args.test_database.exists():
        args.test_database.unlink()
    shutil.copy2(args.source_database, args.test_database)

    spec = load_intraday_spec(args.spec)
    equity = float(spec["capital"]["initial_equity_usd"])
    blackout = float(
        spec["sessions"]["block_new_entries_before_market_close_minutes"]
    )

    check("spec forbids overnight positions",
          bool(spec["sessions"]["allow_overnight_positions"]), False)

    with sqlite3.connect(args.test_database, timeout=5.0) as connection:
        configure(connection)
        (
            source_feature_id,
            _run_id,
            _historical_spec_id,
            relationship_id,
            _feature_timestamp,
            fx_symbol,
        ) = select_candidate(connection)

        # The spec current now, not the one the shipped snapshot was built
        # under; the engine refuses a run whose spec_id does not match.
        spec_id = ensure_strategy_spec(connection, spec)

        (
            entry_bar,
            entry_fill_bar,
            blackout_bar,
            after_deadline_bar,
        ) = find_crossing_bars(
            connection,
            fx_symbol=str(fx_symbol),
            relationship_id=str(relationship_id),
            blackout_minutes=blackout,
        )

        calendar = SessionCalendar()
        commodity_venue, fx_venue = relationship_venues(
            connection, relationship_id=str(relationship_id)
        )
        deadline = calendar.flat_deadline(
            commodity_venue=commodity_venue,
            fx_venue=fx_venue,
            as_of=parse_utc_iso(entry_bar),
        )
        print(
            f"\nrelationship {relationship_id}"
            f"\n  venues        {commodity_venue} / {fx_venue}"
            f"\n  entry bar     {entry_bar} (fills at {entry_fill_bar})"
            f"\n  blackout bar  {blackout_bar}"
            f"\n  deadline      {utc_iso(deadline.deadline_utc)}"
            f"  (via {deadline.binding_leg})"
            f"\n  after-deadline bar {after_deadline_bar}\n"
        )

        # ---- 1. an entry inside the blackout is refused -----------------
        create_run(
            connection, run_id=BLACKOUT_RUN_ID, spec_id=int(spec_id),
            started_at=blackout_bar, equity=equity,
        )
        blackout_feature = copy_feature_for_test_run(
            connection,
            source_feature_id=int(source_feature_id),
            run_id=BLACKOUT_RUN_ID,
            timestamp=blackout_bar,
            divergence_score=2.0,
        )
        create_entry_decision(
            connection, run_id=BLACKOUT_RUN_ID, spec_id=int(spec_id),
            feature_id=blackout_feature,
            relationship_id=str(relationship_id), timestamp=blackout_bar,
        )

        # ---- 2. a position opened in-session is flattened at the deadline
        create_run(
            connection, run_id=FLATTEN_RUN_ID, spec_id=int(spec_id),
            started_at=entry_bar, equity=equity,
        )
        entry_feature = copy_feature_for_test_run(
            connection,
            source_feature_id=int(source_feature_id),
            run_id=FLATTEN_RUN_ID,
            timestamp=entry_bar,
            divergence_score=2.0,
        )
        connection.execute(
            "UPDATE signal_decisions SET decision_id = ? WHERE decision_id = ?",
            ("synthetic-blackout-entry", "synthetic-lifecycle-entry"),
        )
        create_entry_decision(
            connection, run_id=FLATTEN_RUN_ID, spec_id=int(spec_id),
            feature_id=entry_feature,
            relationship_id=str(relationship_id), timestamp=entry_bar,
        )
        connection.commit()

    print("Running execution inside the blackout:")
    run_paper_execution(
        database_path=args.test_database, spec_path=args.spec,
        run_id=BLACKOUT_RUN_ID, run_mode="local_replay",
        as_of=parse_utc_iso(blackout_bar),
    )

    # Run at the bar *after* the decision: that is the first bar an entry can
    # fill against.
    print("Running execution at the in-session entry:")
    entry_result = run_paper_execution(
        database_path=args.test_database, spec_path=args.spec,
        run_id=FLATTEN_RUN_ID, run_mode="local_replay",
        as_of=parse_utc_iso(entry_fill_bar),
    )

    with sqlite3.connect(args.test_database, timeout=5.0) as connection:
        configure(connection)
        blackout_order = connection.execute(
            """
            SELECT status, rejection_reason FROM orders
            WHERE run_id = ? ORDER BY submitted_at_utc LIMIT 1
            """,
            (BLACKOUT_RUN_ID,),
        ).fetchone()
        open_positions = connection.execute(
            "SELECT COUNT(*) FROM positions WHERE run_id = ? AND status = 'open'",
            (FLATTEN_RUN_ID,),
        ).fetchone()[0]

    print("\nBlackout entry:")
    check("blackout order was rejected",
          blackout_order[0] if blackout_order else None, "rejected")
    check("rejection names the session rule",
          blackout_order[1] if blackout_order else None,
          "session_close_blackout")

    print("\nIn-session entry:")
    check("in-session entry opened a position", int(open_positions), 1)
    check("entry reported as filled", entry_result["entries_filled"], 1)

    # Two passes, because a session close fills like any other exit: the
    # first cycle past the deadline writes the exit decision and order, the
    # next one fills it against the following bar. The production
    # orchestrator runs execution every minute, so this is what actually
    # happens rather than a test contrivance.
    print("\nExecution past the flat deadline:")
    first_pass = run_paper_execution(
        database_path=args.test_database, spec_path=args.spec,
        run_id=FLATTEN_RUN_ID, run_mode="local_replay",
        as_of=parse_utc_iso(after_deadline_bar),
    )
    with sqlite3.connect(args.test_database, timeout=5.0) as connection:
        fill_bar = connection.execute(
            """
            SELECT bar_timestamp_utc FROM market_bars_1m
            WHERE symbol = ? AND is_complete = 1
              AND bar_timestamp_utc > ?
            ORDER BY bar_timestamp_utc LIMIT 1
            """,
            (str(fx_symbol), after_deadline_bar),
        ).fetchone()[0]
    flatten_result = run_paper_execution(
        database_path=args.test_database, spec_path=args.spec,
        run_id=FLATTEN_RUN_ID, run_mode="local_replay",
        as_of=parse_utc_iso(str(fill_bar)),
    )

    with sqlite3.connect(args.test_database, timeout=5.0) as connection:
        configure(connection)
        position = connection.execute(
            """
            SELECT status, exit_reason, closed_at_utc FROM positions
            WHERE run_id = ? LIMIT 1
            """,
            (FLATTEN_RUN_ID,),
        ).fetchone()
        still_open = connection.execute(
            "SELECT COUNT(*) FROM positions WHERE run_id = ? AND status = 'open'",
            (FLATTEN_RUN_ID,),
        ).fetchone()[0]

    check("position was closed", position[0] if position else None, "closed")
    check("closed for session_close",
          position[1] if position else None, "session_close")
    check("nothing left open past the deadline", int(still_open), 0)
    check("heartbeat reports enforcement on",
          flatten_result["session_close_enforcement"], "enforced")
    # Whichever pass the fill lands on is a timing detail of which bar was
    # available, not a property worth asserting; that it is counted exactly
    # once across the deadline is.
    check("session close counted exactly once",
          first_pass["session_closes_filled"]
          + flatten_result["session_closes_filled"], 1)
    check("session close was triggered",
          first_pass["session_closes_triggered"] >= 1, True)

    if position and position[2]:
        closed_at = parse_utc_iso(str(position[2]))
        check("close is not before the deadline",
              closed_at >= deadline.deadline_utc, True)

    print()
    if FAILURES:
        print(f"SESSION ENFORCEMENT TESTS FAILED: {len(FAILURES)}")
        for name in FAILURES:
            print(f"  - {name}")
        raise SystemExit(1)

    print("SESSION ENFORCEMENT TESTS PASSED")


if __name__ == "__main__":
    main()
