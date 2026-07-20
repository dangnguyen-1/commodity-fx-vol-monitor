from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Annotated, Literal

import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from paper_trading.api.database import (
    DatabaseUnavailable,
    get_database_path,
    parse_json,
    read_connection,
    resolve_run_id,
    row_to_dict,
    rows_to_dicts,
)


API_VERSION = "0.1.0"

app = FastAPI(
    title="Commodity-FX Paper Trading API",
    version=API_VERSION,
    description=(
        "Read-only API for paper-trading health, strategy, "
        "features, signals, orders, fills, positions, equity, "
        "and alerts."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost",
        "http://127.0.0.1",
        "http://localhost:8501",
        "http://127.0.0.1:8501",
    ],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.exception_handler(DatabaseUnavailable)
def database_unavailable_handler(
    _request,
    exc: DatabaseUnavailable,
) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "detail": str(exc),
            "status": "unavailable",
        },
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_run_id(
    connection: sqlite3.Connection,
    requested_run_id: str | None,
) -> str:
    run_id = resolve_run_id(connection, requested_run_id)
    if run_id is None:
        if requested_run_id:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown paper run: {requested_run_id}",
            )
        raise HTTPException(
            status_code=404,
            detail="No paper-trading run exists.",
        )
    return run_id


def latest_timestamp(
    connection: sqlite3.Connection,
    *,
    table: str,
    timestamp_column: str,
    run_id: str,
) -> str | None:
    allowed = {
        ("feature_snapshots", "feature_timestamp_utc"),
        ("signal_decisions", "decision_timestamp_utc"),
    }
    if (table, timestamp_column) not in allowed:
        raise ValueError("Unsupported latest-timestamp query.")

    row = connection.execute(
        f"""
        SELECT MAX({timestamp_column})
        FROM {table}
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    return str(row[0]) if row and row[0] is not None else None


@app.get("/", tags=["system"])
def root() -> dict:
    return {
        "service": "commodity-fx-paper-trading-api",
        "version": API_VERSION,
        "read_only": True,
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health", tags=["system"])
def health() -> dict:
    with read_connection() as connection:
        connection.execute("SELECT 1").fetchone()

        metadata = rows_to_dicts(
            connection.execute(
                """
                SELECT key, value, updated_at_utc
                FROM schema_metadata
                ORDER BY key
                """
            ).fetchall()
        )

        services = rows_to_dicts(
            connection.execute(
                """
                SELECT
                    service_name,
                    status,
                    last_heartbeat_utc,
                    details_json,
                    updated_at_utc
                FROM service_heartbeats
                ORDER BY service_name
                """
            ).fetchall(),
            json_fields=("details_json",),
        )

        unresolved = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN severity = 'critical' THEN 1 ELSE 0 END)
                    AS critical
            FROM system_alerts
            WHERE resolved = 0
            """
        ).fetchone()

        unhealthy_services = [
            item
            for item in services
            if item["status"] in {"degraded", "offline", "failed"}
        ]
        critical_alerts = int(unresolved["critical"] or 0)
        overall = (
            "degraded"
            if unhealthy_services or critical_alerts
            else "healthy"
        )

        return {
            "status": overall,
            "api_version": API_VERSION,
            "timestamp_utc": utc_now_iso(),
            "database": {
                "reachable": True,
                "filename": get_database_path().name,
                "schema_metadata": metadata,
            },
            "services": services,
            "unresolved_alerts": {
                "total": int(unresolved["total"] or 0),
                "critical": critical_alerts,
            },
        }


@app.get("/services", tags=["system"])
def services() -> dict:
    with read_connection() as connection:
        items = rows_to_dicts(
            connection.execute(
                """
                SELECT
                    service_name,
                    status,
                    last_heartbeat_utc,
                    details_json,
                    updated_at_utc
                FROM service_heartbeats
                ORDER BY service_name
                """
            ).fetchall(),
            json_fields=("details_json",),
        )
        return {"count": len(items), "items": items}


