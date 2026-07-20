from __future__ import annotations

import argparse
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg2
from dotenv import load_dotenv

from paper_trading.database.init_database import (
    DEFAULT_DATABASE_PATH,
    configure_connection,
    initialize_database,
)
from strategy.config.intraday.load_intraday_spec import (
    load_intraday_spec,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]

SOURCE_TIMEFRAME = "1"
SYNC_OVERLAP_MINUTES = 5


@dataclass(frozen=True)
class MarketRoute:
    source_symbol: str
    target_symbol: str
    transform: str
    source_name: str


def utc_now() -> datetime:
    return datetime.now(
        timezone.utc
    )


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(
            tzinfo=timezone.utc
        )

    return value.astimezone(
        timezone.utc
    ).isoformat()


def parse_utc_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(
        value.replace(
            "Z",
            "+00:00",
        )
    )

    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=timezone.utc
        )

    return parsed.astimezone(
        timezone.utc
    )


def finite_positive(
    value: Any,
    field_name: str,
) -> float:
    result = float(value)

    if (
        result <= 0
        or result != result
        or result in (
            float("inf"),
            float("-inf"),
        )
    ):
        raise ValueError(
            f"{field_name} must be a finite "
            f"positive number: {value}"
        )

    return result


def normalize_ohlc(
    *,
    open_price: Any,
    high_price: Any,
    low_price: Any,
    close_price: Any,
    transform: str,
) -> tuple[float, float, float, float]:
    source_open = finite_positive(
        open_price,
        "open",
    )

    source_high = finite_positive(
        high_price,
        "high",
    )

    source_low = finite_positive(
        low_price,
        "low",
    )

    source_close = finite_positive(
        close_price,
        "close",
    )

    if source_high < source_low:
        raise ValueError(
            "Source high is below source low."
        )

    if transform == "identity":
        normalized = (
            source_open,
            source_high,
            source_low,
            source_close,
        )

    elif transform == "inverse":
        normalized = (
            1.0 / source_open,
            1.0 / source_low,
            1.0 / source_high,
            1.0 / source_close,
        )

    else:
        raise ValueError(
            f"Unsupported price transform: "
            f"{transform}"
        )

    (
        normalized_open,
        normalized_high,
        normalized_low,
        normalized_close,
    ) = normalized

    if normalized_high < normalized_low:
        raise ValueError(
            "Normalized high is below "
            "normalized low."
        )

    return (
        normalized_open,
        normalized_high,
        normalized_low,
        normalized_close,
    )


def load_routes(
    connection: sqlite3.Connection,
) -> list[MarketRoute]:
    rows = connection.execute(
        """
        SELECT DISTINCT
            live_commodity_symbol
                AS source_symbol,
            live_commodity_symbol
                AS target_symbol,
            'identity'
                AS transform,
            market_source_name
                AS source_name
        FROM live_instrument_registry
        WHERE active = 1

        UNION

        SELECT DISTINCT
            l.live_fx_symbol
                AS source_symbol,
            r.fx_symbol
                AS target_symbol,
            l.fx_price_transform
                AS transform,
            l.market_source_name
                AS source_name
        FROM live_instrument_registry l
        JOIN relationships r
          ON r.relationship_id =
             l.relationship_id
        WHERE l.active = 1

        ORDER BY
            source_symbol,
            target_symbol
        """
    ).fetchall()

    routes = [
        MarketRoute(
            source_symbol=row[0],
            target_symbol=row[1],
            transform=row[2],
            source_name=row[3],
        )
        for row in rows
    ]

    if not routes:
        raise RuntimeError(
            "No active market-data routes "
            "were found."
        )

    return routes


def get_cursor_timestamp(
    connection: sqlite3.Connection,
    route: MarketRoute,
) -> datetime | None:
    row = connection.execute(
        """
        SELECT last_bar_timestamp_utc
        FROM market_ingestion_state
        WHERE source_name = ?
          AND source_symbol = ?
          AND target_symbol = ?
          AND timeframe = ?
        """,
        (
            route.source_name,
            route.source_symbol,
            route.target_symbol,
            SOURCE_TIMEFRAME,
        ),
    ).fetchone()

    if row is None:
        return None

    return parse_utc_iso(
        row[0]
    )


