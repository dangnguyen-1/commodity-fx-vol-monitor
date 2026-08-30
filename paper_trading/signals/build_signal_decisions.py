from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
SIGNAL_SERVICE_NAME = "signal_engine"


@dataclass(frozen=True)
class FeatureCandidate:
    feature_id: int
    run_id: str
    spec_id: int
    relationship_id: str
    feature_timestamp: datetime
    commodity: str
    currency: str
    fx_symbol: str
    relationship_direction: int
    selected: int
    selection_weight: float
    live_derate_multiplier: float
    commodity_impulse: float
    news_impulse: float
    expected_fx_impulse: float
    observed_fx_impulse: float
    divergence_score: float
    relevant_news_count: int
    market_window_coverage_pct: float


@dataclass(frozen=True)
class DecisionResult:
    decision_type: str
    signal_mode: str
    signal_strength: float | None
    approved: int
    reason_code: str
    reason_detail: str
    snapshot: dict[str, Any]


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


def parse_evaluation_timestamp(
    raw_value: str,
    interval_minutes: int,
) -> datetime:
    value = parse_utc_iso(raw_value)

    if (
        value.second != 0
        or value.microsecond != 0
        or value.minute % interval_minutes != 0
    ):
        raise ValueError(
            "Decision timestamp must fall exactly "
            f"on a {interval_minutes}-minute boundary."
        )

    return value


def finite_number(value: Any, name: str) -> float:
    result = float(value)

    if not math.isfinite(result):
        raise ValueError(
            f"{name} must be a finite number."
        )

    return result


def configure_connection(
    connection: sqlite3.Connection,
) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")


def require_tables(
    connection: sqlite3.Connection,
) -> None:
    required = {
        "strategy_specs",
        "paper_runs",
        "relationships",
        "relationship_weights",
        "relationship_live_derate",
        "feature_snapshots",
        "signal_decisions",
        "positions",
        "service_heartbeats",
    }

    existing = {
        row[0]
        for row in connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            """
        ).fetchall()
    }

    missing = sorted(required - existing)

    if missing:
        raise RuntimeError(
            "Paper-trading database is missing "
            f"required tables: {missing}"
        )


def default_run_id(
    spec: dict[str, Any],
    run_mode: str,
) -> str:
    return (
        f"{spec['strategy']['name']}-"
        f"{run_mode}-"
        f"v{spec['strategy']['specification_version']}"
    )


def current_spec_sha256(
    spec: dict[str, Any],
) -> str:
    spec_path = Path(
        spec["_runtime"]["specification_path"]
    )

    raw_spec = spec_path.read_text(
        encoding="utf-8"
    )

    return hashlib.sha256(
        raw_spec.encode("utf-8")
    ).hexdigest()


def validate_run_spec(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    run_mode: str,
    spec: dict[str, Any],
) -> int:
    row = connection.execute(
        """
        SELECT
            pr.spec_id,
            pr.run_mode,
            pr.status,
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
            f"Paper run {run_id!r} does not exist. "
            "Build feature snapshots first."
        )

    spec_id = int(row[0])
    existing_mode = str(row[1])
    status = str(row[2])
    stored_sha256 = str(row[3])

    if existing_mode != run_mode:
        raise RuntimeError(
            f"Run {run_id!r} uses mode "
            f"{existing_mode!r}, not {run_mode!r}."
        )

    if status in {"stopped", "failed"}:
        raise RuntimeError(
            f"Run {run_id!r} has terminal status "
            f"{status!r}."
        )

    actual_sha256 = current_spec_sha256(spec)

    if stored_sha256 != actual_sha256:
        raise RuntimeError(
            f"Run {run_id!r} was created from a "
            "different strategy specification. "
            "Use a new run ID after changing the YAML."
        )

    return spec_id