@app.get("/strategy", tags=["strategy"])
def strategy(
    run_id: str | None = None,
) -> dict:
    with read_connection() as connection:
        resolved_run_id = resolve_run_id(connection, run_id)

        if run_id and resolved_run_id is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown paper run: {run_id}",
            )

        if resolved_run_id:
            row = connection.execute(
                """
                SELECT
                    s.*,
                    r.run_id,
                    r.run_mode,
                    r.status AS run_status
                FROM paper_runs r
                JOIN strategy_specs s
                  ON s.spec_id = r.spec_id
                WHERE r.run_id = ?
                """,
                (resolved_run_id,),
            ).fetchone()
        else:
            row = connection.execute(
                """
                SELECT
                    s.*,
                    NULL AS run_id,
                    NULL AS run_mode,
                    NULL AS run_status
                FROM strategy_specs s
                ORDER BY s.loaded_at_utc DESC
                LIMIT 1
                """
            ).fetchone()

        if row is None:
            raise HTTPException(
                status_code=404,
                detail="No strategy specification exists.",
            )

        item = row_to_dict(row) or {}
        raw_yaml = item.pop("spec_yaml")
        try:
            parsed_spec = yaml.safe_load(raw_yaml)
        except yaml.YAMLError:
            parsed_spec = None

        return {
            "metadata": item,
            "specification": parsed_spec,
        }