def fetch_source_rows(
    source_connection,
    *,
    source_symbol: str,
    start_time: datetime,
    cutoff_time: datetime,
) -> list[tuple[Any, ...]]:
    with source_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                symbol,
                datetime_utc,
                open,
                high,
                low,
                close,
                volume,
                provider,
                received_at_utc
            FROM market_data
            WHERE symbol = %s
              AND timeframe = %s
              AND datetime_utc > %s
              AND datetime_utc <= %s
            ORDER BY datetime_utc
            """,
            (
                source_symbol,
                SOURCE_TIMEFRAME,
                start_time,
                cutoff_time,
            ),
        )

        return cursor.fetchall()


def write_normalized_row(
    destination: sqlite3.Connection,
    *,
    route: MarketRoute,
    row: tuple[Any, ...],
    adapter_received_at: str,
) -> None:
    (
        source_symbol,
        bar_timestamp,
        source_open,
        source_high,
        source_low,
        source_close,
        source_volume,
        provider,
        source_received_at,
    ) = row

    (
        normalized_open,
        normalized_high,
        normalized_low,
        normalized_close,
    ) = normalize_ohlc(
        open_price=source_open,
        high_price=source_high,
        low_price=source_low,
        close_price=source_close,
        transform=route.transform,
    )

    volume = (
        None
        if source_volume is None
        else float(source_volume)
    )

    raw_payload = {
        "source_symbol": source_symbol,
        "target_symbol": route.target_symbol,
        "price_transform": route.transform,
        "source_provider": provider,
        "source_received_at_utc": (
            utc_iso(source_received_at)
            if source_received_at is not None
            else None
        ),
        "source_ohlc": {
            "open": float(source_open),
            "high": float(source_high),
            "low": float(source_low),
            "close": float(source_close),
        },
    }

    normalized_source_name = (
        f"{route.source_name}_normalized"
    )

    destination.execute(
        """
        INSERT INTO market_bars_1m (
            symbol,
            bar_timestamp_utc,
            open_price,
            high_price,
            low_price,
            close_price,
            volume,
            source_name,
            received_at_utc,
            is_complete,
            raw_payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)

        ON CONFLICT (
            symbol,
            bar_timestamp_utc,
            source_name
        )
        DO UPDATE SET
            open_price =
                excluded.open_price,
            high_price =
                excluded.high_price,
            low_price =
                excluded.low_price,
            close_price =
                excluded.close_price,
            volume =
                excluded.volume,
            received_at_utc =
                excluded.received_at_utc,
            is_complete = 1,
            raw_payload_json =
                excluded.raw_payload_json
        """,
        (
            route.target_symbol,
            utc_iso(bar_timestamp),
            normalized_open,
            normalized_high,
            normalized_low,
            normalized_close,
            volume,
            normalized_source_name,
            adapter_received_at,
            json.dumps(
                raw_payload,
                sort_keys=True,
            ),
        ),
    )


def update_ingestion_state(
    connection: sqlite3.Connection,
    *,
    route: MarketRoute,
    last_timestamp: datetime,
    rows_written: int,
    sync_timestamp: str,
) -> None:
    connection.execute(
        """
        INSERT INTO market_ingestion_state (
            source_name,
            source_symbol,
            target_symbol,
            timeframe,
            last_bar_timestamp_utc,
            rows_written,
            last_sync_at_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)

        ON CONFLICT (
            source_name,
            source_symbol,
            target_symbol,
            timeframe
        )
        DO UPDATE SET
            last_bar_timestamp_utc =
                excluded.last_bar_timestamp_utc,
            rows_written =
                market_ingestion_state.rows_written
                + excluded.rows_written,
            last_sync_at_utc =
                excluded.last_sync_at_utc
        """,
        (
            route.source_name,
            route.source_symbol,
            route.target_symbol,
            SOURCE_TIMEFRAME,
            utc_iso(last_timestamp),
            rows_written,
            sync_timestamp,
        ),
    )


def update_heartbeat(
    connection: sqlite3.Connection,
    *,
    status: str,
    details: dict[str, Any],
    timestamp: str,
) -> None:
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
            status =
                excluded.status,
            last_heartbeat_utc =
                excluded.last_heartbeat_utc,
            details_json =
                excluded.details_json,
            updated_at_utc =
                excluded.updated_at_utc
        """,
        (
            "market_data_adapter",
            status,
            timestamp,
            json.dumps(
                details,
                sort_keys=True,
            ),
            timestamp,
        ),
    )