def resolve_feature_timestamp(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    requested_timestamp: datetime | None,
    interval_minutes: int,
) -> datetime:
    if requested_timestamp is not None:
        timestamp = parse_evaluation_timestamp(
            utc_iso(requested_timestamp),
            interval_minutes,
        )
    else:
        row = connection.execute(
            """
            SELECT MAX(feature_timestamp_utc)
            FROM feature_snapshots
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()

        if row is None or row[0] is None:
            raise RuntimeError(
                f"Run {run_id!r} has no feature "
                "snapshots."
            )

        timestamp = parse_evaluation_timestamp(
            str(row[0]),
            interval_minutes,
        )

    row = connection.execute(
        """
        SELECT COUNT(*)
        FROM feature_snapshots
        WHERE run_id = ?
          AND feature_timestamp_utc = ?
        """,
        (run_id, utc_iso(timestamp)),
    ).fetchone()

    if row is None or int(row[0]) == 0:
        raise RuntimeError(
            "No feature snapshots exist for "
            f"{utc_iso(timestamp)}."
        )

    return timestamp


def load_complete_candidates(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    spec_id: int,
    timestamp: datetime,
) -> list[FeatureCandidate]:
    rows = connection.execute(
        """
        SELECT
            f.feature_id,
            f.run_id,
            f.spec_id,
            f.relationship_id,
            f.feature_timestamp_utc,
            r.commodity,
            r.currency,
            r.fx_symbol,
            r.fx_direction_multiplier,
            rw.selected,
            rw.selection_weight,
            COALESCE(rld.derate_multiplier, 1.0),
            f.commodity_impulse,
            f.news_impulse,
            f.expected_fx_impulse,
            f.observed_fx_impulse,
            f.divergence_score,
            f.relevant_news_count,
            f.market_window_coverage_pct
        FROM feature_snapshots f
        JOIN relationships r
          ON r.relationship_id = f.relationship_id
        LEFT JOIN relationship_weights rw
          ON rw.relationship_id = f.relationship_id
         AND rw.selection_year = ?
        LEFT JOIN relationship_live_derate rld
          ON rld.relationship_id = f.relationship_id
        WHERE f.run_id = ?
          AND f.spec_id = ?
          AND f.feature_timestamp_utc = ?
          AND f.market_data_complete = 1
          AND r.active = 1
        ORDER BY f.relationship_id
        """,
        (
            timestamp.year,
            run_id,
            spec_id,
            utc_iso(timestamp),
        ),
    ).fetchall()

    candidates: list[FeatureCandidate] = []

    for row in rows:
        relationship_id = str(row[3])

        if row[8] not in (-1, 1):
            raise RuntimeError(
                f"Relationship {relationship_id!r} "
                "has no valid direction multiplier."
            )

        if row[9] is None or row[10] is None:
            raise RuntimeError(
                f"Relationship {relationship_id!r} "
                f"has no weight for {timestamp.year}."
            )

        candidates.append(
            FeatureCandidate(
                feature_id=int(row[0]),
                run_id=str(row[1]),
                spec_id=int(row[2]),
                relationship_id=relationship_id,
                feature_timestamp=parse_utc_iso(
                    str(row[4])
                ),
                commodity=str(row[5]),
                currency=str(row[6]),
                fx_symbol=str(row[7]),
                relationship_direction=int(row[8]),
                selected=int(row[9]),
                selection_weight=finite_number(
                    row[10],
                    "selection_weight",
                ),
                live_derate_multiplier=finite_number(
                    row[11],
                    "live_derate_multiplier",
                ),
                commodity_impulse=finite_number(
                    row[12],
                    "commodity_impulse",
                ),
                news_impulse=finite_number(
                    row[13],
                    "news_impulse",
                ),
                expected_fx_impulse=finite_number(
                    row[14],
                    "expected_fx_impulse",
                ),
                observed_fx_impulse=finite_number(
                    row[15],
                    "observed_fx_impulse",
                ),
                divergence_score=finite_number(
                    row[16],
                    "divergence_score",
                ),
                relevant_news_count=int(row[17]),
                market_window_coverage_pct=(
                    finite_number(
                        row[18],
                        "market_window_coverage_pct",
                    )
                ),
            )
        )

    return candidates


def sign(value: float, tolerance: float = 1e-12) -> int:
    if value > tolerance:
        return 1

    if value < -tolerance:
        return -1

    return 0


def has_open_position(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    relationship_id: str,
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM positions
        WHERE run_id = ?
          AND relationship_id = ?
          AND status = 'open'
        LIMIT 1
        """,
        (run_id, relationship_id),
    ).fetchone()

    return row is not None