@app.get("/runs/current", tags=["runs"])
def current_run(
    run_id: str | None = None,
) -> dict:
    with read_connection() as connection:
        resolved = require_run_id(connection, run_id)
        run = row_to_dict(
            connection.execute(
                """
                SELECT
                    r.*,
                    s.strategy_name,
                    s.specification_version,
                    s.status AS specification_status
                FROM paper_runs r
                JOIN strategy_specs s
                  ON s.spec_id = r.spec_id
                WHERE r.run_id = ?
                """,
                (resolved,),
            ).fetchone()
        )

        equity = row_to_dict(
            connection.execute(
                """
                SELECT *
                FROM equity_snapshots
                WHERE run_id = ?
                ORDER BY snapshot_timestamp_utc DESC
                LIMIT 1
                """,
                (resolved,),
            ).fetchone()
        )

        counts = row_to_dict(
            connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM feature_snapshots
                     WHERE run_id = ?) AS features,
                    (SELECT COUNT(*) FROM signal_decisions
                     WHERE run_id = ?) AS decisions,
                    (SELECT COUNT(*) FROM orders
                     WHERE run_id = ?) AS orders,
                    (SELECT COUNT(*)
                     FROM fills f
                     JOIN orders o ON o.order_id = f.order_id
                     WHERE o.run_id = ?) AS fills,
                    (SELECT COUNT(*) FROM positions
                     WHERE run_id = ? AND status = 'open')
                        AS open_positions,
                    (SELECT COUNT(*) FROM positions
                     WHERE run_id = ? AND status = 'closed')
                        AS closed_positions
                """,
                (resolved,) * 6,
            ).fetchone()
        )

        return {
            "run": run,
            "latest_equity": equity,
            "counts": counts,
        }


@app.get("/relationships", tags=["strategy"])
def relationships(
    active_only: bool = True,
) -> dict:
    with read_connection() as connection:
        rows = connection.execute(
            """
            WITH latest_weight_year AS (
                SELECT
                    relationship_id,
                    MAX(selection_year) AS selection_year
                FROM relationship_weights
                GROUP BY relationship_id
            ),
            latest_weights AS (
                SELECT rw.*
                FROM relationship_weights rw
                JOIN latest_weight_year ly
                  ON ly.relationship_id = rw.relationship_id
                 AND ly.selection_year = rw.selection_year
            )
            SELECT
                r.*,
                lw.selection_year,
                lw.selected,
                lw.selection_weight,
                lw.trailing_trades,
                lw.trailing_net_return_pct,
                lw.trailing_profit_factor,
                lir.live_commodity_symbol,
                lir.live_fx_symbol,
                lir.commodity_venue,
                lir.fx_venue,
                lir.fx_price_transform,
                lir.market_source_name,
                lir.active AS live_registry_active
            FROM relationships r
            LEFT JOIN latest_weights lw
              ON lw.relationship_id = r.relationship_id
            LEFT JOIN live_instrument_registry lir
              ON lir.relationship_id = r.relationship_id
            WHERE (? = 0 OR (r.active = 1 AND lir.active = 1))
            ORDER BY r.commodity, r.currency, r.relationship_id
            """,
            (1 if active_only else 0,),
        ).fetchall()
        items = rows_to_dicts(rows)
        return {"count": len(items), "items": items}


@app.get("/features/latest", tags=["signals"])
def latest_features(
    run_id: str | None = None,
    complete_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict:
    with read_connection() as connection:
        resolved = require_run_id(connection, run_id)
        timestamp = latest_timestamp(
            connection,
            table="feature_snapshots",
            timestamp_column="feature_timestamp_utc",
            run_id=resolved,
        )
        if timestamp is None:
            return {
                "run_id": resolved,
                "feature_timestamp_utc": None,
                "count": 0,
                "items": [],
            }

        rows = connection.execute(
            """
            SELECT
                f.*,
                r.commodity,
                r.currency,
                r.fx_symbol,
                rw.selected,
                rw.selection_weight
            FROM feature_snapshots f
            JOIN relationships r
              ON r.relationship_id = f.relationship_id
            LEFT JOIN relationship_weights rw
              ON rw.relationship_id = f.relationship_id
             AND rw.selection_year = CAST(
                    strftime('%Y', f.feature_timestamp_utc)
                    AS INTEGER
                 )
            WHERE f.run_id = ?
              AND f.feature_timestamp_utc = ?
              AND (? = 0 OR f.market_data_complete = 1)
            ORDER BY
                f.market_data_complete DESC,
                ABS(COALESCE(f.divergence_score, 0)) DESC,
                f.relationship_id
            LIMIT ?
            """,
            (
                resolved,
                timestamp,
                1 if complete_only else 0,
                limit,
            ),
        ).fetchall()
        items = rows_to_dicts(rows)
        return {
            "run_id": resolved,
            "feature_timestamp_utc": timestamp,
            "complete_only": complete_only,
            "count": len(items),
            "items": items,
        }


@app.get("/signals/latest", tags=["signals"])
def latest_signals(
    run_id: str | None = None,
    approved_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict:
    with read_connection() as connection:
        resolved = require_run_id(connection, run_id)
        timestamp = latest_timestamp(
            connection,
            table="signal_decisions",
            timestamp_column="decision_timestamp_utc",
            run_id=resolved,
        )
        if timestamp is None:
            return {
                "run_id": resolved,
                "decision_timestamp_utc": None,
                "count": 0,
                "items": [],
            }

        rows = connection.execute(
            """
            SELECT
                d.*,
                r.commodity,
                r.currency,
                r.fx_symbol,
                f.divergence_score,
                f.market_data_complete,
                f.relevant_news_count
            FROM signal_decisions d
            JOIN relationships r
              ON r.relationship_id = d.relationship_id
            LEFT JOIN feature_snapshots f
              ON f.feature_id = d.feature_id
            WHERE d.run_id = ?
              AND d.decision_timestamp_utc = ?
              AND (? = 0 OR d.approved = 1)
            ORDER BY
                d.approved DESC,
                ABS(COALESCE(d.signal_strength, 0)) DESC,
                d.relationship_id
            LIMIT ?
            """,
            (
                resolved,
                timestamp,
                1 if approved_only else 0,
                limit,
            ),
        ).fetchall()
        items = rows_to_dicts(
            rows,
            json_fields=("decision_snapshot_json",),
        )
        return {
            "run_id": resolved,
            "decision_timestamp_utc": timestamp,
            "approved_only": approved_only,
            "count": len(items),
            "items": items,
        }


@app.get("/positions", tags=["portfolio"])
def positions(
    run_id: str | None = None,
    status: Literal["open", "closed", "all"] = "open",
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> dict:
    with read_connection() as connection:
        resolved = require_run_id(connection, run_id)
        rows = connection.execute(
            """
            SELECT
                p.*,
                r.commodity,
                r.currency,
                r.fx_symbol,
                pm.mark_timestamp_utc AS latest_mark_timestamp_utc,
                pm.mark_price AS latest_mark_price,
                pm.unrealized_pnl_usd AS latest_unrealized_pnl_usd
            FROM positions p
            JOIN relationships r
              ON r.relationship_id = p.relationship_id
            LEFT JOIN position_marks pm
              ON pm.mark_id = (
                    SELECT pm2.mark_id
                    FROM position_marks pm2
                    WHERE pm2.position_id = p.position_id
                    ORDER BY pm2.mark_timestamp_utc DESC
                    LIMIT 1
                 )
            WHERE p.run_id = ?
              AND (? = 'all' OR p.status = ?)
            ORDER BY
                CASE p.status WHEN 'open' THEN 0 ELSE 1 END,
                COALESCE(p.closed_at_utc, p.opened_at_utc) DESC
            LIMIT ?
            """,
            (resolved, status, status, limit),
        ).fetchall()
        items = rows_to_dicts(rows)
        return {
            "run_id": resolved,
            "status_filter": status,
            "count": len(items),
            "items": items,
        }


@app.get("/orders", tags=["portfolio"])
def orders(
    run_id: str | None = None,
    status: Literal[
        "all",
        "created",
        "submitted",
        "filled",
        "cancelled",
        "rejected",
    ] = "all",
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> dict:
    with read_connection() as connection:
        resolved = require_run_id(connection, run_id)
        rows = connection.execute(
            """
            SELECT
                o.*,
                r.commodity,
                r.currency,
                r.fx_symbol,
                d.decision_type,
                d.signal_mode,
                d.signal_strength
            FROM orders o
            JOIN relationships r
              ON r.relationship_id = o.relationship_id
            JOIN signal_decisions d
              ON d.decision_id = o.decision_id
            WHERE o.run_id = ?
              AND (? = 'all' OR o.status = ?)
            ORDER BY o.submitted_at_utc DESC
            LIMIT ?
            """,
            (resolved, status, status, limit),
        ).fetchall()
        items = rows_to_dicts(rows)
        return {
            "run_id": resolved,
            "status_filter": status,
            "count": len(items),
            "items": items,
        }


@app.get("/fills", tags=["portfolio"])
def fills(
    run_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> dict:
    with read_connection() as connection:
        resolved = require_run_id(connection, run_id)
        rows = connection.execute(
            """
            SELECT
                f.*,
                o.run_id,
                o.relationship_id,
                o.side,
                o.order_action,
                r.commodity,
                r.currency,
                r.fx_symbol
            FROM fills f
            JOIN orders o
              ON o.order_id = f.order_id
            JOIN relationships r
              ON r.relationship_id = o.relationship_id
            WHERE o.run_id = ?
            ORDER BY f.fill_timestamp_utc DESC
            LIMIT ?
            """,
            (resolved, limit),
        ).fetchall()
        items = rows_to_dicts(rows)
        return {
            "run_id": resolved,
            "count": len(items),
            "items": items,
        }


@app.get("/equity", tags=["portfolio"])
def equity(
    run_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> dict:
    with read_connection() as connection:
        resolved = require_run_id(connection, run_id)
        rows = connection.execute(
            """
            SELECT *
            FROM equity_snapshots
            WHERE run_id = ?
            ORDER BY snapshot_timestamp_utc DESC
            LIMIT ?
            """,
            (resolved, limit),
        ).fetchall()
        items = rows_to_dicts(rows)
        items.reverse()
        return {
            "run_id": resolved,
            "count": len(items),
            "items": items,
            "latest": items[-1] if items else None,
        }


@app.get("/alerts", tags=["system"])
def alerts(
    run_id: str | None = None,
    resolved: bool | None = False,
    severity: Literal["info", "warning", "critical"] | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> dict:
    with read_connection() as connection:
        if run_id and resolve_run_id(connection, run_id) is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown paper run: {run_id}",
            )

        rows = connection.execute(
            """
            SELECT *
            FROM system_alerts
            WHERE (? IS NULL OR run_id = ?)
              AND (? IS NULL OR resolved = ?)
              AND (? IS NULL OR severity = ?)
            ORDER BY
                CASE severity
                    WHEN 'critical' THEN 0
                    WHEN 'warning' THEN 1
                    ELSE 2
                END,
                alert_timestamp_utc DESC
            LIMIT ?
            """,
            (
                run_id,
                run_id,
                None if resolved is None else 1,
                None if resolved is None else int(resolved),
                severity,
                severity,
                limit,
            ),
        ).fetchall()
        items = rows_to_dicts(rows, json_fields=("details_json",))
        return {"count": len(items), "items": items}


@app.get("/summary", tags=["dashboard"])
def summary(
    run_id: str | None = None,
) -> dict:
    with read_connection() as connection:
        resolved = require_run_id(connection, run_id)

        run = row_to_dict(
            connection.execute(
                "SELECT * FROM paper_runs WHERE run_id = ?",
                (resolved,),
            ).fetchone()
        )
        equity_item = row_to_dict(
            connection.execute(
                """
                SELECT *
                FROM equity_snapshots
                WHERE run_id = ?
                ORDER BY snapshot_timestamp_utc DESC
                LIMIT 1
                """,
                (resolved,),
            ).fetchone()
        )
        counts = row_to_dict(
            connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM positions
                     WHERE run_id = ? AND status = 'open')
                        AS open_positions,
                    (SELECT COUNT(*) FROM orders
                     WHERE run_id = ? AND status IN ('created', 'submitted'))
                        AS pending_orders,
                    (SELECT COUNT(*) FROM system_alerts
                     WHERE resolved = 0
                       AND (run_id IS NULL OR run_id = ?))
                        AS unresolved_alerts,
                    (SELECT COUNT(*) FROM signal_decisions
                     WHERE run_id = ? AND approved = 1
                       AND decision_timestamp_utc = (
                           SELECT MAX(decision_timestamp_utc)
                           FROM signal_decisions
                           WHERE run_id = ?
                       )) AS latest_approved_signals
                """,
                (resolved,) * 5,
            ).fetchone()
        )
        timestamps = row_to_dict(
            connection.execute(
                """
                SELECT
                    (SELECT MAX(feature_timestamp_utc)
                     FROM feature_snapshots WHERE run_id = ?)
                        AS latest_feature_timestamp_utc,
                    (SELECT MAX(decision_timestamp_utc)
                     FROM signal_decisions WHERE run_id = ?)
                        AS latest_decision_timestamp_utc,
                    (SELECT MAX(fill_timestamp_utc)
                     FROM fills f
                     JOIN orders o ON o.order_id = f.order_id
                     WHERE o.run_id = ?)
                        AS latest_fill_timestamp_utc
                """,
                (resolved,) * 3,
            ).fetchone()
        )
        service_items = rows_to_dicts(
            connection.execute(
                """
                SELECT
                    service_name,
                    status,
                    last_heartbeat_utc,
                    details_json
                FROM service_heartbeats
                ORDER BY service_name
                """
            ).fetchall(),
            json_fields=("details_json",),
        )

        return {
            "run": run,
            "latest_equity": equity_item,
            "counts": counts,
            "timestamps": timestamps,
            "services": service_items,
        }
