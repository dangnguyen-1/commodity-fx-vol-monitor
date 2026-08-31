"""Exchange-session arithmetic for the intraday strategy.

This exists to enforce two rules the spec has always stated but nothing has
ever implemented:

    sessions.allow_overnight_positions: false
    sessions.block_new_entries_before_market_close_minutes: 30

The execution engine's own heartbeat has been reporting
``"session_close_enforcement": "deferred_to_relationship_exchange_calendar_scheduler"``
since the engine was written — deferring to a scheduler that did not exist.
This module is that scheduler's calendar.

The awkward part, and the reason this is not simply "look up the exchange's
closing time": the position the strategy actually holds is the **FX** leg,
and FX trades continuously. Only the *signal* leg — the commodity future —
has a real daily close. So a relationship's flat deadline is taken as the
earliest of:

  * its commodity venue's session close, after which the signal input is
    stale and the position is running blind, and
  * the 17:00 New York FX rollover, which is where the FX trading day turns
    over and where the weekend break begins on a Friday.

Session times themselves live in
``strategy/config/intraday/exchange_sessions.yaml`` and were derived from our
own bar data; see the commentary there.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SESSIONS_PATH = (
    PROJECT_ROOT
    / "strategy"
    / "config"
    / "intraday"
    / "exchange_sessions.yaml"
)


class UnknownVenueError(LookupError):
    """Raised for a venue the session config says nothing about.

    Deliberately an error rather than a permissive default: treating an
    unrecognised venue as always-open is precisely how a position ends up
    held overnight.
    """


@dataclass(frozen=True)
class FlatDeadline:
    """When a relationship must be flat, and which leg decided it."""

    deadline_utc: datetime
    binding_leg: str
    commodity_close_utc: datetime | None
    fx_rollover_utc: datetime

    def minutes_remaining(self, as_of: datetime) -> float:
        return (
            self.deadline_utc - as_of
        ).total_seconds() / 60.0


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def load_sessions(
    path: Path = DEFAULT_SESSIONS_PATH,
) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)

    for key in ("venues", "fx_rollover"):
        if key not in config:
            raise ValueError(
                f"Session config is missing '{key}': {path}"
            )

    return config


class SessionCalendar:
    """Answers session questions for a venue or a relationship."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        path: Path = DEFAULT_SESSIONS_PATH,
    ) -> None:
        self._config = (
            config if config is not None else load_sessions(path)
        )
        self._venues = self._config["venues"]
        rollover = self._config["fx_rollover"]
        self._fx_zone = ZoneInfo(rollover["timezone"])
        self._fx_time = _parse_hhmm(rollover["time"])

    # -- venue-level ----------------------------------------------------

    def venue(self, name: str) -> dict[str, Any]:
        try:
            return self._venues[name]
        except KeyError as error:
            raise UnknownVenueError(
                f"No session definition for venue {name!r}. "
                "Add it to exchange_sessions.yaml before trading it."
            ) from error

    def is_continuous(self, name: str) -> bool:
        return bool(self.venue(name).get("continuous", False))

    def next_close_utc(
        self,
        name: str,
        as_of: datetime,
    ) -> datetime | None:
        """The venue's next session close at or after ``as_of``.

        ``None`` for a continuously-traded venue, which has no close of its
        own to offer.
        """
        venue = self.venue(name)
        if venue.get("continuous", False):
            return None

        zone = ZoneInfo(venue["timezone"])
        close_time = _parse_hhmm(venue["close"])
        local = as_of.astimezone(zone)

        # Look at today and tomorrow: "today's close" may already have passed,
        # and constructing the local datetime then converting back to UTC is
        # what makes daylight saving transitions come out right.
        for offset in (0, 1):
            candidate_day = local.date() + timedelta(days=offset)
            candidate = datetime.combine(
                candidate_day, close_time, tzinfo=zone
            ).astimezone(timezone.utc)
            if candidate >= as_of:
                return candidate

        raise RuntimeError(
            f"Could not find a next close for {name!r} after {as_of!r}."
        )

    # -- FX rollover ----------------------------------------------------

    def next_fx_rollover_utc(self, as_of: datetime) -> datetime:
        local = as_of.astimezone(self._fx_zone)
        for offset in (0, 1):
            candidate_day = local.date() + timedelta(days=offset)
            candidate = datetime.combine(
                candidate_day, self._fx_time, tzinfo=self._fx_zone
            ).astimezone(timezone.utc)
            if candidate >= as_of:
                return candidate

        raise RuntimeError(
            f"Could not find an FX rollover after {as_of!r}."
        )

    # -- relationship-level ---------------------------------------------

    def flat_deadline(
        self,
        *,
        commodity_venue: str,
        fx_venue: str,
        as_of: datetime,
    ) -> FlatDeadline:
        """When a position on this relationship must be flat.

        The earliest of the commodity venue's close and the FX rollover. Both
        legs are consulted even though only the FX leg is held: a position
        whose signal leg has stopped trading is running on a stale input,
        which is no better than holding it overnight.
        """
        # Validate the FX venue even though a continuous venue contributes no
        # close, so that an unrecognised FX venue is still caught here rather
        # than silently ignored.
        self.venue(fx_venue)

        commodity_close = self.next_close_utc(commodity_venue, as_of)
        fx_rollover = self.next_fx_rollover_utc(as_of)

        if commodity_close is None or fx_rollover <= commodity_close:
            return FlatDeadline(
                deadline_utc=fx_rollover,
                binding_leg="fx_rollover",
                commodity_close_utc=commodity_close,
                fx_rollover_utc=fx_rollover,
            )

        return FlatDeadline(
            deadline_utc=commodity_close,
            binding_leg="commodity_session_close",
            commodity_close_utc=commodity_close,
            fx_rollover_utc=fx_rollover,
        )

    def must_be_flat(
        self,
        *,
        commodity_venue: str,
        fx_venue: str,
        opened_at: datetime,
        as_of: datetime,
    ) -> tuple[bool, FlatDeadline]:
        """Whether a position opened at ``opened_at`` is past its deadline.

        The deadline is anchored to when the position was *opened*, not to
        the current time, and that distinction is the whole point. Computing
        it from "now" means that once now is past the close, the next close
        is tomorrow's — the position stops looking overdue and quietly
        survives the night, which is the failure this rule exists to
        prevent. Anchored to the open, the deadline is a fixed point that
        ``as_of`` can only move further beyond.
        """
        deadline = self.flat_deadline(
            commodity_venue=commodity_venue,
            fx_venue=fx_venue,
            as_of=opened_at,
        )
        return as_of >= deadline.deadline_utc, deadline

    def entry_blocked(
        self,
        *,
        commodity_venue: str,
        fx_venue: str,
        as_of: datetime,
        block_minutes: float,
    ) -> tuple[bool, FlatDeadline]:
        """Whether a new entry is inside the pre-close blackout.

        A position opened this close to the deadline would be force-closed
        almost immediately, paying both sides of the spread for nothing.
        """
        deadline = self.flat_deadline(
            commodity_venue=commodity_venue,
            fx_venue=fx_venue,
            as_of=as_of,
        )
        return (
            deadline.minutes_remaining(as_of) <= block_minutes,
            deadline,
        )
