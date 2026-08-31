from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import statistics
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from paper_trading.sessions.session_calendar import (
    SessionCalendar,
)
from strategy.config.intraday.load_intraday_spec import (
    DEFAULT_SPEC_PATH,
    load_intraday_spec,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = (
    PROJECT_ROOT
    / "paper_trading"
    / "data"
    / "paper_trading.db"
)
EXECUTION_SERVICE_NAME = "paper_execution_engine"
BPS_DENOMINATOR = 10_000.0


@dataclass(frozen=True)
class EntryDecision:
    decision_id: str
    run_id: str
    spec_id: int
    feature_id: int
    relationship_id: str
    decision_timestamp: datetime
    decision_type: str
    signal_strength: float
    commodity: str
    currency: str
    fx_symbol: str
    realized_volatility_60m: float
    selection_weight: float

    @property
    def direction(self) -> int:
        return 1 if self.decision_type == "enter_long" else -1


@dataclass(frozen=True)
class OpenPosition:
    position_id: str
    run_id: str
    relationship_id: str
    direction: int
    opened_at: datetime
    entry_fill_id: str
    entry_price: float
    position_size_usd: float
    entry_quantity: float
    commodity: str
    currency: str
    fx_symbol: str


@dataclass(frozen=True)
class Bar:
    symbol: str
    timestamp: datetime
    open_price: float
    close_price: float
    source_name: str


@dataclass(frozen=True)
class FillModel:
    expected_fill_price: float
    simulated_fill_price: float
    bid_price: float | None
    ask_price: float | None
    spread_bps: float
    expected_round_trip_cost_bps: float
    spread_cost_usd: float
    slippage_cost_usd: float
    total_transaction_cost_usd: float
    fill_source: str


@dataclass(frozen=True)
class PortfolioState:
    initial_equity_usd: float
    realized_pnl_usd: float
    unrealized_pnl_usd: float
    transaction_cost_usd: float
    cash_usd: float
    total_equity_usd: float
    gross_exposure_usd: float
    net_exposure_usd: float
    open_positions: int
    drawdown_pct: float


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc).isoformat()


def parse_utc_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def finite_number(value: Any, name: str) -> float:
    result = float(value)

    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite.")

    return result


_SESSION_CALENDAR: SessionCalendar | None = None


def session_calendar() -> SessionCalendar:
    """The exchange-session calendar, loaded once per process."""
    global _SESSION_CALENDAR

    if _SESSION_CALENDAR is None:
        _SESSION_CALENDAR = SessionCalendar()

    return _SESSION_CALENDAR


def relationship_venues(
    connection: sqlite3.Connection,
    *,
    relationship_id: str,
) -> tuple[str, str]:
    """The commodity and FX venues a relationship trades on.

    Raises rather than defaulting: without knowing the venue there is no way
    to say when the position must be flat, and guessing is how a position
    ends up held overnight.
    """
    row = connection.execute(
        """
        SELECT commodity_venue, fx_venue
        FROM live_instrument_registry
        WHERE relationship_id = ?
        """,
        (relationship_id,),
    ).fetchone()

    if row is None:
        raise RuntimeError(
            "No live_instrument_registry row for "
            f"{relationship_id!r}; cannot determine its session."
        )

    return str(row[0]), str(row[1])


def overnight_positions_allowed(spec: dict[str, Any]) -> bool:
    return bool(
        spec["sessions"]["allow_overnight_positions"]
    )


def finite_positive(value: Any, name: str) -> float:
    result = finite_number(value, name)

    if result <= 0:
        raise ValueError(f"{name} must be positive.")

    return result


def sign(value: float, tolerance: float = 1e-12) -> int:
    if value > tolerance:
        return 1
    if value < -tolerance:
        return -1
    return 0


def configure_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")


def require_tables(connection: sqlite3.Connection) -> None:
    required = {
        "strategy_specs",
        "paper_runs",
        "relationships",
        "relationship_weights",
        "market_bars_1m",
        "market_quotes",
        "feature_snapshots",
        "signal_decisions",
        "orders",
        "fills",
        "positions",
        "position_marks",
        "equity_snapshots",
        "service_heartbeats",
        "system_alerts",
    }

    existing = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    missing = sorted(required - existing)

    if missing:
        raise RuntimeError(
            "Paper-trading database is missing required tables: "
            f"{missing}"
        )


def default_run_id(spec: dict[str, Any], run_mode: str) -> str:
    return (
        f"{spec['strategy']['name']}-"
        f"{run_mode}-"
        f"v{spec['strategy']['specification_version']}"
    )


def current_spec_sha256(spec: dict[str, Any]) -> str:
    path = Path(spec["_runtime"]["specification_path"])
    raw = path.read_text(encoding="utf-8")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_run_spec(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    run_mode: str,
    spec: dict[str, Any],
) -> tuple[int, float]:
    row = connection.execute(
        """
        SELECT
            pr.spec_id,
            pr.run_mode,
            pr.status,
            pr.initial_equity_usd,
            ss.spec_sha256
        FROM paper_runs pr
        JOIN strategy_specs ss
          ON ss.spec_id = pr.spec_id
        WHERE pr.run_id = ?
        """,
        (run_id,),
    ).fetchone()

    if row is None:
        raise RuntimeError(
            f"Paper run {run_id!r} does not exist."
        )

    spec_id = int(row[0])
    existing_mode = str(row[1])
    status = str(row[2])
    initial_equity = finite_positive(
        row[3], "initial_equity_usd"
    )
    stored_sha256 = str(row[4])

    if existing_mode != run_mode:
        raise RuntimeError(
            f"Run {run_id!r} uses mode {existing_mode!r}, "
            f"not {run_mode!r}."
        )

    if status in {"stopped", "failed"}:
        raise RuntimeError(
            f"Run {run_id!r} has terminal status {status!r}."
        )

    if stored_sha256 != current_spec_sha256(spec):
        raise RuntimeError(
            f"Run {run_id!r} was created from a different "
            "strategy specification. Use a new run ID."
        )

    return spec_id, initial_equity