def evaluate_candidate(
    candidate: FeatureCandidate,
    *,
    spec: dict[str, Any],
    open_position: bool,
) -> DecisionResult:
    signals = spec["signals"]
    entry_modes = signals["entry_modes"]
    conflict_threshold = float(
        signals["conflicting_news"][
            "block_entry_when_opposing_news_score_exceeds"
        ]
    )

    sizing = spec["position_sizing"]
    strength_floor = float(
        sizing["signal_strength_floor"]
    )
    strength_cap = float(
        sizing["signal_strength_cap"]
    )

    market_sign = sign(
        candidate.commodity_impulse
    )
    news_sign = sign(candidate.news_impulse)
    divergence_sign = sign(
        candidate.divergence_score
    )

    news_available = (
        candidate.relevant_news_count > 0
        and news_sign != 0
    )
    market_news_agree = (
        market_sign != 0
        and market_sign == news_sign
    )
    opposing_news = (
        market_sign != 0
        and news_sign != 0
        and market_sign != news_sign
        and abs(candidate.news_impulse)
        > conflict_threshold
    )

    raw_strength = abs(
        candidate.divergence_score
    )
    uncapped_weighted_strength = (
        raw_strength
        * candidate.selection_weight
        * candidate.live_derate_multiplier
    )
    display_strength = min(
        strength_cap,
        uncapped_weighted_strength,
    )

    snapshot: dict[str, Any] = {
        "feature_timestamp_utc": utc_iso(
            candidate.feature_timestamp
        ),
        "commodity": candidate.commodity,
        "currency": candidate.currency,
        "fx_symbol": candidate.fx_symbol,
        "relationship_direction": (
            candidate.relationship_direction
        ),
        "selected": candidate.selected,
        "selection_weight": (
            candidate.selection_weight
        ),
        "live_derate_multiplier": (
            candidate.live_derate_multiplier
        ),
        "commodity_impulse": (
            candidate.commodity_impulse
        ),
        "news_impulse": candidate.news_impulse,
        "expected_fx_impulse": (
            candidate.expected_fx_impulse
        ),
        "observed_fx_impulse": (
            candidate.observed_fx_impulse
        ),
        "divergence_score": (
            candidate.divergence_score
        ),
        "relevant_news_count": (
            candidate.relevant_news_count
        ),
        "market_window_coverage_pct": (
            candidate.market_window_coverage_pct
        ),
        "market_news_direction_agreement": (
            market_news_agree
        ),
        "opposing_news_block": opposing_news,
        "raw_signal_strength": raw_strength,
        "uncapped_weighted_signal_strength": (
            uncapped_weighted_strength
        ),
    }

    if open_position:
        snapshot["entry_eligible"] = False

        return DecisionResult(
            decision_type="hold",
            signal_mode="none",
            signal_strength=display_strength,
            approved=0,
            reason_code="position_already_open",
            reason_detail=(
                "An open position already exists for "
                "this relationship. Exit handling is "
                "performed by the execution layer."
            ),
            snapshot=snapshot,
        )

    if opposing_news:
        snapshot["entry_eligible"] = False

        return DecisionResult(
            decision_type="reject",
            signal_mode="risk",
            signal_strength=display_strength,
            approved=0,
            reason_code="opposing_news_block",
            reason_detail=(
                "Commodity-market and net-news impulses "
                "oppose one another, and the absolute "
                "news score exceeds the configured block."
            ),
            snapshot=snapshot,
        )

    confirmed = entry_modes["confirmed"]
    confirmed_pass = bool(confirmed["enabled"])

    if bool(confirmed["require_relevant_news"]):
        confirmed_pass = (
            confirmed_pass and news_available
        )

    if bool(
        confirmed[
            "require_market_news_direction_agreement"
        ]
    ):
        confirmed_pass = (
            confirmed_pass and market_news_agree
        )

    confirmed_pass = (
        confirmed_pass
        and abs(candidate.expected_fx_impulse)
        >= float(
            confirmed[
                "minimum_absolute_expected_fx_impulse"
            ]
        )
        and raw_strength
        >= float(
            confirmed[
                "minimum_absolute_divergence_score"
            ]
        )
    )

    divergence = entry_modes["divergence"]
    divergence_pass = (
        bool(divergence["enabled"])
        and abs(candidate.commodity_impulse)
        >= float(
            divergence[
                "minimum_absolute_commodity_impulse"
            ]
        )
        and raw_strength
        >= float(
            divergence[
                "minimum_absolute_divergence_score"
            ]
        )
    )

    if confirmed_pass:
        mode = "confirmed"
    elif divergence_pass:
        mode = "divergence"
    else:
        snapshot["entry_eligible"] = False
        snapshot["confirmed_mode_passed"] = False
        snapshot["divergence_mode_passed"] = False

        return DecisionResult(
            decision_type="no_action",
            signal_mode="none",
            signal_strength=display_strength,
            approved=0,
            reason_code="entry_thresholds_not_met",
            reason_detail=(
                "The completed feature snapshot did not "
                "meet the confirmed or divergence entry "
                "thresholds."
            ),
            snapshot=snapshot,
        )

    if divergence_sign == 0:
        snapshot["entry_eligible"] = False

        return DecisionResult(
            decision_type="no_action",
            signal_mode="none",
            signal_strength=0.0,
            approved=0,
            reason_code="zero_divergence",
            reason_detail=(
                "The divergence score has no directional "
                "sign."
            ),
            snapshot=snapshot,
        )

    base_sizing_strength = min(
        strength_cap,
        max(strength_floor, raw_strength),
    )
    final_strength = (
        base_sizing_strength
        * candidate.selection_weight
        * candidate.live_derate_multiplier
    )

    snapshot["entry_eligible"] = True
    snapshot["confirmed_mode_passed"] = (
        confirmed_pass
    )
    snapshot["divergence_mode_passed"] = (
        divergence_pass
    )
    snapshot["base_sizing_signal_strength"] = (
        base_sizing_strength
    )
    snapshot["final_weighted_signal_strength"] = (
        final_strength
    )
    snapshot["trade_direction"] = (
        "long" if divergence_sign > 0 else "short"
    )

    return DecisionResult(
        decision_type=(
            "enter_long"
            if divergence_sign > 0
            else "enter_short"
        ),
        signal_mode=mode,
        signal_strength=final_strength,
        approved=1,
        reason_code=f"{mode}_entry_signal",
        reason_detail=(
            f"The {mode} entry conditions passed. "
            "Trade direction follows the sign of the "
            "FX divergence score."
        ),
        snapshot=snapshot,
    )