def sync_market_bars(
    *,
    lookback_hours: int,
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> dict[str, Any]:
    if lookback_hours < 1:
        raise ValueError(
            "lookback_hours must be positive."
        )

    initialize_database(
        database_path=database_path
    )

    spec = load_intraday_spec()

    lateness_seconds = int(
        spec["data"]["market"][
            "maximum_bar_lateness_seconds"
        ]
    )

    now = utc_now()

    cutoff_time = (
        now
        - timedelta(
            seconds=lateness_seconds
        )
    )

    sync_timestamp = utc_iso(
        now
    )

    load_dotenv(
        dotenv_path=(
            PROJECT_ROOT
            / ".env"
        )
    )

    database_url = os.getenv(
        "DATABASE_URL"
    )

    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is not set."
        )

    source_connection = (
        psycopg2.connect(
            database_url
        )
    )

    source_connection.autocommit = True

    route_summaries: list[
        dict[str, Any]
    ] = []

    total_rows_read = 0
    total_rows_written = 0

    try:
        with sqlite3.connect(
            database_path
        ) as destination:
            configure_connection(
                destination
            )

            routes = load_routes(
                destination
            )

            for route in routes:
                cursor_timestamp = (
                    get_cursor_timestamp(
                        destination,
                        route,
                    )
                )

                if cursor_timestamp is None:
                    start_time = (
                        cutoff_time
                        - timedelta(
                            hours=lookback_hours
                        )
                    )
                else:
                    start_time = (
                        cursor_timestamp
                        - timedelta(
                            minutes=(
                                SYNC_OVERLAP_MINUTES
                            )
                        )
                    )

                rows = fetch_source_rows(
                    source_connection,
                    source_symbol=(
                        route.source_symbol
                    ),
                    start_time=start_time,
                    cutoff_time=cutoff_time,
                )

                written = 0
                last_timestamp = None

                for row in rows:
                    write_normalized_row(
                        destination,
                        route=route,
                        row=row,
                        adapter_received_at=(
                            sync_timestamp
                        ),
                    )

                    written += 1
                    last_timestamp = row[1]

                if last_timestamp is not None:
                    update_ingestion_state(
                        destination,
                        route=route,
                        last_timestamp=(
                            last_timestamp
                        ),
                        rows_written=written,
                        sync_timestamp=(
                            sync_timestamp
                        ),
                    )

                total_rows_read += len(rows)
                total_rows_written += written

                route_summaries.append(
                    {
                        "source_symbol":
                            route.source_symbol,
                        "target_symbol":
                            route.target_symbol,
                        "transform":
                            route.transform,
                        "rows":
                            written,
                    }
                )

            details = {
                "routes": len(routes),
                "rows_read": total_rows_read,
                "rows_written":
                    total_rows_written,
                "cutoff_time_utc":
                    utc_iso(cutoff_time),
            }

            update_heartbeat(
                destination,
                status="healthy",
                details=details,
                timestamp=sync_timestamp,
            )

            destination.commit()

            foreign_key_errors = (
                destination.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
            )

            if foreign_key_errors:
                raise RuntimeError(
                    "Foreign-key validation failed: "
                    f"{foreign_key_errors[:10]}"
                )

    finally:
        source_connection.close()

    return {
        "routes": route_summaries,
        "rows_read": total_rows_read,
        "rows_written": total_rows_written,
        "cutoff_time_utc":
            utc_iso(cutoff_time),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Incrementally normalize completed "
            "one-minute market bars from the "
            "collector PostgreSQL database into "
            "the paper-trading SQLite database."
        )
    )

    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=168,
        help=(
            "Initial history window when no "
            "ingestion cursor exists. Default: "
            "168 hours."
        ),
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    result = sync_market_bars(
        lookback_hours=(
            args.lookback_hours
        )
    )

    print(
        "One-minute market-data sync "
        "completed successfully."
    )

    print(
        f"Cutoff: "
        f"{result['cutoff_time_utc']}"
    )

    print(
        f"Routes: "
        f"{len(result['routes'])}"
    )

    print(
        f"Rows read: "
        f"{result['rows_read']:,}"
    )

    print(
        f"Rows written: "
        f"{result['rows_written']:,}"
    )

    print(
        "\nRoute summary:"
    )

    for route in result["routes"]:
        print(
            f"  {route['source_symbol']} "
            f"-> {route['target_symbol']} "
            f"({route['transform']}): "
            f"{route['rows']:,}"
        )


if __name__ == "__main__":
    main()
