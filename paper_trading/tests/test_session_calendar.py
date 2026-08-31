"""Checks on the exchange-session calendar.

The rule this supports — never hold a position overnight — is the one the
original handoff called the most important blocker before formal paper
trading, so the arithmetic under it is worth pinning down explicitly.

Run:  python3 -m paper_trading.tests.test_session_calendar
"""

from __future__ import annotations

from datetime import datetime, timezone

from paper_trading.sessions.session_calendar import (
    SessionCalendar,
    UnknownVenueError,
)


FAILURES: list[str] = []


def check(name: str, actual: object, expected: object) -> None:
    if actual == expected:
        print(f"  ok    {name}")
        return
    FAILURES.append(name)
    print(f"  FAIL  {name}\n          expected {expected!r}\n          actual   {actual!r}")


def iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def main() -> None:
    calendar = SessionCalendar()

    print("Daylight saving is handled by the tz database, not a fixed offset:")
    # 16:59 America/New_York is 20:59 UTC in summer and 21:59 UTC in winter.
    # Getting this wrong would flatten positions an hour early or an hour late
    # for roughly half of every year.
    summer = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    winter = datetime(2026, 1, 20, 12, 0, tzinfo=timezone.utc)
    check(
        "COMEX close, August (EDT)",
        iso(calendar.next_close_utc("COMEX", summer)),
        "2026-08-25T20:59:00+00:00",
    )
    check(
        "COMEX close, January (EST)",
        iso(calendar.next_close_utc("COMEX", winter)),
        "2026-01-20T21:59:00+00:00",
    )

    print("\nA close that has already passed rolls to the next day:")
    after_close = datetime(2026, 8, 25, 21, 30, tzinfo=timezone.utc)
    check(
        "COMEX close asked after today's close",
        iso(calendar.next_close_utc("COMEX", after_close)),
        "2026-08-26T20:59:00+00:00",
    )

    print("\nContinuously-traded venues offer no close of their own:")
    check("FX is continuous", calendar.is_continuous("FX"), True)
    check("DERIVED is continuous", calendar.is_continuous("DERIVED"), True)
    check(
        "continuous venue has no close",
        calendar.next_close_utc("FX", summer),
        None,
    )
    check("COMEX is not continuous", calendar.is_continuous("COMEX"), False)

    print("\nThe binding leg is whichever closes first:")
    # CBOT closes 14:19 ET, well before the 17:00 ET FX rollover.
    cbot = calendar.flat_deadline(
        commodity_venue="CBOT", fx_venue="FX", as_of=summer
    )
    check("CBOT deadline is its own close", cbot.binding_leg, "commodity_session_close")
    check("CBOT deadline time", iso(cbot.deadline_utc), "2026-08-25T18:19:00+00:00")

    # SGX's next close (05:14 Singapore, next day) falls after the rollover.
    sgx = calendar.flat_deadline(
        commodity_venue="SGX", fx_venue="FX", as_of=summer
    )
    check("SGX deadline is the FX rollover", sgx.binding_leg, "fx_rollover")
    check("SGX deadline time", iso(sgx.deadline_utc), "2026-08-25T21:00:00+00:00")

    print("\nThe deadline is never later than the FX rollover:")
    for venue in ("COMEX", "NYMEX", "CBOT", "ICEUS", "CME", "AMEX", "SGX", "RUS"):
        deadline = calendar.flat_deadline(
            commodity_venue=venue, fx_venue="FX", as_of=summer
        )
        check(
            f"{venue} deadline <= rollover",
            deadline.deadline_utc <= deadline.fx_rollover_utc,
            True,
        )

    print("\nPre-close entry blackout:")
    # 20:35 UTC is 24 minutes before COMEX's 20:59 close — inside a 30-minute
    # blackout, outside a 15-minute one.
    near_close = datetime(2026, 8, 25, 20, 35, tzinfo=timezone.utc)
    blocked_30, _ = calendar.entry_blocked(
        commodity_venue="COMEX", fx_venue="FX",
        as_of=near_close, block_minutes=30,
    )
    blocked_15, _ = calendar.entry_blocked(
        commodity_venue="COMEX", fx_venue="FX",
        as_of=near_close, block_minutes=15,
    )
    check("24m before close is blocked at 30m", blocked_30, True)
    check("24m before close is allowed at 15m", blocked_15, False)

    print("\nFlat enforcement fires at the deadline, not before:")
    opened = datetime(2026, 8, 25, 18, 0, tzinfo=timezone.utc)
    one_minute_before = datetime(2026, 8, 25, 20, 58, tzinfo=timezone.utc)
    at_deadline = datetime(2026, 8, 25, 20, 59, tzinfo=timezone.utc)
    flat_before, _ = calendar.must_be_flat(
        commodity_venue="COMEX", fx_venue="FX",
        opened_at=opened, as_of=one_minute_before,
    )
    flat_at, _ = calendar.must_be_flat(
        commodity_venue="COMEX", fx_venue="FX",
        opened_at=opened, as_of=at_deadline,
    )
    check("not yet flat one minute before", flat_before, False)
    check("flat at the deadline", flat_at, True)

    print("\nStill overdue well past the deadline (the regression that matters):")
    # Anchoring the deadline to "now" rather than to the open makes this
    # come back False once the close has passed, because the next close is
    # then tomorrow's -- and the position quietly survives the night.
    long_after = datetime(2026, 8, 26, 3, 0, tzinfo=timezone.utc)
    flat_long_after, overdue = calendar.must_be_flat(
        commodity_venue="COMEX", fx_venue="FX",
        opened_at=opened, as_of=long_after,
    )
    check("still overdue hours later", flat_long_after, True)
    check("deadline stays the open day's close",
          iso(overdue.deadline_utc), "2026-08-25T20:59:00+00:00")

    print("\nAn unknown venue is refused rather than assumed open:")
    try:
        calendar.flat_deadline(
            commodity_venue="NOT_A_VENUE", fx_venue="FX", as_of=summer
        )
        check("unknown venue raises", False, True)
    except UnknownVenueError:
        check("unknown venue raises", True, True)

    try:
        calendar.flat_deadline(
            commodity_venue="COMEX", fx_venue="NOT_A_VENUE", as_of=summer
        )
        check("unknown FX venue raises", False, True)
    except UnknownVenueError:
        check("unknown FX venue raises", True, True)

    print("\nEvery venue in the live registry has a session definition:")
    import sqlite3
    from pathlib import Path

    database = Path("paper_trading/data/paper_trading.db")
    if database.exists():
        connection = sqlite3.connect(database)
        venues = connection.execute(
            """
            SELECT DISTINCT commodity_venue FROM live_instrument_registry
            UNION
            SELECT DISTINCT fx_venue FROM live_instrument_registry
            """
        ).fetchall()
        connection.close()
        for (venue,) in sorted(venues):
            try:
                calendar.venue(venue)
                check(f"{venue} is defined", True, True)
            except UnknownVenueError:
                check(f"{venue} is defined", False, True)
    else:
        print("  skip  no local database to check the registry against")

    print()
    if FAILURES:
        print(f"SESSION CALENDAR TESTS FAILED: {len(FAILURES)}")
        for name in FAILURES:
            print(f"  - {name}")
        raise SystemExit(1)

    print("SESSION CALENDAR TESTS PASSED")


if __name__ == "__main__":
    main()