def deterministic_decision_id(
    *,
    run_id: str,
    relationship_id: str,
    timestamp: datetime,
) -> str:
    key = (
        "commodity-fx-signal-decision|"
        f"{run_id}|"
        f"{relationship_id}|"
        f"{utc_iso(timestamp)}"
    )

    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            key,
        )
    )


def upsert_decision(
    connection: sqlite3.Connection,
    *,
    candidate: FeatureCandidate,
    result: DecisionResult,
    created_at_utc: str,
) -> str:
    decision_id = deterministic_decision_id(
        run_id=candidate.run_id,
        relationship_id=(
            candidate.relationship_id
        ),
        timestamp=candidate.feature_timestamp,
    )

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
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(decision_id)
        DO UPDATE SET
            feature_id = excluded.feature_id,
            decision_type = excluded.decision_type,
            signal_mode = excluded.signal_mode,
            signal_strength = excluded.signal_strength,
            approved = excluded.approved,
            reason_code = excluded.reason_code,
            reason_detail = excluded.reason_detail,
            decision_snapshot_json = (
                excluded.decision_snapshot_json
            ),
            created_at_utc = excluded.created_at_utc
        """,
        (
            decision_id,
            candidate.run_id,
            candidate.spec_id,
            candidate.feature_id,
            candidate.relationship_id,
            utc_iso(candidate.feature_timestamp),
            result.decision_type,
            result.signal_mode,
            result.signal_strength,
            result.approved,
            result.reason_code,
            result.reason_detail,
            json.dumps(
                result.snapshot,
                sort_keys=True,
                separators=(",", ":"),
            ),
            created_at_utc,
        ),
    )

    return decision_id


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
            last_heartbeat_utc = (
                excluded.last_heartbeat_utc
            ),
            details_json = excluded.details_json,
            updated_at_utc = excluded.updated_at_utc
        """,
        (
            SIGNAL_SERVICE_NAME,
            status,
            now,
            json.dumps(
                details,
                sort_keys=True,
                separators=(",", ":"),
            ),
            now,
        ),
    )


