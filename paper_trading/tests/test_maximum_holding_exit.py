"""Checks that the maximum-holding-time exit actually closes a position.

This exit had never worked. It was anchored to the *current* bar, and
next_fill_bar wants a bar strictly after the trigger and at or before as_of
-- so the window was empty on every cycle, the trigger advanced along with
the bar, and the exit re-fired each minute forever. It was found in
production holding a position for 450 minutes against a 240-minute limit,
having written an exit decision every minute for three and a half hours.

The existing lifecycle test did not catch it because that position exits via
divergence_convergence, which anchors to a feature timestamp and fills
normally. Nothing exercised the time stop. Hence this.

The distinction worth testing is not "does an exit decision get written" --
that always happened -- but "does the position actually close", which is a
different question and the one that mattered.

Run:  python3 -m paper_trading.tests.test_maximum_holding_exit
"""

from __future__ import annotations

import argparse
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
DEFAULT_TEST_DB = Path("/tmp/maximum_holding_exit.db")
RUN_ID = "commodity_fx_intraday-holding-limit-test"

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

    if args.test_database.exists():
        args.test_database.unlink()
    shutil.copy2(args.source_database, args.test_database)

    spec = load_intraday_spec(args.spec)
    equity = float(spec["capital"]["initial_equity_usd"])
    limit_minutes = float(spec["exits"]["maximum_holding_time"]["minutes"])
    check("maximum_holding_time is enabled",
          bool(spec["exits"]["maximum_holding_time"]["enabled"]), True)

    calendar = SessionCalendar()

    with sqlite3.connect(args.test_database, timeout=5.0) as connection:
        configure(connection)
        (
            source_feature_id,
            _run,
            _hist_spec,
            relationship_id,
            _ts,
            fx_symbol,
        ) = select_candidate(connection)
        spec_id = ensure_strategy_spec(connection, spec)
        commodity_venue, fx_venue = relationship_venues(
            connection, relationship_id=str(relationship_id)
        )

        # An entry with enough bars after it to pass the holding limit and
        # still have something to fill against, and far enough from the flat
        # deadline that session_close does not pre-empt the exit under test.
        stamps = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT bar_timestamp_utc FROM market_bars_1m
                WHERE symbol = ? AND is_complete = 1
                ORDER BY bar_timestamp_utc
                """,
                (str(fx_symbol),),
            ).fetchall()
        ]
        entry = fill = after_limit = None
        for candidate in stamps:
            as_of = parse_utc_iso(candidate)
            deadline = calendar.flat_deadline(
                commodity_venue=commodity_venue,
                fx_venue=fx_venue,
                as_of=as_of,
            )
            # The session deadline must land beyond the holding limit,
            # otherwise the position is flattened before the time stop.
            if deadline.minutes_remaining(as_of) <= limit_minutes + 20:
                continue
            limit_at = as_of + timedelta(minutes=limit_minutes)
            later = [s for s in stamps if parse_utc_iso(s) > limit_at]
            follow = [
                s
                for s in stamps
                if 0 < (parse_utc_iso(s) - as_of).total_seconds() <= 180
            ]
            if later and follow:
                entry, fill, after_limit = candidate, follow[0], later[0]
                break

        if entry is None:
            raise SystemExit(
                "No bar in the dataset has both a holding-limit horizon and "
                "a session deadline beyond it."
            )

        print(
            f"\nrelationship {relationship_id}"
            f"\n  entry        {entry}"
            f"\n  holding limit {utc_iso(parse_utc_iso(entry) + timedelta(minutes=limit_minutes))}"
            f"\n  evaluated at  {after_limit}\n"
        )

        connection.execute(
            """
            INSERT INTO paper_runs (
                run_id, spec_id, run_mode, status,
                initial_equity_usd, started_at_utc, stopped_at_utc, notes
            ) VALUES (?, ?, 'local_replay', 'running', ?, ?, NULL, ?)
            """,
            (RUN_ID, spec_id, equity, entry, "Holding-limit test."),
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
        connection.commit()

    opened = run_paper_execution(
        database_path=args.test_database, spec_path=args.spec,
        run_id=RUN_ID, run_mode="local_replay",
        as_of=parse_utc_iso(fill),
    )
    check("position opened", opened["entries_filled"], 1)

    # Two passes past the limit, since an exit fills on the bar after its
    # trigger just like an entry does.
    run_paper_execution(
        database_path=args.test_database, spec_path=args.spec,
        run_id=RUN_ID, run_mode="local_replay",
        as_of=parse_utc_iso(after_limit),
    )
    with sqlite3.connect(args.test_database, timeout=5.0) as connection:
        nxt = connection.execute(
            """
            SELECT bar_timestamp_utc FROM market_bars_1m
            WHERE symbol = ? AND is_complete = 1 AND bar_timestamp_utc > ?
            ORDER BY bar_timestamp_utc LIMIT 1
            """,
            (str(fx_symbol), after_limit),
        ).fetchone()
    if nxt:
        run_paper_execution(
            database_path=args.test_database, spec_path=args.spec,
            run_id=RUN_ID, run_mode="local_replay",
            as_of=parse_utc_iso(str(nxt[0])),
        )

    with sqlite3.connect(args.test_database, timeout=5.0) as connection:
        configure(connection)
        position = connection.execute(
            """
            SELECT status, exit_reason, closed_at_utc, opened_at_utc
            FROM positions WHERE run_id = ? LIMIT 1
            """,
            (RUN_ID,),
        ).fetchone()
        still_open = connection.execute(
            "SELECT COUNT(*) FROM positions WHERE run_id=? AND status='open'",
            (RUN_ID,),
        ).fetchone()[0]
        exit_decisions = connection.execute(
            """
            SELECT COUNT(*) FROM signal_decisions
            WHERE run_id = ? AND decision_type = 'exit'
            """,
            (RUN_ID,),
        ).fetchone()[0]

    print("Exit:")
    check("position closed", position[0] if position else None, "closed")
    check("closed for maximum_holding_time",
          position[1] if position else None, "maximum_holding_time")
    check("nothing left open", int(still_open), 0)

    # The old behaviour wrote one exit decision per cycle forever. A handful
    # is normal (trigger, then fill); dozens means it is re-firing again.
    check("did not re-fire every cycle", exit_decisions <= 3, True)

    if position and position[2] and position[3]:
        held = (
            parse_utc_iso(str(position[2])) - parse_utc_iso(str(position[3]))
        ).total_seconds() / 60.0
        print(f"\n  held {held:.0f} minutes against a {limit_minutes:.0f} limit")
        # One extra bar for the fill is expected; hours are not.
        check("closed close to the limit", held <= limit_minutes + 15, True)

    print()
    if FAILURES:
        print(f"MAXIMUM HOLDING EXIT TESTS FAILED: {len(FAILURES)}")
        for name in FAILURES:
            print(f"  - {name}")
        raise SystemExit(1)
    print("MAXIMUM HOLDING EXIT TESTS PASSED")


if __name__ == "__main__":
    main()
