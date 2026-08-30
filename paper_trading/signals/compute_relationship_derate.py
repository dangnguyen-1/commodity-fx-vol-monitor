"""
Fast-reacting relationship derating — a supplement to relationship_weights
(which only updates once a year from a 2-year rolling daily backtest).

A relationship that starts failing under live paper trading would
otherwise keep trading at full annual weight for up to 11 months before
the next annual cycle catches it. This looks at each relationship's own
trailing closed positions (default 60-day window) and, once it has
enough trades to say anything statistically meaningful, derates its
effective weight when its live profit factor is below breakeven.

Deliberately asymmetric: this can only ever reduce a relationship's
weight, never raise it above 1.0. Deciding a relationship deserves MORE
than its annual weight is a research judgment for the annual process,
not something a fast automatic rule should grant on its own. A floor
(never below MIN_DERATE_MULTIPLIER) also means this never fully zeroes
a relationship out by itself — full deactivation stays a human/annual
decision, this just throttles exposure while a relationship is
underperforming live.

Writes to relationship_live_derate; build_signal_decisions.py multiplies
this against the annual selection_weight when sizing a signal. With
zero live trade history (as of this writing, nothing has traded yet),
every relationship stays at derate_multiplier=1.0 — this is pure
infrastructure until real trades accumulate.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = (
    PROJECT_ROOT
    / "paper_trading"
    / "data"
    / "paper_trading.db"
)
DERATE_SERVICE_NAME = "relationship_derate_engine"

# Matches the daily research's own "minimum trailing trades: 20"
# convention for when a rolling stat is trustworthy enough to act on.
MIN_TRAILING_TRADES = 20
DEFAULT_WINDOW_DAYS = 60
MIN_DERATE_MULTIPLIER = 0.25


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def parse_utc_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def configure_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")


@dataclass(frozen=True)
class RelationshipTrailingStats:
    relationship_id: str
    trailing_trades: int
    trailing_net_pnl_usd: float
    trailing_profit_factor: float | None


def load_trailing_stats(
    connection: sqlite3.Connection,
    *,
    window_start: datetime,
) -> list[RelationshipTrailingStats]:
    rows = connection.execute(
        """
        SELECT
            relationship_id,
            net_pnl_usd
        FROM positions
        WHERE status = 'closed'
          AND closed_at_utc >= ?
          AND net_pnl_usd IS NOT NULL
        """,
        (utc_iso(window_start),),
    ).fetchall()

    by_relationship: dict[str, list[float]] = {}
    for relationship_id, net_pnl_usd in rows:
        by_relationship.setdefault(str(relationship_id), []).append(
            float(net_pnl_usd)
        )

    stats = []
    for relationship_id, pnls in by_relationship.items():
        gains = sum(p for p in pnls if p > 0)
        losses = -sum(p for p in pnls if p <= 0)
        profit_factor = (gains / losses) if losses > 0 else (
            float("inf") if gains > 0 else None
        )
        stats.append(
            RelationshipTrailingStats(
                relationship_id=relationship_id,
                trailing_trades=len(pnls),
                trailing_net_pnl_usd=sum(pnls),
                trailing_profit_factor=profit_factor,
            )
        )
    return stats


def derate_multiplier_for(stats: RelationshipTrailingStats) -> float:
    """1.0 (no derate) until there's enough trailing trade history to
    trust the stat; below breakeven, scale down toward (but never
    below) MIN_DERATE_MULTIPLIER, proportional to how bad the trailing
    profit factor is."""
    if stats.trailing_trades < MIN_TRAILING_TRADES:
        return 1.0

    profit_factor = stats.trailing_profit_factor
    if profit_factor is None or profit_factor >= 1.0:
        return 1.0

    return max(MIN_DERATE_MULTIPLIER, profit_factor)


def upsert_derate(
    connection: sqlite3.Connection,
    *,
    relationship_id: str,
    window_days: int,
    stats: RelationshipTrailingStats,
    multiplier: float,
    as_of: datetime,
) -> None:
    now = utc_iso(as_of)
    profit_factor = stats.trailing_profit_factor
    if profit_factor is not None and profit_factor == float("inf"):
        profit_factor = None  # SQLite REAL has no infinity representation

    connection.execute(
        """
        INSERT INTO relationship_live_derate (
            relationship_id,
            as_of_utc,
            window_days,
            trailing_trades,
            trailing_net_pnl_usd,
            trailing_profit_factor,
            derate_multiplier,
            updated_at_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(relationship_id)
        DO UPDATE SET
            as_of_utc = excluded.as_of_utc,
            window_days = excluded.window_days,
            trailing_trades = excluded.trailing_trades,
            trailing_net_pnl_usd = excluded.trailing_net_pnl_usd,
            trailing_profit_factor = excluded.trailing_profit_factor,
            derate_multiplier = excluded.derate_multiplier,
            updated_at_utc = excluded.updated_at_utc
        """,
        (
            relationship_id,
            now,
            window_days,
            stats.trailing_trades,
            stats.trailing_net_pnl_usd,
            profit_factor,
            multiplier,
            now,
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
            DERATE_SERVICE_NAME,
            status,
            now,
            json.dumps(details, sort_keys=True, separators=(",", ":")),
            now,
        ),
    )


def compute_relationship_derates(
    *,
    database_path: Path = DEFAULT_DATABASE_PATH,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict[str, Any]:
    as_of = utc_now()
    window_start = as_of - timedelta(days=window_days)

    connection = sqlite3.connect(database_path)
    configure_connection(connection)

    try:
        with connection:
            relationship_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT relationship_id FROM relationships WHERE active = 1"
                ).fetchall()
            ]
            stats_by_relationship = {
                s.relationship_id: s
                for s in load_trailing_stats(connection, window_start=window_start)
            }

            derated_count = 0
            for relationship_id in relationship_ids:
                stats = stats_by_relationship.get(
                    relationship_id,
                    RelationshipTrailingStats(relationship_id, 0, 0.0, None),
                )
                multiplier = derate_multiplier_for(stats)
                if multiplier < 1.0:
                    derated_count += 1
                upsert_derate(
                    connection,
                    relationship_id=relationship_id,
                    window_days=window_days,
                    stats=stats,
                    multiplier=multiplier,
                    as_of=as_of,
                )

            details = {
                "relationships_evaluated": len(relationship_ids),
                "relationships_derated": derated_count,
                "window_days": window_days,
                "min_trailing_trades": MIN_TRAILING_TRADES,
            }
            update_heartbeat(connection, status="healthy", details=details)
    finally:
        connection.close()

    return details


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Derate relationships whose trailing live paper-trading performance is below breakeven."
    )
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--database-path", type=Path, default=DEFAULT_DATABASE_PATH)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    details = compute_relationship_derates(
        database_path=args.database_path,
        window_days=args.window_days,
    )
    print("Relationship derate computation complete.")
    for key, value in details.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