def build_signal_decisions(
    *,
    database_path: Path,
    spec_path: Path,
    decision_timestamp: datetime | None,
    run_id: str | None,
    run_mode: str,
) -> dict[str, Any]:
    spec = load_intraday_spec(spec_path)
    interval_minutes = int(
        spec["signals"]["evaluation"][
            "interval_minutes"
        ]
    )

    database_path = database_path.resolve()

    if not database_path.exists():
        raise FileNotFoundError(
            "Paper-trading database does not exist: "
            f"{database_path}"
        )

    resolved_run_id = (
        run_id
        if run_id is not None
        else default_run_id(spec, run_mode)
    )

    with sqlite3.connect(
        database_path,
        timeout=5.0,
    ) as connection:
        configure_connection(connection)
        require_tables(connection)

        spec_id = validate_run_spec(
            connection,
            run_id=resolved_run_id,
            run_mode=run_mode,
            spec=spec,
        )

        timestamp = resolve_feature_timestamp(
            connection,
            run_id=resolved_run_id,
            requested_timestamp=decision_timestamp,
            interval_minutes=interval_minutes,
        )

        candidates = load_complete_candidates(
            connection,
            run_id=resolved_run_id,
            spec_id=spec_id,
            timestamp=timestamp,
        )

        created_at = utc_iso(utc_now())
        counts = {
            "enter_long": 0,
            "enter_short": 0,
            "hold": 0,
            "reject": 0,
            "no_action": 0,
            "confirmed": 0,
            "divergence": 0,
        }

        for candidate in candidates:
            result = evaluate_candidate(
                candidate,
                spec=spec,
                open_position=has_open_position(
                    connection,
                    run_id=resolved_run_id,
                    relationship_id=(
                        candidate.relationship_id
                    ),
                ),
            )

            upsert_decision(
                connection,
                candidate=candidate,
                result=result,
                created_at_utc=created_at,
            )

            counts[result.decision_type] += 1

            if result.signal_mode in {
                "confirmed",
                "divergence",
            }:
                counts[result.signal_mode] += 1

        approved_entries = (
            counts["enter_long"]
            + counts["enter_short"]
        )

        details = {
            "run_id": resolved_run_id,
            "spec_id": spec_id,
            "decision_timestamp_utc": utc_iso(
                timestamp
            ),
            "complete_features_evaluated": len(
                candidates
            ),
            "approved_entries": approved_entries,
            "enter_long": counts["enter_long"],
            "enter_short": counts["enter_short"],
            "confirmed_signals": counts["confirmed"],
            "divergence_signals": counts[
                "divergence"
            ],
            "holds": counts["hold"],
            "rejections": counts["reject"],
            "no_action": counts["no_action"],
        }

        update_heartbeat(
            connection,
            status="healthy",
            details=details,
        )

        foreign_key_errors = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

        if foreign_key_errors:
            raise RuntimeError(
                "Foreign-key check failed: "
                f"{foreign_key_errors}"
            )

        connection.commit()

    return details


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build idempotent intraday signal "
            "decisions from completed feature snapshots."
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
    parser.add_argument(
        "--timestamp",
        type=str,
        default=None,
        help=(
            "Optional UTC feature timestamp. Defaults "
            "to the latest snapshot for the run."
        ),
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
    )
    parser.add_argument(
        "--run-mode",
        choices=[
            "local_replay",
            "shadow",
            "live_paper",
        ],
        default="local_replay",
    )

    return parser


def main() -> None:
    args = build_parser().parse_args()
    spec = load_intraday_spec(args.spec)
    interval_minutes = int(
        spec["signals"]["evaluation"][
            "interval_minutes"
        ]
    )

    timestamp = (
        None
        if args.timestamp is None
        else parse_evaluation_timestamp(
            args.timestamp,
            interval_minutes,
        )
    )

    details = build_signal_decisions(
        database_path=args.database,
        spec_path=args.spec,
        decision_timestamp=timestamp,
        run_id=args.run_id,
        run_mode=args.run_mode,
    )

    print(
        "Signal decision build completed "
        "successfully."
    )

    for key, value in details.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()