def resolve_as_of(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    run_mode: str,
    requested: datetime | None,
    cancel_after_minutes: int,
) -> datetime:
    if requested is not None:
        return requested.astimezone(timezone.utc)

    if run_mode == "local_replay":
        row = connection.execute(
            """
            SELECT MAX(decision_timestamp_utc)
            FROM signal_decisions
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()

        if row is None or row[0] is None:
            raise RuntimeError(
                f"Run {run_id!r} has no signal decisions."
            )

        return parse_utc_iso(str(row[0])) + timedelta(
            minutes=cancel_after_minutes
        )

    now = utc_now()
    return now.replace(second=0, microsecond=0)


def deterministic_id(namespace: str, *parts: Any) -> str:
    key = "|".join([namespace, *(str(part) for part in parts)])
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def load_pending_entries(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    spec_id: int,
    as_of: datetime,
) -> list[EntryDecision]:
    rows = connection.execute(
        """
        SELECT
            d.decision_id,
            d.run_id,
            d.spec_id,
            d.feature_id,
            d.relationship_id,
            d.decision_timestamp_utc,
            d.decision_type,
            d.signal_strength,
            r.commodity,
            r.currency,
            r.fx_symbol,
            f.realized_volatility_60m,
            rw.selection_weight
        FROM signal_decisions d
        JOIN feature_snapshots f
          ON f.feature_id = d.feature_id
        JOIN relationships r
          ON r.relationship_id = d.relationship_id
        LEFT JOIN relationship_weights rw
          ON rw.relationship_id = d.relationship_id
         AND rw.selection_year = CAST(
             strftime('%Y', d.decision_timestamp_utc)
             AS INTEGER
         )
        LEFT JOIN orders o
          ON o.decision_id = d.decision_id
         AND o.order_action = 'open'
        WHERE d.run_id = ?
          AND d.spec_id = ?
          AND d.approved = 1
          AND d.decision_type IN (
              'enter_long',
              'enter_short'
          )
          AND d.decision_timestamp_utc <= ?
          AND o.order_id IS NULL
          AND f.market_data_complete = 1
          AND r.active = 1
        ORDER BY
            d.signal_strength DESC,
            d.decision_timestamp_utc,
            d.relationship_id
        """,
        (run_id, spec_id, utc_iso(as_of)),
    ).fetchall()

    decisions: list[EntryDecision] = []

    for row in rows:
        if row[3] is None:
            raise RuntimeError(
                f"Approved decision {row[0]!r} has no feature."
            )
        if row[7] is None:
            raise RuntimeError(
                f"Approved decision {row[0]!r} has no signal strength."
            )
        if row[11] is None or row[12] is None:
            raise RuntimeError(
                f"Approved decision {row[0]!r} is missing "
                "volatility or relationship weight."
            )

        decisions.append(
            EntryDecision(
                decision_id=str(row[0]),
                run_id=str(row[1]),
                spec_id=int(row[2]),
                feature_id=int(row[3]),
                relationship_id=str(row[4]),
                decision_timestamp=parse_utc_iso(str(row[5])),
                decision_type=str(row[6]),
                signal_strength=finite_positive(
                    row[7], "signal_strength"
                ),
                commodity=str(row[8]),
                currency=str(row[9]),
                fx_symbol=str(row[10]),
                realized_volatility_60m=finite_positive(
                    row[11], "realized_volatility_60m"
                ),
                selection_weight=finite_number(
                    row[12], "selection_weight"
                ),
            )
        )

    return decisions


def load_open_positions(
    connection: sqlite3.Connection,
    *,
    run_id: str,
) -> list[OpenPosition]:
    rows = connection.execute(
        """
        SELECT
            p.position_id,
            p.run_id,
            p.relationship_id,
            p.direction,
            p.opened_at_utc,
            p.entry_fill_id,
            p.entry_price,
            p.position_size_usd,
            ef.filled_notional_usd / ef.fill_price,
            r.commodity,
            r.currency,
            r.fx_symbol
        FROM positions p
        JOIN fills ef
          ON ef.fill_id = p.entry_fill_id
        JOIN relationships r
          ON r.relationship_id = p.relationship_id
        WHERE p.run_id = ?
          AND p.status = 'open'
        ORDER BY p.opened_at_utc, p.position_id
        """,
        (run_id,),
    ).fetchall()

    return [
        OpenPosition(
            position_id=str(row[0]),
            run_id=str(row[1]),
            relationship_id=str(row[2]),
            direction=int(row[3]),
            opened_at=parse_utc_iso(str(row[4])),
            entry_fill_id=str(row[5]),
            entry_price=finite_positive(row[6], "entry_price"),
            position_size_usd=finite_positive(
                row[7], "position_size_usd"
            ),
            entry_quantity=finite_positive(
                row[8], "entry_quantity"
            ),
            commodity=str(row[9]),
            currency=str(row[10]),
            fx_symbol=str(row[11]),
        )
        for row in rows
    ]


def latest_bar_at_or_before(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    timestamp: datetime,
) -> Bar | None:
    row = connection.execute(
        """
        SELECT
            symbol,
            bar_timestamp_utc,
            open_price,
            close_price,
            source_name
        FROM market_bars_1m
        WHERE symbol = ?
          AND is_complete = 1
          AND bar_timestamp_utc <= ?
        ORDER BY
            bar_timestamp_utc DESC,
            received_at_utc DESC
        LIMIT 1
        """,
        (symbol, utc_iso(timestamp)),
    ).fetchone()

    if row is None:
        return None

    return Bar(
        symbol=str(row[0]),
        timestamp=parse_utc_iso(str(row[1])),
        open_price=finite_positive(row[2], "open_price"),
        close_price=finite_positive(row[3], "close_price"),
        source_name=str(row[4]),
    )


def next_fill_bar(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    signal_timestamp: datetime,
    additional_delay_minutes: int,
    cancel_after_minutes: int,
    as_of: datetime,
) -> Bar | None:
    earliest = signal_timestamp + timedelta(
        minutes=additional_delay_minutes
    )
    deadline = min(
        signal_timestamp + timedelta(
            minutes=cancel_after_minutes
        ),
        as_of,
    )

    row = connection.execute(
        """
        SELECT
            symbol,
            bar_timestamp_utc,
            open_price,
            close_price,
            source_name
        FROM market_bars_1m
        WHERE symbol = ?
          AND is_complete = 1
          AND bar_timestamp_utc > ?
          AND bar_timestamp_utc <= ?
        ORDER BY
            bar_timestamp_utc,
            received_at_utc DESC
        LIMIT 1
        """,
        (symbol, utc_iso(earliest), utc_iso(deadline)),
    ).fetchone()

    if row is None:
        return None

    return Bar(
        symbol=str(row[0]),
        timestamp=parse_utc_iso(str(row[1])),
        open_price=finite_positive(row[2], "open_price"),
        close_price=finite_positive(row[3], "close_price"),
        source_name=str(row[4]),
    )


def latest_quote_near_bar(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    bar_timestamp: datetime,
    maximum_lateness_seconds: int,
) -> tuple[float, float, float, float, str] | None:
    lower = bar_timestamp - timedelta(
        seconds=maximum_lateness_seconds
    )
    row = connection.execute(
        """
        SELECT
            bid_price,
            ask_price,
            mid_price,
            spread_bps,
            source_name
        FROM market_quotes
        WHERE symbol = ?
          AND quote_timestamp_utc >= ?
          AND quote_timestamp_utc <= ?
        ORDER BY
            quote_timestamp_utc DESC,
            received_at_utc DESC
        LIMIT 1
        """,
        (symbol, utc_iso(lower), utc_iso(bar_timestamp)),
    ).fetchone()

    if row is None:
        return None

    return (
        finite_positive(row[0], "bid_price"),
        finite_positive(row[1], "ask_price"),
        finite_positive(row[2], "mid_price"),
        finite_number(row[3], "spread_bps"),
        str(row[4]),
    )


def build_fill_model(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    side: str,
    fill_bar: Bar,
    notional_usd: float,
    use_bid_ask: bool,
    fallback_round_trip_bps: float,
    slippage_per_side_bps: float,
    maximum_quote_lateness_seconds: int,
) -> FillModel:
    if side not in {"buy", "sell"}:
        raise ValueError(f"Unsupported side: {side}")

    quote = None
    if use_bid_ask:
        quote = latest_quote_near_bar(
            connection,
            symbol=symbol,
            bar_timestamp=fill_bar.timestamp,
            maximum_lateness_seconds=(
                maximum_quote_lateness_seconds
            ),
        )

    if quote is not None:
        bid, ask, mid, spread_bps, quote_source = quote
        expected = ask if side == "buy" else bid
        half_spread_cost = abs(expected - mid)
        spread_cost = (
            notional_usd * half_spread_cost / mid
        )
        fill_source = f"bid_ask:{quote_source}"
        bid_price: float | None = bid
        ask_price: float | None = ask
    else:
        mid = fill_bar.open_price
        spread_bps = fallback_round_trip_bps
        half_spread_fraction = (
            spread_bps / 2.0 / BPS_DENOMINATOR
        )
        expected = mid * (
            1.0 + half_spread_fraction
            if side == "buy"
            else 1.0 - half_spread_fraction
        )
        spread_cost = (
            notional_usd
            * spread_bps
            / 2.0
            / BPS_DENOMINATOR
        )
        fill_source = f"next_1m_open:{fill_bar.source_name}:fallback"
        bid_price = None
        ask_price = None

    slippage_fraction = (
        slippage_per_side_bps / BPS_DENOMINATOR
    )
    simulated = expected * (
        1.0 + slippage_fraction
        if side == "buy"
        else 1.0 - slippage_fraction
    )
    slippage_cost = (
        notional_usd
        * slippage_per_side_bps
        / BPS_DENOMINATOR
    )
    total_cost = spread_cost + slippage_cost
    expected_round_trip = (
        spread_bps + 2.0 * slippage_per_side_bps
    )

    return FillModel(
        expected_fill_price=finite_positive(
            expected, "expected_fill_price"
        ),
        simulated_fill_price=finite_positive(
            simulated, "simulated_fill_price"
        ),
        bid_price=bid_price,
        ask_price=ask_price,
        spread_bps=spread_bps,
        expected_round_trip_cost_bps=(
            expected_round_trip
        ),
        spread_cost_usd=spread_cost,
        slippage_cost_usd=slippage_cost,
        total_transaction_cost_usd=total_cost,
        fill_source=fill_source,
    )


def latest_mark_for_position(
    connection: sqlite3.Connection,
    *,
    position_id: str,
    at_or_before: datetime,
) -> tuple[datetime, float] | None:
    row = connection.execute(
        """
        SELECT mark_timestamp_utc, unrealized_pnl_usd
        FROM position_marks
        WHERE position_id = ?
          AND mark_timestamp_utc <= ?
        ORDER BY mark_timestamp_utc DESC
        LIMIT 1
        """,
        (position_id, utc_iso(at_or_before)),
    ).fetchone()

    if row is None:
        return None

    return parse_utc_iso(str(row[0])), finite_number(
        row[1], "unrealized_pnl_usd"
    )


def mark_open_positions(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    as_of: datetime,
) -> tuple[int, int]:
    written = 0
    missing = 0

    for position in load_open_positions(
        connection, run_id=run_id
    ):
        bar = latest_bar_at_or_before(
            connection,
            symbol=position.fx_symbol,
            timestamp=as_of,
        )

        if bar is None:
            missing += 1
            continue

        return_fraction = (
            bar.close_price / position.entry_price - 1.0
        )
        unrealized = (
            position.direction
            * position.position_size_usd
            * return_fraction
        )

        connection.execute(
            """
            INSERT INTO position_marks (
                run_id,
                position_id,
                mark_timestamp_utc,
                mark_price,
                unrealized_pnl_usd,
                mark_source
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                run_id,
                position_id,
                mark_timestamp_utc
            )
            DO UPDATE SET
                mark_price = excluded.mark_price,
                unrealized_pnl_usd = (
                    excluded.unrealized_pnl_usd
                ),
                mark_source = excluded.mark_source
            """,
            (
                run_id,
                position.position_id,
                utc_iso(bar.timestamp),
                bar.close_price,
                unrealized,
                f"1m_close:{bar.source_name}",
            ),
        )
        written += 1

    return written, missing


def portfolio_state(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    initial_equity_usd: float,
    as_of: datetime,
) -> PortfolioState:
    closed = connection.execute(
        """
        SELECT COALESCE(SUM(gross_pnl_usd), 0.0)
        FROM positions
        WHERE run_id = ?
          AND status = 'closed'
          AND closed_at_utc <= ?
        """,
        (run_id, utc_iso(as_of)),
    ).fetchone()
    realized = finite_number(
        closed[0] if closed is not None else 0.0,
        "realized_pnl_usd",
    )

    costs = connection.execute(
        """
        SELECT COALESCE(SUM(f.total_transaction_cost_usd), 0.0)
        FROM fills f
        JOIN orders o
          ON o.order_id = f.order_id
        WHERE o.run_id = ?
          AND f.fill_timestamp_utc <= ?
        """,
        (run_id, utc_iso(as_of)),
    ).fetchone()
    transaction_cost = finite_number(
        costs[0] if costs is not None else 0.0,
        "transaction_cost_usd",
    )

    open_rows = connection.execute(
        """
        SELECT
            p.position_id,
            p.direction,
            p.position_size_usd
        FROM positions p
        WHERE p.run_id = ?
          AND p.status = 'open'
          AND p.opened_at_utc <= ?
        """,
        (run_id, utc_iso(as_of)),
    ).fetchall()

    unrealized = 0.0
    gross_exposure = 0.0
    net_exposure = 0.0

    for position_id, direction, size in open_rows:
        position_size = finite_positive(
            size, "position_size_usd"
        )
        gross_exposure += position_size
        net_exposure += int(direction) * position_size

        mark = latest_mark_for_position(
            connection,
            position_id=str(position_id),
            at_or_before=as_of,
        )
        if mark is not None:
            unrealized += mark[1]

    cash = initial_equity_usd + realized - transaction_cost
    total_equity = cash + unrealized

    peak_row = connection.execute(
        """
        SELECT MAX(total_equity_usd)
        FROM equity_snapshots
        WHERE run_id = ?
          AND snapshot_timestamp_utc <= ?
        """,
        (run_id, utc_iso(as_of)),
    ).fetchone()
    previous_peak = (
        finite_number(peak_row[0], "peak_equity")
        if peak_row is not None and peak_row[0] is not None
        else initial_equity_usd
    )
    peak = max(initial_equity_usd, previous_peak, total_equity)
    drawdown_pct = (
        0.0
        if peak <= 0
        else 100.0 * (total_equity / peak - 1.0)
    )

    return PortfolioState(
        initial_equity_usd=initial_equity_usd,
        realized_pnl_usd=realized,
        unrealized_pnl_usd=unrealized,
        transaction_cost_usd=transaction_cost,
        cash_usd=cash,
        total_equity_usd=total_equity,
        gross_exposure_usd=gross_exposure,
        net_exposure_usd=net_exposure,
        open_positions=len(open_rows),
        drawdown_pct=drawdown_pct,
    )


def daily_loss_pct(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    current_equity: float,
    as_of: datetime,
    initial_equity_usd: float,
) -> float:
    day_start = as_of.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    row = connection.execute(
        """
        SELECT total_equity_usd
        FROM equity_snapshots
        WHERE run_id = ?
          AND snapshot_timestamp_utc >= ?
          AND snapshot_timestamp_utc <= ?
        ORDER BY snapshot_timestamp_utc
        LIMIT 1
        """,
        (run_id, utc_iso(day_start), utc_iso(as_of)),
    ).fetchone()
    baseline = (
        finite_positive(row[0], "day_start_equity")
        if row is not None
        else initial_equity_usd
    )
    return 100.0 * (current_equity / baseline - 1.0)


def currency_exposure_usd(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    currency: str,
) -> float:
    row = connection.execute(
        """
        SELECT COALESCE(SUM(p.position_size_usd), 0.0)
        FROM positions p
        JOIN relationships r
          ON r.relationship_id = p.relationship_id
        WHERE p.run_id = ?
          AND p.status = 'open'
          AND r.currency = ?
        """,
        (run_id, currency),
    ).fetchone()
    return finite_number(
        row[0] if row is not None else 0.0,
        "currency_exposure_usd",
    )


def volatility_scalar(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    feature_timestamp: datetime,
    own_volatility: float,
    floor: float,
) -> tuple[float, float]:
    rows = connection.execute(
        """
        SELECT realized_volatility_60m
        FROM feature_snapshots
        WHERE run_id = ?
          AND feature_timestamp_utc = ?
          AND market_data_complete = 1
          AND realized_volatility_60m IS NOT NULL
          AND realized_volatility_60m > 0
        """,
        (run_id, utc_iso(feature_timestamp)),
    ).fetchall()

    values = [
        max(floor, finite_positive(row[0], "realized_volatility"))
        for row in rows
    ]
    benchmark = (
        statistics.median(values)
        if values
        else max(floor, own_volatility)
    )
    own = max(floor, own_volatility)

    # Volatility scaling may reduce a high-volatility position, but it
    # never increases notional beyond the configured relationship cap.
    scalar = min(1.0, benchmark / own)
    return scalar, benchmark


def requested_entry_notional(
    connection: sqlite3.Connection,
    *,
    decision: EntryDecision,
    state: PortfolioState,
    spec: dict[str, Any],
) -> tuple[float, dict[str, float]]:
    risk = spec["risk"]
    sizing = spec["position_sizing"]
    normalization = spec["features"]["normalization"]

    relationship_cap = (
        state.total_equity_usd
        * float(risk["maximum_relationship_exposure_pct"])
    )
    strength_cap = float(sizing["signal_strength_cap"])
    strength_ratio = min(
        1.0,
        max(0.0, decision.signal_strength / strength_cap),
    )

    if bool(sizing["volatility_scaling_enabled"]):
        vol_scalar, benchmark_vol = volatility_scalar(
            connection,
            run_id=decision.run_id,
            feature_timestamp=decision.decision_timestamp,
            own_volatility=decision.realized_volatility_60m,
            floor=float(
                normalization["minimum_volatility_floor"]
            ),
        )
    else:
        vol_scalar = 1.0
        benchmark_vol = decision.realized_volatility_60m

    requested = relationship_cap * strength_ratio * vol_scalar

    return requested, {
        "relationship_cap_usd": relationship_cap,
        "signal_strength_ratio": strength_ratio,
        "volatility_scalar": vol_scalar,
        "volatility_benchmark_60m": benchmark_vol,
    }


def available_entry_capacity(
    connection: sqlite3.Connection,
    *,
    decision: EntryDecision,
    state: PortfolioState,
    spec: dict[str, Any],
) -> tuple[float, dict[str, float]]:
    risk = spec["risk"]
    equity = state.total_equity_usd

    gross_cap = equity * float(
        risk["maximum_gross_exposure_pct"]
    )
    gross_available = max(
        0.0, gross_cap - state.gross_exposure_usd
    )

    currency_cap = equity * float(
        risk["maximum_currency_exposure_pct"]
    )
    current_currency = currency_exposure_usd(
        connection,
        run_id=decision.run_id,
        currency=decision.currency,
    )
    currency_available = max(
        0.0, currency_cap - current_currency
    )

    relationship_cap = equity * float(
        risk["maximum_relationship_exposure_pct"]
    )

    return min(
        relationship_cap,
        gross_available,
        currency_available,
    ), {
        "relationship_cap_usd": relationship_cap,
        "gross_cap_usd": gross_cap,
        "gross_available_usd": gross_available,
        "currency_cap_usd": currency_cap,
        "currency_exposure_usd": current_currency,
        "currency_available_usd": currency_available,
    }


def create_alert(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    timestamp: datetime,
    severity: str,
    alert_type: str,
    message: str,
    details: dict[str, Any],
) -> None:
    alert_id = deterministic_id(
        "paper-alert",
        run_id,
        alert_type,
        utc_iso(timestamp),
        details.get("relationship_id", ""),
        details.get("decision_id", ""),
    )
    connection.execute(
        """
        INSERT INTO system_alerts (
            alert_id,
            run_id,
            alert_timestamp_utc,
            severity,
            service_name,
            alert_type,
            message,
            details_json,
            resolved,
            resolved_at_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
        ON CONFLICT(alert_id)
        DO UPDATE SET
            severity = excluded.severity,
            message = excluded.message,
            details_json = excluded.details_json
        """,
        (
            alert_id,
            run_id,
            utc_iso(timestamp),
            severity,
            EXECUTION_SERVICE_NAME,
            alert_type,
            message,
            json.dumps(details, sort_keys=True, separators=(",", ":")),
        ),
    )


def write_order(
    connection: sqlite3.Connection,
    *,
    order_id: str,
    run_id: str,
    decision_id: str,
    relationship_id: str,
    side: str,
    action: str,
    notional_usd: float,
    quantity: float | None,
    signal_price: float,
    expected_round_trip_cost_bps: float,
    submitted_at: datetime,
    status: str,
    rejection_reason: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO orders (
            order_id,
            run_id,
            decision_id,
            relationship_id,
            side,
            order_action,
            order_type,
            notional_usd,
            quantity,
            signal_price,
            expected_round_trip_cost_bps,
            submitted_at_utc,
            status,
            rejection_reason
        )
        VALUES (?, ?, ?, ?, ?, ?, 'simulated_market', ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(order_id)
        DO UPDATE SET
            quantity = excluded.quantity,
            expected_round_trip_cost_bps = (
                excluded.expected_round_trip_cost_bps
            ),
            status = excluded.status,
            rejection_reason = excluded.rejection_reason
        """,
        (
            order_id,
            run_id,
            decision_id,
            relationship_id,
            side,
            action,
            notional_usd,
            quantity,
            signal_price,
            expected_round_trip_cost_bps,
            utc_iso(submitted_at),
            status,
            rejection_reason,
        ),
    )


def write_fill(
    connection: sqlite3.Connection,
    *,
    fill_id: str,
    order_id: str,
    timestamp: datetime,
    fill_price: float,
    filled_notional_usd: float,
    model: FillModel,
) -> None:
    connection.execute(
        """
        INSERT INTO fills (
            fill_id,
            order_id,
            fill_timestamp_utc,
            fill_price,
            filled_notional_usd,
            bid_price,
            ask_price,
            spread_cost_usd,
            slippage_cost_usd,
            total_transaction_cost_usd,
            fill_source,
            created_at_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(fill_id)
        DO UPDATE SET
            fill_price = excluded.fill_price,
            filled_notional_usd = excluded.filled_notional_usd,
            bid_price = excluded.bid_price,
            ask_price = excluded.ask_price,
            spread_cost_usd = excluded.spread_cost_usd,
            slippage_cost_usd = excluded.slippage_cost_usd,
            total_transaction_cost_usd = (
                excluded.total_transaction_cost_usd
            ),
            fill_source = excluded.fill_source
        """,
        (
            fill_id,
            order_id,
            utc_iso(timestamp),
            fill_price,
            filled_notional_usd,
            model.bid_price,
            model.ask_price,
            model.spread_cost_usd,
            model.slippage_cost_usd,
            model.total_transaction_cost_usd,
            model.fill_source,
            utc_iso(utc_now()),
        ),
    )


def reject_or_cancel_entry(
    connection: sqlite3.Connection,
    *,
    decision: EntryDecision,
    status: str,
    reason: str,
    signal_price: float,
    notional_usd: float,
    expected_cost_bps: float,
) -> None:
    order_id = deterministic_id(
        "paper-order", decision.decision_id, "open"
    )
    side = "buy" if decision.direction == 1 else "sell"
    write_order(
        connection,
        order_id=order_id,
        run_id=decision.run_id,
        decision_id=decision.decision_id,
        relationship_id=decision.relationship_id,
        side=side,
        action="open",
        notional_usd=max(notional_usd, 0.01),
        quantity=None,
        signal_price=signal_price,
        expected_round_trip_cost_bps=max(expected_cost_bps, 0.0),
        submitted_at=decision.decision_timestamp,
        status=status,
        rejection_reason=reason,
    )

    create_alert(
        connection,
        run_id=decision.run_id,
        timestamp=decision.decision_timestamp,
        severity="warning",
        alert_type=(
            "rejected_order" if status == "rejected" else "cancelled_order"
        ),
        message=(
            f"Entry order for {decision.relationship_id} was {status}: "
            f"{reason}"
        ),
        details={
            "decision_id": decision.decision_id,
            "relationship_id": decision.relationship_id,
            "reason": reason,
        },
    )


def process_entry(
    connection: sqlite3.Connection,
    *,
    decision: EntryDecision,
    spec: dict[str, Any],
    initial_equity_usd: float,
    as_of: datetime,
) -> str:
    execution = spec["execution"]
    risk = spec["risk"]
    fill_config = execution["fill_model"]

    signal_bar = latest_bar_at_or_before(
        connection,
        symbol=decision.fx_symbol,
        timestamp=decision.decision_timestamp,
    )
    if signal_bar is None:
        # The schema requires a positive signal price. This should not occur
        # for a completed feature, so fail loudly instead of inventing one.
        raise RuntimeError(
            f"No signal-time FX bar for {decision.relationship_id}."
        )

    if connection.execute(
        """
        SELECT 1
        FROM positions
        WHERE run_id = ?
          AND relationship_id = ?
          AND status = 'open'
        LIMIT 1
        """,
        (decision.run_id, decision.relationship_id),
    ).fetchone() is not None:
        reject_or_cancel_entry(
            connection,
            decision=decision,
            status="rejected",
            reason="position_already_open",
            signal_price=signal_bar.close_price,
            notional_usd=0.01,
            expected_cost_bps=0.0,
        )
        return "rejected"

    # Don't open something the session-close rule will immediately take back
    # off. Entering inside the blackout pays the spread twice for a position
    # with no room to work.
    if not overnight_positions_allowed(spec):
        commodity_venue, fx_venue = relationship_venues(
            connection,
            relationship_id=decision.relationship_id,
        )
        blocked, _deadline = session_calendar().entry_blocked(
            commodity_venue=commodity_venue,
            fx_venue=fx_venue,
            as_of=decision.decision_timestamp,
            block_minutes=float(
                spec["sessions"][
                    "block_new_entries_before_market_close_minutes"
                ]
            ),
        )
        if blocked:
            reject_or_cancel_entry(
                connection,
                decision=decision,
                status="rejected",
                reason="session_close_blackout",
                signal_price=signal_bar.close_price,
                notional_usd=0.01,
                expected_cost_bps=0.0,
            )
            return "rejected"

    state = portfolio_state(
        connection,
        run_id=decision.run_id,
        initial_equity_usd=initial_equity_usd,
        as_of=decision.decision_timestamp,
    )

    current_daily_loss = daily_loss_pct(
        connection,
        run_id=decision.run_id,
        current_equity=state.total_equity_usd,
        as_of=decision.decision_timestamp,
        initial_equity_usd=initial_equity_usd,
    )

    if current_daily_loss <= -float(risk["daily_loss_pause_pct"]):
        reject_or_cancel_entry(
            connection,
            decision=decision,
            status="rejected",
            reason="daily_loss_pause",
            signal_price=signal_bar.close_price,
            notional_usd=0.01,
            expected_cost_bps=0.0,
        )
        return "rejected"

    if state.drawdown_pct <= -float(risk["total_drawdown_pause_pct"]):
        reject_or_cancel_entry(
            connection,
            decision=decision,
            status="rejected",
            reason="total_drawdown_pause",
            signal_price=signal_bar.close_price,
            notional_usd=0.01,
            expected_cost_bps=0.0,
        )
        return "rejected"

    if state.open_positions >= int(risk["maximum_simultaneous_positions"]):
        reject_or_cancel_entry(
            connection,
            decision=decision,
            status="rejected",
            reason="maximum_simultaneous_positions",
            signal_price=signal_bar.close_price,
            notional_usd=0.01,
            expected_cost_bps=0.0,
        )
        return "rejected"

    requested, sizing_details = requested_entry_notional(
        connection,
        decision=decision,
        state=state,
        spec=spec,
    )
    capacity, capacity_details = available_entry_capacity(
        connection,
        decision=decision,
        state=state,
        spec=spec,
    )
    notional = min(requested, capacity)

    if notional <= 0:
        reject_or_cancel_entry(
            connection,
            decision=decision,
            status="rejected",
            reason="no_risk_capacity",
            signal_price=signal_bar.close_price,
            notional_usd=max(requested, 0.01),
            expected_cost_bps=0.0,
        )
        create_alert(
            connection,
            run_id=decision.run_id,
            timestamp=decision.decision_timestamp,
            severity="warning",
            alert_type="risk_limit_breach",
            message="No portfolio risk capacity remained for entry.",
            details={
                "decision_id": decision.decision_id,
                "relationship_id": decision.relationship_id,
                **sizing_details,
                **capacity_details,
            },
        )
        return "rejected"

    fill_bar = next_fill_bar(
        connection,
        symbol=decision.fx_symbol,
        signal_timestamp=decision.decision_timestamp,
        additional_delay_minutes=int(
            execution["additional_entry_delay_minutes"]
        ),
        cancel_after_minutes=int(
            execution["stale_signal_policy"]["cancel_after_minutes"]
        ),
        as_of=as_of,
    )

    fallback_rt = float(
        fill_config["fallback_minimum_round_trip_cost_bps"]
    )
    slippage = float(
        fill_config["fallback_slippage_per_side_bps"]
    )
    fallback_expected_cost = fallback_rt + 2.0 * slippage

    if fill_bar is None:
        reject_or_cancel_entry(
            connection,
            decision=decision,
            status="cancelled",
            reason="no_fill_bar_before_signal_expiry",
            signal_price=signal_bar.close_price,
            notional_usd=notional,
            expected_cost_bps=fallback_expected_cost,
        )
        return "cancelled"

    side = "buy" if decision.direction == 1 else "sell"
    model = build_fill_model(
        connection,
        symbol=decision.fx_symbol,
        side=side,
        fill_bar=fill_bar,
        notional_usd=notional,
        use_bid_ask=bool(fill_config["use_bid_ask_when_available"]),
        fallback_round_trip_bps=fallback_rt,
        slippage_per_side_bps=slippage,
        maximum_quote_lateness_seconds=int(
            spec["data"]["market"]["maximum_bar_lateness_seconds"]
        ),
    )

    max_cost = float(
        execution[
            "reject_entry_when_expected_round_trip_cost_bps_exceeds"
        ]
    )
    if model.expected_round_trip_cost_bps > max_cost:
        reject_or_cancel_entry(
            connection,
            decision=decision,
            status="rejected",
            reason="expected_transaction_cost_too_high",
            signal_price=signal_bar.close_price,
            notional_usd=notional,
            expected_cost_bps=model.expected_round_trip_cost_bps,
        )
        return "rejected"

    quantity = notional / model.simulated_fill_price
    order_id = deterministic_id(
        "paper-order", decision.decision_id, "open"
    )
    fill_id = deterministic_id("paper-fill", order_id)
    position_id = deterministic_id(
        "paper-position",
        decision.run_id,
        decision.relationship_id,
        fill_id,
    )

    write_order(
        connection,
        order_id=order_id,
        run_id=decision.run_id,
        decision_id=decision.decision_id,
        relationship_id=decision.relationship_id,
        side=side,
        action="open",
        notional_usd=notional,
        quantity=quantity,
        signal_price=signal_bar.close_price,
        expected_round_trip_cost_bps=model.expected_round_trip_cost_bps,
        submitted_at=decision.decision_timestamp,
        status="filled",
        rejection_reason=None,
    )
    write_fill(
        connection,
        fill_id=fill_id,
        order_id=order_id,
        timestamp=fill_bar.timestamp,
        fill_price=model.simulated_fill_price,
        filled_notional_usd=notional,
        model=model,
    )

    now = utc_iso(utc_now())
    connection.execute(
        """
        INSERT INTO positions (
            position_id,
            run_id,
            relationship_id,
            direction,
            status,
            opened_at_utc,
            entry_fill_id,
            entry_price,
            position_size_usd,
            closed_at_utc,
            exit_fill_id,
            exit_price,
            gross_pnl_usd,
            transaction_cost_usd,
            net_pnl_usd,
            exit_reason,
            created_at_utc,
            updated_at_utc
        )
        VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)
        ON CONFLICT(position_id)
        DO UPDATE SET
            entry_price = excluded.entry_price,
            position_size_usd = excluded.position_size_usd,
            updated_at_utc = excluded.updated_at_utc
        """,
        (
            position_id,
            decision.run_id,
            decision.relationship_id,
            decision.direction,
            utc_iso(fill_bar.timestamp),
            fill_id,
            model.simulated_fill_price,
            notional,
            now,
            now,
        ),
    )

    return "filled"


def latest_complete_feature(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    relationship_id: str,
    as_of: datetime,
) -> tuple[int, int, datetime, float, float, float] | None:
    row = connection.execute(
        """
        SELECT
            f.feature_id,
            f.spec_id,
            f.feature_timestamp_utc,
            f.divergence_score,
            f.realized_volatility_60m,
            rw.selection_weight
        FROM feature_snapshots f
        LEFT JOIN relationship_weights rw
          ON rw.relationship_id = f.relationship_id
         AND rw.selection_year = CAST(
             strftime('%Y', f.feature_timestamp_utc)
             AS INTEGER
         )
        WHERE f.run_id = ?
          AND f.relationship_id = ?
          AND f.feature_timestamp_utc <= ?
          AND f.market_data_complete = 1
        ORDER BY f.feature_timestamp_utc DESC
        LIMIT 1
        """,
        (run_id, relationship_id, utc_iso(as_of)),
    ).fetchone()

    if row is None:
        return None
    if row[3] is None or row[4] is None or row[5] is None:
        return None

    return (
        int(row[0]),
        int(row[1]),
        parse_utc_iso(str(row[2])),
        finite_number(row[3], "divergence_score"),
        finite_positive(row[4], "realized_volatility_60m"),
        finite_number(row[5], "selection_weight"),
    )


def choose_exit_reason(
    connection: sqlite3.Connection,
    *,
    position: OpenPosition,
    as_of: datetime,
    spec: dict[str, Any],
) -> tuple[str, datetime, int | None, int, float | None, dict[str, Any]] | None:
    exits = spec["exits"]
    current_bar = latest_bar_at_or_before(
        connection,
        symbol=position.fx_symbol,
        timestamp=as_of,
    )
    if current_bar is None:
        return None

    feature = latest_complete_feature(
        connection,
        run_id=position.run_id,
        relationship_id=position.relationship_id,
        as_of=as_of,
    )

    return_fraction = (
        current_bar.close_price / position.entry_price - 1.0
    )
    directional_return = position.direction * return_fraction
    holding_minutes = (
        current_bar.timestamp - position.opened_at
    ).total_seconds() / 60.0

    details: dict[str, Any] = {
        "position_id": position.position_id,
        "relationship_id": position.relationship_id,
        "current_mark_price": current_bar.close_price,
        "entry_price": position.entry_price,
        "directional_return": directional_return,
        "holding_minutes": holding_minutes,
    }

    feature_id: int | None = None
    spec_id = 0
    signal_strength: float | None = None
    feature_timestamp = current_bar.timestamp

    # Session close is checked before every other exit and outranks all of
    # them. The spec has said `allow_overnight_positions: false` from the
    # start; until now nothing enforced it, and this engine's own heartbeat
    # advertised the gap as "deferred_to_relationship_exchange_calendar_
    # scheduler". A position past its flat deadline is not a position to be
    # reasoned about on its merits — it has to go, whatever the divergence
    # is doing.
    if not overnight_positions_allowed(spec):
        commodity_venue, fx_venue = relationship_venues(
            connection,
            relationship_id=position.relationship_id,
        )
        must_flatten, deadline = session_calendar().must_be_flat(
            commodity_venue=commodity_venue,
            fx_venue=fx_venue,
            opened_at=position.opened_at,
            as_of=current_bar.timestamp,
        )
        details.update(
            {
                "session_flat_deadline_utc": utc_iso(
                    deadline.deadline_utc
                ),
                "session_binding_leg": deadline.binding_leg,
                "session_commodity_venue": commodity_venue,
                "session_fx_venue": fx_venue,
            }
        )
        if must_flatten:
            # Trigger at the deadline itself, not at the current bar.
            # next_fill_bar looks for a bar strictly after the trigger and at
            # or before `as_of`; anchoring the trigger to the moving current
            # bar would push it forward on every cycle and the fill could
            # never land, leaving the position open past its deadline
            # indefinitely — the exact failure this rule exists to prevent.
            # The deadline is a fixed point, so `as_of` advances past it and
            # the exit fills on the following bar like any other.
            return (
                "session_close",
                deadline.deadline_utc,
                feature_id,
                spec_id,
                signal_strength,
                details,
            )

    if feature is not None:
        (
            feature_id,
            spec_id,
            feature_timestamp,
            divergence,
            volatility,
            selection_weight,
        ) = feature
        weighted_opposite_strength = min(
            float(spec["position_sizing"]["signal_strength_cap"]),
            abs(divergence) * selection_weight,
        )
        details.update(
            {
                "feature_timestamp_utc": utc_iso(feature_timestamp),
                "divergence_score": divergence,
                "realized_volatility_60m": volatility,
                "selection_weight": selection_weight,
                "weighted_opposite_signal_strength": (
                    weighted_opposite_strength
                ),
            }
        )

        if bool(exits["volatility_stop"]["enabled"]):
            stop_units = float(
                exits["volatility_stop"]["volatility_units"]
            )
            if directional_return <= -stop_units * volatility:
                return (
                    "volatility_stop",
                    feature_timestamp,
                    feature_id,
                    spec_id,
                    weighted_opposite_strength,
                    details,
                )

        reversal = exits["signal_reversal"]
        if (
            bool(reversal["enabled"])
            and sign(divergence) == -position.direction
            and weighted_opposite_strength
            >= float(reversal["minimum_opposite_signal_strength"])
        ):
            return (
                "signal_reversal",
                feature_timestamp,
                feature_id,
                spec_id,
                weighted_opposite_strength,
                details,
            )

        convergence = exits["divergence_convergence"]
        if (
            bool(convergence["enabled"])
            and abs(divergence)
            < float(
                convergence[
                    "close_when_absolute_divergence_below"
                ]
            )
        ):
            return (
                "divergence_convergence",
                feature_timestamp,
                feature_id,
                spec_id,
                abs(divergence) * selection_weight,
                details,
            )

    maximum = exits["maximum_holding_time"]
    if (
        bool(maximum["enabled"])
        and holding_minutes >= float(maximum["minutes"])
    ):
        # Time-based exits are triggered at the first available mark at or
        # after the configured holding limit.
        return (
            "maximum_holding_time",
            current_bar.timestamp,
            feature_id,
            spec_id,
            signal_strength,
            details,
        )

    return None


def write_exit_decision(
    connection: sqlite3.Connection,
    *,
    position: OpenPosition,
    reason: str,
    timestamp: datetime,
    feature_id: int | None,
    spec_id: int,
    signal_strength: float | None,
    details: dict[str, Any],
) -> str:
    if spec_id == 0:
        row = connection.execute(
            "SELECT spec_id FROM paper_runs WHERE run_id = ?",
            (position.run_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Run disappeared during exit processing.")
        spec_id = int(row[0])

    decision_id = deterministic_id(
        "paper-exit-decision",
        position.run_id,
        position.position_id,
        reason,
        utc_iso(timestamp),
    )
    now = utc_iso(utc_now())
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
        VALUES (?, ?, ?, ?, ?, ?, 'exit', 'risk', ?, 1, ?, ?, ?, ?)
        ON CONFLICT(decision_id)
        DO UPDATE SET
            feature_id = excluded.feature_id,
            signal_strength = excluded.signal_strength,
            reason_detail = excluded.reason_detail,
            decision_snapshot_json = excluded.decision_snapshot_json,
            created_at_utc = excluded.created_at_utc
        """,
        (
            decision_id,
            position.run_id,
            spec_id,
            feature_id,
            position.relationship_id,
            utc_iso(timestamp),
            signal_strength,
            f"exit_{reason}",
            f"Execution-layer exit triggered by {reason}.",
            json.dumps(details, sort_keys=True, separators=(",", ":")),
            now,
        ),
    )
    return decision_id


def process_exit(
    connection: sqlite3.Connection,
    *,
    position: OpenPosition,
    spec: dict[str, Any],
    as_of: datetime,
) -> tuple[str, str | None]:
    """Returns the outcome and, when one was chosen, the exit reason.

    The reason comes back so callers can report on *why* positions closed —
    session-close enforcement in particular is worth being able to see in the
    heartbeat rather than having to infer from the ledger.
    """
    chosen = choose_exit_reason(
        connection,
        position=position,
        as_of=as_of,
        spec=spec,
    )
    if chosen is None:
        return "held", None

    (
        reason,
        trigger_timestamp,
        feature_id,
        spec_id,
        signal_strength,
        details,
    ) = chosen
    decision_id = write_exit_decision(
        connection,
        position=position,
        reason=reason,
        timestamp=trigger_timestamp,
        feature_id=feature_id,
        spec_id=spec_id,
        signal_strength=signal_strength,
        details=details,
    )

    execution = spec["execution"]
    fill_config = execution["fill_model"]
    fill_bar = next_fill_bar(
        connection,
        symbol=position.fx_symbol,
        signal_timestamp=trigger_timestamp,
        additional_delay_minutes=0,
        cancel_after_minutes=int(
            execution["stale_signal_policy"]["cancel_after_minutes"]
        ),
        as_of=as_of,
    )

    signal_bar = latest_bar_at_or_before(
        connection,
        symbol=position.fx_symbol,
        timestamp=trigger_timestamp,
    )
    if signal_bar is None:
        return "waiting_for_fill", reason

    side = "sell" if position.direction == 1 else "buy"
    current_notional = (
        position.entry_quantity * signal_bar.close_price
    )
    order_id = deterministic_id(
        "paper-order", decision_id, "close"
    )

    fallback_rt = float(
        fill_config["fallback_minimum_round_trip_cost_bps"]
    )
    slippage = float(
        fill_config["fallback_slippage_per_side_bps"]
    )
    fallback_expected_cost = fallback_rt + 2.0 * slippage

    if fill_bar is None:
        write_order(
            connection,
            order_id=order_id,
            run_id=position.run_id,
            decision_id=decision_id,
            relationship_id=position.relationship_id,
            side=side,
            action="close",
            notional_usd=max(current_notional, 0.01),
            quantity=position.entry_quantity,
            signal_price=signal_bar.close_price,
            expected_round_trip_cost_bps=fallback_expected_cost,
            submitted_at=trigger_timestamp,
            status="submitted",
            rejection_reason="waiting_for_next_completed_bar",
        )
        return "waiting_for_fill", reason

    exit_notional = (
        position.entry_quantity * fill_bar.open_price
    )
    model = build_fill_model(
        connection,
        symbol=position.fx_symbol,
        side=side,
        fill_bar=fill_bar,
        notional_usd=exit_notional,
        use_bid_ask=bool(fill_config["use_bid_ask_when_available"]),
        fallback_round_trip_bps=fallback_rt,
        slippage_per_side_bps=slippage,
        maximum_quote_lateness_seconds=int(
            spec["data"]["market"]["maximum_bar_lateness_seconds"]
        ),
    )
    filled_notional = (
        position.entry_quantity * model.simulated_fill_price
    )
    fill_id = deterministic_id("paper-fill", order_id)

    write_order(
        connection,
        order_id=order_id,
        run_id=position.run_id,
        decision_id=decision_id,
        relationship_id=position.relationship_id,
        side=side,
        action="close",
        notional_usd=max(current_notional, 0.01),
        quantity=position.entry_quantity,
        signal_price=signal_bar.close_price,
        expected_round_trip_cost_bps=model.expected_round_trip_cost_bps,
        submitted_at=trigger_timestamp,
        status="filled",
        rejection_reason=None,
    )
    write_fill(
        connection,
        fill_id=fill_id,
        order_id=order_id,
        timestamp=fill_bar.timestamp,
        fill_price=model.simulated_fill_price,
        filled_notional_usd=filled_notional,
        model=model,
    )

    entry_cost_row = connection.execute(
        """
        SELECT total_transaction_cost_usd
        FROM fills
        WHERE fill_id = ?
        """,
        (position.entry_fill_id,),
    ).fetchone()
    entry_cost = finite_number(
        entry_cost_row[0] if entry_cost_row is not None else 0.0,
        "entry_transaction_cost",
    )
    total_cost = entry_cost + model.total_transaction_cost_usd
    gross_pnl = (
        position.direction
        * position.position_size_usd
        * (model.simulated_fill_price / position.entry_price - 1.0)
    )
    net_pnl = gross_pnl - total_cost
    now = utc_iso(utc_now())

    connection.execute(
        """
        UPDATE positions
        SET
            status = 'closed',
            closed_at_utc = ?,
            exit_fill_id = ?,
            exit_price = ?,
            gross_pnl_usd = ?,
            transaction_cost_usd = ?,
            net_pnl_usd = ?,
            exit_reason = ?,
            updated_at_utc = ?
        WHERE position_id = ?
          AND status = 'open'
        """,
        (
            utc_iso(fill_bar.timestamp),
            fill_id,
            model.simulated_fill_price,
            gross_pnl,
            total_cost,
            net_pnl,
            reason,
            now,
            position.position_id,
        ),
    )

    return "closed", reason


def write_equity_snapshot(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    timestamp: datetime,
    state: PortfolioState,
) -> None:
    connection.execute(
        """
        INSERT INTO equity_snapshots (
            run_id,
            snapshot_timestamp_utc,
            cash_usd,
            realized_pnl_usd,
            unrealized_pnl_usd,
            transaction_cost_usd,
            total_equity_usd,
            gross_exposure_usd,
            net_exposure_usd,
            drawdown_pct,
            open_positions
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id, snapshot_timestamp_utc)
        DO UPDATE SET
            cash_usd = excluded.cash_usd,
            realized_pnl_usd = excluded.realized_pnl_usd,
            unrealized_pnl_usd = excluded.unrealized_pnl_usd,
            transaction_cost_usd = excluded.transaction_cost_usd,
            total_equity_usd = excluded.total_equity_usd,
            gross_exposure_usd = excluded.gross_exposure_usd,
            net_exposure_usd = excluded.net_exposure_usd,
            drawdown_pct = excluded.drawdown_pct,
            open_positions = excluded.open_positions
        """,
        (
            run_id,
            utc_iso(timestamp),
            state.cash_usd,
            state.realized_pnl_usd,
            state.unrealized_pnl_usd,
            state.transaction_cost_usd,
            state.total_equity_usd,
            state.gross_exposure_usd,
            state.net_exposure_usd,
            state.drawdown_pct,
            state.open_positions,
        ),
    )


def update_heartbeat(
    connection: sqlite3.Connection,
    *,
    status: str,
    details: dict[str, Any],
) -> None:
    now = utc_iso(utc_now())
    connection.execute(
        """
        INSERT INTO service_heartbeats (
            service_name,
            status,
            last_heartbeat_utc,
            details_json,
            updated_at_utc
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(service_name)
        DO UPDATE SET
            status = excluded.status,
            last_heartbeat_utc = excluded.last_heartbeat_utc,
            details_json = excluded.details_json,
            updated_at_utc = excluded.updated_at_utc
        """,
        (
            EXECUTION_SERVICE_NAME,
            status,
            now,
            json.dumps(details, sort_keys=True, separators=(",", ":")),
            now,
        ),
    )


def count_rows(
    connection: sqlite3.Connection,
    table: str,
    run_id: str,
) -> int:
    if table == "fills":
        query = """
            SELECT COUNT(*)
            FROM fills f
            JOIN orders o ON o.order_id = f.order_id
            WHERE o.run_id = ?
        """
    elif table == "position_marks":
        query = "SELECT COUNT(*) FROM position_marks WHERE run_id = ?"
    elif table == "equity_snapshots":
        query = "SELECT COUNT(*) FROM equity_snapshots WHERE run_id = ?"
    else:
        query = f"SELECT COUNT(*) FROM {table} WHERE run_id = ?"

    row = connection.execute(query, (run_id,)).fetchone()
    return int(row[0]) if row is not None else 0


def run_paper_execution(
    *,
    database_path: Path,
    spec_path: Path,
    run_id: str | None,
    run_mode: str,
    as_of: datetime | None,
) -> dict[str, Any]:
    spec = load_intraday_spec(spec_path)
    database_path = database_path.resolve()

    if not database_path.exists():
        raise FileNotFoundError(
            f"Paper-trading database does not exist: {database_path}"
        )

    resolved_run_id = run_id or default_run_id(spec, run_mode)
    cancel_after = int(
        spec["execution"]["stale_signal_policy"]["cancel_after_minutes"]
    )

    with sqlite3.connect(database_path, timeout=5.0) as connection:
        configure_connection(connection)
        require_tables(connection)
        spec_id, initial_equity = validate_run_spec(
            connection,
            run_id=resolved_run_id,
            run_mode=run_mode,
            spec=spec,
        )
        execution_time = resolve_as_of(
            connection,
            run_id=resolved_run_id,
            run_mode=run_mode,
            requested=as_of,
            cancel_after_minutes=cancel_after,
        )

        before = {
            table: count_rows(connection, table, resolved_run_id)
            for table in (
                "orders",
                "fills",
                "positions",
                "position_marks",
                "equity_snapshots",
            )
        }

        # Mark first so exit and risk calculations use the newest available
        # prices at or before the execution timestamp.
        marks_written_before, marks_missing_before = mark_open_positions(
            connection,
            run_id=resolved_run_id,
            as_of=execution_time,
        )

        exit_counts = {
            "closed": 0,
            "held": 0,
            "waiting_for_fill": 0,
            "session_close_triggered": 0,
            "session_close_filled": 0,
        }
        for position in load_open_positions(
            connection, run_id=resolved_run_id
        ):
            result, exit_reason = process_exit(
                connection,
                position=position,
                spec=spec,
                as_of=execution_time,
            )
            exit_counts[result] += 1
            if exit_reason == "session_close":
                # Triggered, which is not the same as filled: like every other
                # exit, a session close fills against the following bar, so a
                # position can be past its deadline and still open for one
                # more cycle while that bar arrives.
                exit_counts["session_close_triggered"] += 1
                if result == "closed":
                    exit_counts["session_close_filled"] += 1

        entry_counts = {
            "filled": 0,
            "rejected": 0,
            "cancelled": 0,
        }
        entries = load_pending_entries(
            connection,
            run_id=resolved_run_id,
            spec_id=spec_id,
            as_of=execution_time,
        )
        for decision in entries:
            result = process_entry(
                connection,
                decision=decision,
                spec=spec,
                initial_equity_usd=initial_equity,
                as_of=execution_time,
            )
            entry_counts[result] += 1

        marks_written_after, marks_missing_after = mark_open_positions(
            connection,
            run_id=resolved_run_id,
            as_of=execution_time,
        )
        state = portfolio_state(
            connection,
            run_id=resolved_run_id,
            initial_equity_usd=initial_equity,
            as_of=execution_time,
        )
        write_equity_snapshot(
            connection,
            run_id=resolved_run_id,
            timestamp=execution_time,
            state=state,
        )

        after = {
            table: count_rows(connection, table, resolved_run_id)
            for table in (
                "orders",
                "fills",
                "positions",
                "position_marks",
                "equity_snapshots",
            )
        }

        details: dict[str, Any] = {
            "run_id": resolved_run_id,
            "spec_id": spec_id,
            "execution_timestamp_utc": utc_iso(execution_time),
            "entry_decisions_evaluated": len(entries),
            "entries_filled": entry_counts["filled"],
            "entries_rejected": entry_counts["rejected"],
            "entries_cancelled": entry_counts["cancelled"],
            "positions_closed": exit_counts["closed"],
            "positions_held": exit_counts["held"],
            "exits_waiting_for_fill": exit_counts["waiting_for_fill"],
            "marks_written": marks_written_before + marks_written_after,
            "marks_missing": marks_missing_before + marks_missing_after,
            "open_positions": state.open_positions,
            "cash_usd": state.cash_usd,
            "realized_pnl_usd": state.realized_pnl_usd,
            "unrealized_pnl_usd": state.unrealized_pnl_usd,
            "transaction_cost_usd": state.transaction_cost_usd,
            "total_equity_usd": state.total_equity_usd,
            "gross_exposure_usd": state.gross_exposure_usd,
            "net_exposure_usd": state.net_exposure_usd,
            "drawdown_pct": state.drawdown_pct,
            "new_orders": after["orders"] - before["orders"],
            "new_fills": after["fills"] - before["fills"],
            "new_positions": after["positions"] - before["positions"],
            "new_position_marks": (
                after["position_marks"] - before["position_marks"]
            ),
            "new_equity_snapshots": (
                after["equity_snapshots"]
                - before["equity_snapshots"]
            ),
            "session_close_enforcement": (
                "enforced"
                if not overnight_positions_allowed(spec)
                else "disabled_by_spec"
            ),
            "session_closes_triggered": (
                exit_counts["session_close_triggered"]
            ),
            "session_closes_filled": (
                exit_counts["session_close_filled"]
            ),
        }

        update_heartbeat(
            connection,
            status=(
                "healthy" if details["marks_missing"] == 0 else "degraded"
            ),
            details=details,
        )

        foreign_key_errors = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if foreign_key_errors:
            raise RuntimeError(
                f"Foreign-key check failed: {foreign_key_errors}"
            )

        connection.commit()

    return details


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Execute approved paper signals, manage exits, mark "
            "positions, and persist portfolio equity."
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
    )
    parser.add_argument(
        "--spec",
        type=Path,
        default=DEFAULT_SPEC_PATH,
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--run-mode",
        choices=["local_replay", "shadow", "live_paper"],
        default="local_replay",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="UTC ISO-8601 execution timestamp.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    as_of = (
        None
        if args.as_of is None
        else parse_utc_iso(args.as_of)
    )
    details = run_paper_execution(
        database_path=args.database,
        spec_path=args.spec,
        run_id=args.run_id,
        run_mode=args.run_mode,
        as_of=as_of,
    )

    print("Paper execution completed successfully.")
    for key, value in details.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()