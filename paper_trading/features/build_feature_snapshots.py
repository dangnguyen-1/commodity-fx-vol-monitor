from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from strategy.config.intraday.load_intraday_spec import (
    load_intraday_spec,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = (
    PROJECT_ROOT
    / "paper_trading"
    / "data"
    / "paper_trading.db"
)

FEATURE_SERVICE_NAME = "feature_engine"


@dataclass(frozen=True)
class RelationshipRoute:
    relationship_id: str
    commodity: str
    currency: str
    commodity_symbol: str
    fx_symbol: str
    relationship_direction: int
    # Measured transmission coefficient, carrying its own sign. None when
    # the relationship has never been measured -- see expected_fx_impulse.
    transmission_beta: float | None
    market_source_name: str
    selected: int
    selection_weight: float


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    close_price: float


@dataclass(frozen=True)
class MarketFeatures:
    commodity_return_15m: float | None
    commodity_return_60m: float | None
    commodity_return_240m: float | None
    fx_return_15m: float | None
    commodity_realized_volatility_60m: float | None
    fx_realized_volatility_60m: float | None
    normalized_commodity_return_15m: float | None
    normalized_commodity_return_60m: float | None
    normalized_commodity_return_240m: float | None
    normalized_fx_return_15m: float | None
    commodity_impulse: float | None
    market_window_coverage_pct: float
    market_data_complete: int


@dataclass(frozen=True)
class NewsFeatures:
    commodity_news_impulse: float
    fx_news_impulse: float
    net_news_impulse: float
    relevant_news_count: int


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


def finite_or_none(value: float | None) -> float | None:
    if value is None:
        return None

    result = float(value)

    if not math.isfinite(result):
        return None

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
        "live_instrument_registry",
        "market_bars_1m",
        "news_articles",
        "news_classifications",
        "news_classification_assets",
        "feature_snapshots",
        "service_heartbeats",
    }

    rows = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        """
    ).fetchall()

    existing = {
        row[0]
        for row in rows
    }

    missing = sorted(
        required - existing
    )

    if missing:
        raise RuntimeError(
            "Paper-trading database is missing "
            f"required tables: {missing}"
        )


def floor_to_interval(
    value: datetime,
    interval_minutes: int,
) -> datetime:
    value = value.astimezone(timezone.utc)

    floored_minute = (
        value.minute
        - value.minute % interval_minutes
    )

    return value.replace(
        minute=floored_minute,
        second=0,
        microsecond=0,
    )


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
            "Evaluation timestamp must fall exactly "
            f"on a {interval_minutes}-minute boundary."
        )

    return value


def resolve_latest_evaluation_timestamp(
    connection: sqlite3.Connection,
    interval_minutes: int,
    lag_minutes: int = 0,
) -> datetime:
    """The newest timestamp worth evaluating.

    Resolved from FX bar availability, then backed off by `lag_minutes`.
    The back-off matters: FX bars sync continuously while commodity futures
    arrive several minutes later, so evaluating at the newest FX timestamp
    leaves the commodity endpoint stale beyond maximum_bar_lateness_seconds
    and fails every commodity relationship's freshness check at once.
    """
    row = connection.execute(
        """
        WITH active_fx AS (
            SELECT DISTINCT
                r.fx_symbol AS symbol,
                l.market_source_name
                    || '_normalized'
                    AS source_name
            FROM relationships r
            JOIN live_instrument_registry l
              ON l.relationship_id =
                 r.relationship_id
            WHERE r.active = 1
              AND l.active = 1
        ),
        latest_by_fx AS (
            SELECT
                af.symbol,
                af.source_name,
                MAX(b.bar_timestamp_utc)
                    AS latest_timestamp
            FROM active_fx af
            LEFT JOIN market_bars_1m b
              ON b.symbol = af.symbol
             AND b.source_name = af.source_name
             AND b.is_complete = 1
            GROUP BY
                af.symbol,
                af.source_name
        )
        SELECT
            MIN(latest_timestamp),
            COUNT(*),
            COUNT(latest_timestamp)
        FROM latest_by_fx
        """
    ).fetchone()

    if (
        row is None
        or row[0] is None
        or int(row[1]) != int(row[2])
    ):
        raise RuntimeError(
            "At least one active FX symbol has "
            "no completed normalized bars."
        )

    return floor_to_interval(
        parse_utc_iso(row[0]) - timedelta(minutes=lag_minutes),
        interval_minutes,
    )


def relative_project_path(path: Path) -> str:
    try:
        return str(
            path.resolve().relative_to(
                PROJECT_ROOT.resolve()
            )
        )
    except ValueError:
        return str(path.resolve())


def ensure_strategy_spec(
    connection: sqlite3.Connection,
    spec: dict[str, Any],
) -> int:
    spec_path = Path(
        spec["_runtime"]["specification_path"]
    ).resolve()

    raw_spec = spec_path.read_text(
        encoding="utf-8"
    )

    digest = hashlib.sha256(
        raw_spec.encode("utf-8")
    ).hexdigest()

    now_iso = utc_iso(utc_now())

    connection.execute(
        """
        INSERT INTO strategy_specs (
            strategy_name,
            specification_version,
            status,
            spec_path,
            spec_sha256,
            spec_yaml,
            loaded_at_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(spec_sha256)
        DO NOTHING
        """,
        (
            spec["strategy"]["name"],
            spec["strategy"][
                "specification_version"
            ],
            spec["strategy"]["status"],
            relative_project_path(spec_path),
            digest,
            raw_spec,
            now_iso,
        ),
    )

    row = connection.execute(
        """
        SELECT spec_id
        FROM strategy_specs
        WHERE spec_sha256 = ?
        """,
        (digest,),
    ).fetchone()

    if row is None:
        raise RuntimeError(
            "Could not resolve the strategy specification."
        )

    return int(row[0])


def default_run_id(
    spec: dict[str, Any],
    run_mode: str,
) -> str:
    name = spec["strategy"]["name"]
    version = spec["strategy"][
        "specification_version"
    ]

    return (
        f"{name}-{run_mode}-v{version}"
    )


def ensure_paper_run(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    spec_id: int,
    run_mode: str,
    initial_equity_usd: float,
    evaluation_timestamp: datetime,
) -> None:
    row = connection.execute(
        """
        SELECT
            spec_id,
            run_mode,
            status
        FROM paper_runs
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()

    if row is None:
        connection.execute(
            """
            INSERT INTO paper_runs (
                run_id,
                spec_id,
                run_mode,
                status,
                initial_equity_usd,
                started_at_utc,
                notes
            )
            VALUES (?, ?, ?, 'running', ?, ?, ?)
            """,
            (
                run_id,
                spec_id,
                run_mode,
                initial_equity_usd,
                utc_iso(evaluation_timestamp),
                (
                    "Created by the intraday "
                    "feature snapshot engine."
                ),
            ),
        )
        return

    existing_spec_id = int(row[0])
    existing_mode = str(row[1])
    existing_status = str(row[2])

    if existing_spec_id != spec_id:
        raise RuntimeError(
            f"Run {run_id!r} belongs to spec_id "
            f"{existing_spec_id}, not {spec_id}. "
            "Use a new run ID after changing the spec."
        )

    if existing_mode != run_mode:
        raise RuntimeError(
            f"Run {run_id!r} uses mode "
            f"{existing_mode!r}, not {run_mode!r}."
        )

    if existing_status in {
        "stopped",
        "failed",
    }:
        raise RuntimeError(
            f"Run {run_id!r} has terminal status "
            f"{existing_status!r}."
        )

    connection.execute(
        """
        UPDATE paper_runs
        SET status = 'running'
        WHERE run_id = ?
          AND status IN ('created', 'paused')
        """,
        (run_id,),
    )


def load_relationship_routes(
    connection: sqlite3.Connection,
    selection_year: int,
) -> list[RelationshipRoute]:
    rows = connection.execute(
        """
        SELECT
            r.relationship_id,
            r.commodity,
            r.currency,
            l.live_commodity_symbol,
            r.fx_symbol,
            r.fx_direction_multiplier,
            l.fx_direction_multiplier,
            l.transmission_beta,
            l.market_source_name,
            rw.selected,
            rw.selection_weight
        FROM relationships r
        JOIN live_instrument_registry l
          ON l.relationship_id = r.relationship_id
        LEFT JOIN relationship_weights rw
          ON rw.relationship_id = r.relationship_id
         AND rw.selection_year = ?
        WHERE r.active = 1
          AND l.active = 1
        ORDER BY r.relationship_id
        """,
        (selection_year,),
    ).fetchall()

    if not rows:
        raise RuntimeError(
            "No active intraday relationships were found."
        )

    routes: list[RelationshipRoute] = []

    for row in rows:
        (
            relationship_id,
            commodity,
            currency,
            commodity_symbol,
            fx_symbol,
            relationship_direction,
            registry_direction,
            transmission_beta,
            market_source_name,
            selected,
            selection_weight,
        ) = row

        if (
            relationship_direction is None
            or registry_direction is None
        ):
            raise RuntimeError(
                "Missing relationship direction for "
                f"{relationship_id}."
            )

        if int(
            relationship_direction
        ) != int(registry_direction):
            raise RuntimeError(
                "Relationship direction mismatch for "
                f"{relationship_id}: relationships="
                f"{relationship_direction}, registry="
                f"{registry_direction}."
            )

        if (
            selected is None
            or selection_weight is None
        ):
            raise RuntimeError(
                "Missing annual relationship weight for "
                f"{relationship_id} in {selection_year}."
            )

        routes.append(
            RelationshipRoute(
                relationship_id=str(
                    relationship_id
                ),
                commodity=str(commodity),
                currency=str(currency),
                commodity_symbol=str(
                    commodity_symbol
                ),
                fx_symbol=str(fx_symbol),
                relationship_direction=int(
                    relationship_direction
                ),
                transmission_beta=(
                    None
                    if transmission_beta is None
                    else float(transmission_beta)
                ),
                market_source_name=str(
                    market_source_name
                ),
                selected=int(selected),
                selection_weight=float(
                    selection_weight
                ),
            )
        )

    return routes


def load_bars(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    source_name: str,
    start_time: datetime,
    end_time: datetime,
) -> list[Bar]:
    normalized_source_name = (
        f"{source_name}_normalized"
    )

    rows = connection.execute(
        """
        SELECT
            bar_timestamp_utc,
            close_price
        FROM market_bars_1m
        WHERE symbol = ?
          AND source_name = ?
          AND is_complete = 1
          AND bar_timestamp_utc >= ?
          AND bar_timestamp_utc <= ?
        ORDER BY bar_timestamp_utc
        """,
        (
            symbol,
            normalized_source_name,
            utc_iso(start_time),
            utc_iso(end_time),
        ),
    ).fetchall()

    bars: list[Bar] = []

    for timestamp, close_price in rows:
        price = float(close_price)

        if (
            price <= 0
            or not math.isfinite(price)
        ):
            continue

        bars.append(
            Bar(
                timestamp=parse_utc_iso(
                    timestamp
                ),
                close_price=price,
            )
        )

    return bars


def latest_bar_at_or_before(
    bars: Iterable[Bar],
    target: datetime,
) -> Bar | None:
    result: Bar | None = None

    for bar in bars:
        if bar.timestamp > target:
            break

        result = bar

    return result


def bar_is_fresh_for_target(
    bar: Bar | None,
    target: datetime,
    maximum_lateness_seconds: int,
) -> bool:
    if bar is None:
        return False

    lateness = (
        target - bar.timestamp
    ).total_seconds()

    return (
        0 <= lateness
        <= maximum_lateness_seconds
    )


def simple_return(
    current: Bar | None,
    prior: Bar | None,
) -> float | None:
    if (
        current is None
        or prior is None
        or prior.close_price <= 0
    ):
        return None

    return finite_or_none(
        current.close_price
        / prior.close_price
        - 1.0
    )


def realized_volatility(
    bars: list[Bar],
    *,
    start_time: datetime,
    end_time: datetime,
) -> float | None:
    window = [
        bar
        for bar in bars
        if (
            start_time
            <= bar.timestamp
            <= end_time
        )
    ]

    if len(window) < 3:
        return None

    log_returns: list[float] = []

    for previous, current in zip(
        window,
        window[1:],
    ):
        if (
            previous.close_price <= 0
            or current.close_price <= 0
        ):
            continue

        value = math.log(
            current.close_price
            / previous.close_price
        )

        if math.isfinite(value):
            log_returns.append(value)

    if len(log_returns) < 2:
        return None

    # Root-sum-square realized volatility over the
    # complete trailing window. It remains in return units.
    return finite_or_none(
        math.sqrt(
            sum(
                value * value
                for value in log_returns
            )
        )
    )


def coverage_pct(
    bars: list[Bar],
    *,
    start_time: datetime,
    end_time: datetime,
    expected_bars: int,
) -> float:
    actual = len(
        {
            bar.timestamp
            for bar in bars
            if (
                start_time
                <= bar.timestamp
                <= end_time
            )
        }
    )

    if expected_bars <= 0:
        return 0.0

    return min(
        100.0,
        100.0
        * actual
        / expected_bars,
    )


def normalize_return(
    value: float | None,
    volatility: float | None,
    volatility_floor: float,
    *,
    return_window_minutes: int,
    volatility_window_minutes: int,
) -> float | None:
    """Divide a return by the volatility of a move over the same horizon.

    The volatility input is realised over `volatility_window_minutes` (60),
    but the returns being normalised span 15, 60 and 240 minutes. Dividing
    all three by the same 60-minute figure compares each return against the
    wrong yardstick: under the usual square-root-of-time scaling a 240-minute
    move has roughly twice the standard deviation of a 60-minute one, and a
    15-minute move about half.

    Left uncorrected this does not merely add noise, it silently re-weights
    the blend. Expressed in units of `return / volatility_60m`, the spec's
    intended 0.50 / 0.30 / 0.20 weighting was really behaving as
    1.00 / 0.30 / 0.10 -- the 15-minute term halved and the 240-minute term
    doubled -- so the impulse leaned on the long horizon far harder than the
    configuration says it does.
    """
    if (
        value is None
        or volatility is None
    ):
        return None

    horizon_scale = math.sqrt(
        return_window_minutes
        / volatility_window_minutes
    )

    denominator = max(
        volatility * horizon_scale,
        volatility_floor,
    )

    return finite_or_none(
        value / denominator
    )


def weighted_sum(
    values: list[tuple[float | None, float]],
) -> float | None:
    if any(
        value is None
        for value, _ in values
    ):
        return None

    return finite_or_none(
        sum(
            float(value) * weight
            for value, weight in values
            if value is not None
        )
    )


def build_market_features(
    connection: sqlite3.Connection,
    *,
    route: RelationshipRoute,
    evaluation_timestamp: datetime,
    spec: dict[str, Any],
) -> MarketFeatures:
    feature_spec = spec["features"]
    market_spec = spec["data"]["market"]

    commodity_windows = [
        int(value)
        for value in feature_spec[
            "commodity_return_windows_minutes"
        ]
    ]

    if commodity_windows != [
        15,
        60,
        240,
    ]:
        raise RuntimeError(
            "The current feature schema requires "
            "commodity windows [15, 60, 240]."
        )

    fx_window = int(
        feature_spec[
            "fx_return_window_minutes"
        ]
    )

    if fx_window != 15:
        raise RuntimeError(
            "The current feature schema requires a "
            "15-minute FX return window."
        )

    volatility_window = int(
        feature_spec[
            "realized_volatility_window_minutes"
        ]
    )

    if volatility_window != 60:
        raise RuntimeError(
            "The current feature schema requires a "
            "60-minute realized-volatility window."
        )

    maximum_window = max(
        max(commodity_windows),
        fx_window,
        volatility_window,
    )

    maximum_lateness_seconds = int(
        market_spec[
            "maximum_bar_lateness_seconds"
        ]
    )

    fetch_buffer_minutes = max(
        2,
        math.ceil(
            maximum_lateness_seconds / 60
        ),
    )

    start_time = (
        evaluation_timestamp
        - timedelta(
            minutes=(
                maximum_window
                + fetch_buffer_minutes
            )
        )
    )

    commodity_bars = load_bars(
        connection,
        symbol=route.commodity_symbol,
        source_name=route.market_source_name,
        start_time=start_time,
        end_time=evaluation_timestamp,
    )

    fx_bars = load_bars(
        connection,
        symbol=route.fx_symbol,
        source_name=route.market_source_name,
        start_time=start_time,
        end_time=evaluation_timestamp,
    )

    commodity_current = latest_bar_at_or_before(
        commodity_bars,
        evaluation_timestamp,
    )

    fx_current = latest_bar_at_or_before(
        fx_bars,
        evaluation_timestamp,
    )

    commodity_prior = {
        window: latest_bar_at_or_before(
            commodity_bars,
            evaluation_timestamp
            - timedelta(minutes=window),
        )
        for window in commodity_windows
    }

    fx_prior = latest_bar_at_or_before(
        fx_bars,
        evaluation_timestamp
        - timedelta(minutes=fx_window),
    )

    commodity_returns = {
        window: simple_return(
            commodity_current,
            commodity_prior[window],
        )
        for window in commodity_windows
    }

    fx_return = simple_return(
        fx_current,
        fx_prior,
    )

    volatility_start = (
        evaluation_timestamp
        - timedelta(
            minutes=volatility_window
        )
    )

    commodity_volatility = (
        realized_volatility(
            commodity_bars,
            start_time=volatility_start,
            end_time=evaluation_timestamp,
        )
    )

    fx_volatility = realized_volatility(
        fx_bars,
        start_time=volatility_start,
        end_time=evaluation_timestamp,
    )

    volatility_floor = float(
        feature_spec["normalization"][
            "minimum_volatility_floor"
        ]
    )

    normalized_commodity = {
        window: normalize_return(
            commodity_returns[window],
            commodity_volatility,
            volatility_floor,
            return_window_minutes=window,
            volatility_window_minutes=volatility_window,
        )
        for window in commodity_windows
    }

    # The FX leg has the same mismatch: its return window is 15 minutes while
    # its volatility is realised over 60.
    normalized_fx = normalize_return(
        fx_return,
        fx_volatility,
        volatility_floor,
        return_window_minutes=fx_window,
        volatility_window_minutes=volatility_window,
    )

    impulse_weights = feature_spec[
        "market_impulse_weights"
    ]

    commodity_impulse = weighted_sum(
        [
            (
                normalized_commodity[15],
                float(
                    impulse_weights[
                        "return_15m"
                    ]
                ),
            ),
            (
                normalized_commodity[60],
                float(
                    impulse_weights[
                        "return_60m"
                    ]
                ),
            ),
            (
                normalized_commodity[240],
                float(
                    impulse_weights[
                        "return_240m"
                    ]
                ),
            ),
        ]
    )

    commodity_coverage = coverage_pct(
        commodity_bars,
        start_time=(
            evaluation_timestamp
            - timedelta(
                minutes=max(
                    commodity_windows
                )
            )
        ),
        end_time=evaluation_timestamp,
        expected_bars=(
            max(commodity_windows)
            + 1
        ),
    )

    fx_coverage = coverage_pct(
        fx_bars,
        start_time=(
            evaluation_timestamp
            - timedelta(
                minutes=max(
                    fx_window,
                    volatility_window,
                )
            )
        ),
        end_time=evaluation_timestamp,
        expected_bars=(
            max(
                fx_window,
                volatility_window,
            )
            + 1
        ),
    )

    market_coverage = min(
        commodity_coverage,
        fx_coverage,
    )

    endpoint_freshness = [
        bar_is_fresh_for_target(
            commodity_current,
            evaluation_timestamp,
            maximum_lateness_seconds,
        ),
        bar_is_fresh_for_target(
            fx_current,
            evaluation_timestamp,
            maximum_lateness_seconds,
        ),
        *[
            bar_is_fresh_for_target(
                commodity_prior[window],
                evaluation_timestamp
                - timedelta(minutes=window),
                maximum_lateness_seconds,
            )
            for window in commodity_windows
        ],
        bar_is_fresh_for_target(
            fx_prior,
            evaluation_timestamp
            - timedelta(minutes=fx_window),
            maximum_lateness_seconds,
        ),
    ]

    minimum_coverage = float(
        market_spec[
            "minimum_window_coverage_pct"
        ]
    )

    required_values = [
        *commodity_returns.values(),
        fx_return,
        commodity_volatility,
        fx_volatility,
        *normalized_commodity.values(),
        normalized_fx,
        commodity_impulse,
    ]

    complete = int(
        market_coverage >= minimum_coverage
        and all(endpoint_freshness)
        and all(
            value is not None
            for value in required_values
        )
    )

    return MarketFeatures(
        commodity_return_15m=finite_or_none(
            commodity_returns[15]
        ),
        commodity_return_60m=finite_or_none(
            commodity_returns[60]
        ),
        commodity_return_240m=finite_or_none(
            commodity_returns[240]
        ),
        fx_return_15m=finite_or_none(
            fx_return
        ),
        commodity_realized_volatility_60m=(
            finite_or_none(
                commodity_volatility
            )
        ),
        fx_realized_volatility_60m=(
            finite_or_none(
                fx_volatility
            )
        ),
        normalized_commodity_return_15m=(
            finite_or_none(
                normalized_commodity[15]
            )
        ),
        normalized_commodity_return_60m=(
            finite_or_none(
                normalized_commodity[60]
            )
        ),
        normalized_commodity_return_240m=(
            finite_or_none(
                normalized_commodity[240]
            )
        ),
        normalized_fx_return_15m=(
            finite_or_none(
                normalized_fx
            )
        ),
        commodity_impulse=finite_or_none(
            commodity_impulse
        ),
        market_window_coverage_pct=(
            finite_or_none(
                market_coverage
            )
            or 0.0
        ),
        market_data_complete=complete,
    )


def news_decay(
    age_minutes: float,
    *,
    full_weight_minutes: int,
    half_life_minutes: int,
    expiry_minutes: int,
) -> float:
    if (
        age_minutes < 0
        or age_minutes > expiry_minutes
    ):
        return 0.0

    if age_minutes <= full_weight_minutes:
        return 1.0

    decay_age = (
        age_minutes
        - full_weight_minutes
    )

    return 0.5 ** (
        decay_age
        / half_life_minutes
    )


def aggregate_news_scores(
    scores: list[float],
) -> float:
    if not scores:
        return 0.0

    # Each article score already contains sentiment,
    # confidence, and time decay. Taking their mean
    # preserves the [-1, 1] scale while allowing every
    # article's influence to fade toward zero.
    return finite_or_none(
        statistics.fmean(scores)
    ) or 0.0


def build_news_features(
    connection: sqlite3.Connection,
    *,
    route: RelationshipRoute,
    evaluation_timestamp: datetime,
    spec: dict[str, Any],
) -> NewsFeatures:
    news_spec = spec["features"]["news"]
    classification_spec = spec["data"]["news"][
        "sentiment_classification"
    ]

    full_weight_minutes = int(
        news_spec["full_weight_minutes"]
    )

    half_life_minutes = int(
        news_spec["decay_half_life_minutes"]
    )

    expiry_minutes = int(
        news_spec["expiry_minutes"]
    )

    minimum_confidence = float(
        classification_spec[
            "minimum_relevance_confidence"
        ]
    )

    start_time = (
        evaluation_timestamp
        - timedelta(
            minutes=expiry_minutes
        )
    )

    rows = connection.execute(
        """
        SELECT
            nca.asset,
            nca.asset_type,
            nca.sentiment,
            nca.confidence,
            na.publication_timestamp_utc
        FROM news_classification_assets nca
        JOIN news_classifications nc
          ON nc.classification_id =
             nca.classification_id
        JOIN news_articles na
          ON na.article_id = nc.article_id
        WHERE nc.relevant = 1
          AND nca.confidence >= ?
          AND (
                (
                    nca.asset_type = 'commodity'
                    AND nca.asset = ?
                )
                OR
                (
                    nca.asset_type = 'currency'
                    AND nca.asset = ?
                )
          )
          AND na.publication_timestamp_utc >= ?
          AND na.publication_timestamp_utc <= ?
          AND na.retrieval_timestamp_utc <= ?
          AND nc.classified_at_utc <= ?
        ORDER BY na.publication_timestamp_utc
        """,
        (
            minimum_confidence,
            route.commodity,
            route.currency,
            utc_iso(start_time),
            utc_iso(evaluation_timestamp),
            utc_iso(evaluation_timestamp),
            utc_iso(evaluation_timestamp),
        ),
    ).fetchall()

    commodity_scores: list[float] = []
    fx_scores: list[float] = []

    for (
        asset,
        asset_type,
        sentiment,
        confidence,
        publication_timestamp,
    ) in rows:
        published_at = parse_utc_iso(
            publication_timestamp
        )

        age_minutes = (
            evaluation_timestamp
            - published_at
        ).total_seconds() / 60.0

        decay = news_decay(
            age_minutes,
            full_weight_minutes=(
                full_weight_minutes
            ),
            half_life_minutes=(
                half_life_minutes
            ),
            expiry_minutes=expiry_minutes,
        )

        score = (
            float(sentiment)
            * float(confidence)
            * decay
        )

        if not math.isfinite(score):
            continue

        if (
            asset_type == "commodity"
            and asset == route.commodity
        ):
            commodity_scores.append(score)

        elif (
            asset_type == "currency"
            and asset == route.currency
        ):
            fx_scores.append(score)

    commodity_news = aggregate_news_scores(
        commodity_scores
    )

    fx_news = aggregate_news_scores(
        fx_scores
    )

    # Convert direct currency news into commodity-
    # equivalent units before combining it. Applying the
    # relationship direction later converts the net news
    # impulse back into expected FX direction exactly once.
    net_news = (
        commodity_news
        - route.relationship_direction
        * fx_news
    )

    return NewsFeatures(
        commodity_news_impulse=(
            finite_or_none(
                commodity_news
            )
            or 0.0
        ),
        fx_news_impulse=(
            finite_or_none(
                fx_news
            )
            or 0.0
        ),
        net_news_impulse=(
            finite_or_none(
                net_news
            )
            or 0.0
        ),
        relevant_news_count=(
            len(commodity_scores)
            + len(fx_scores)
        ),
    )


def extract_news_impulse_coefficient(
    spec: dict[str, Any],
) -> float:
    formula = str(
        spec["signals"]["formulas"][
            "expected_fx_impulse"
        ]
    )

    compact = " ".join(
        formula.split()
    )

    match = re.search(
        r"([0-9]+(?:\.[0-9]+)?)"
        r"\s*\*\s*news_impulse\b",
        compact,
    )

    if match is None:
        raise RuntimeError(
            "Expected-FX formula does not contain "
            "a numeric news_impulse coefficient."
        )

    coefficient = float(
        match.group(1)
    )

    if (
        not math.isfinite(coefficient)
        or coefficient < 0
    ):
        raise RuntimeError(
            "News-impulse coefficient must be a "
            "finite non-negative number."
        )

    return coefficient


def upsert_feature_snapshot(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    spec_id: int,
    route: RelationshipRoute,
    evaluation_timestamp: datetime,
    market: MarketFeatures,
    news: NewsFeatures,
    news_coefficient: float,
    created_at_utc: str,
) -> None:
    # The transmission coefficient replaces the fixed +/-1 direction as the
    # *magnitude* of the expected move. A relationship with no measured beta
    # produces no expected impulse at all: falling back to 1 would keep the
    # very error this replaces, and would do so precisely for the
    # relationships nobody has checked.
    #
    # relationship_direction is untouched elsewhere. The news blend uses it
    # to convert currency news into commodity-equivalent units, which is a
    # unit conversion rather than a magnitude, and beta is the wrong tool
    # for that job.
    if (
        market.commodity_impulse is None
        or route.transmission_beta is None
    ):
        expected_fx_impulse = None
        observed_fx_impulse = (
            market.normalized_fx_return_15m
        )
        divergence_score = None
    else:
        expected_fx_impulse = (
            route.transmission_beta
            * (
                market.commodity_impulse
                + news_coefficient
                * news.net_news_impulse
            )
        )

        observed_fx_impulse = (
            market.normalized_fx_return_15m
        )

        divergence_score = (
            None
            if observed_fx_impulse is None
            else (
                expected_fx_impulse
                - observed_fx_impulse
            )
        )

    connection.execute(
        """
        INSERT INTO feature_snapshots (
            run_id,
            spec_id,
            relationship_id,
            feature_timestamp_utc,
            commodity_return_15m,
            commodity_return_60m,
            commodity_return_240m,
            fx_return_15m,
            realized_volatility_60m,
            normalized_commodity_return_15m,
            normalized_commodity_return_60m,
            normalized_commodity_return_240m,
            normalized_fx_return_15m,
            commodity_impulse,
            news_impulse,
            expected_fx_impulse,
            observed_fx_impulse,
            divergence_score,
            relevant_news_count,
            market_window_coverage_pct,
            market_data_complete,
            created_at_utc
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT (
            run_id,
            relationship_id,
            feature_timestamp_utc
        )
        DO UPDATE SET
            spec_id = excluded.spec_id,
            commodity_return_15m =
                excluded.commodity_return_15m,
            commodity_return_60m =
                excluded.commodity_return_60m,
            commodity_return_240m =
                excluded.commodity_return_240m,
            fx_return_15m =
                excluded.fx_return_15m,
            realized_volatility_60m =
                excluded.realized_volatility_60m,
            normalized_commodity_return_15m =
                excluded.normalized_commodity_return_15m,
            normalized_commodity_return_60m =
                excluded.normalized_commodity_return_60m,
            normalized_commodity_return_240m =
                excluded.normalized_commodity_return_240m,
            normalized_fx_return_15m =
                excluded.normalized_fx_return_15m,
            commodity_impulse =
                excluded.commodity_impulse,
            news_impulse =
                excluded.news_impulse,
            expected_fx_impulse =
                excluded.expected_fx_impulse,
            observed_fx_impulse =
                excluded.observed_fx_impulse,
            divergence_score =
                excluded.divergence_score,
            relevant_news_count =
                excluded.relevant_news_count,
            market_window_coverage_pct =
                excluded.market_window_coverage_pct,
            market_data_complete =
                excluded.market_data_complete,
            created_at_utc =
                excluded.created_at_utc
        """,
        (
            run_id,
            spec_id,
            route.relationship_id,
            utc_iso(evaluation_timestamp),
            market.commodity_return_15m,
            market.commodity_return_60m,
            market.commodity_return_240m,
            market.fx_return_15m,
            (
                market
                .commodity_realized_volatility_60m
            ),
            (
                market
                .normalized_commodity_return_15m
            ),
            (
                market
                .normalized_commodity_return_60m
            ),
            (
                market
                .normalized_commodity_return_240m
            ),
            market.normalized_fx_return_15m,
            market.commodity_impulse,
            news.net_news_impulse,
            finite_or_none(
                expected_fx_impulse
            ),
            finite_or_none(
                observed_fx_impulse
            ),
            finite_or_none(
                divergence_score
            ),
            news.relevant_news_count,
            market.market_window_coverage_pct,
            market.market_data_complete,
            created_at_utc,
        ),
    )


def update_heartbeat(
    connection: sqlite3.Connection,
    *,
    status: str,
    details: dict[str, Any],
) -> None:
    now_iso = utc_iso(utc_now())

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
            last_heartbeat_utc =
                excluded.last_heartbeat_utc,
            details_json =
                excluded.details_json,
            updated_at_utc =
                excluded.updated_at_utc
        """,
        (
            FEATURE_SERVICE_NAME,
            status,
            now_iso,
            json.dumps(
                details,
                sort_keys=True,
            ),
            now_iso,
        ),
    )


def build_feature_snapshots(
    *,
    database_path: Path,
    spec_path: Path,
    evaluation_timestamp: datetime | None,
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

    with sqlite3.connect(
        database_path,
        timeout=5.0,
    ) as connection:
        configure_connection(connection)
        require_tables(connection)

        resolved_timestamp = (
            evaluation_timestamp
            if evaluation_timestamp is not None
            else resolve_latest_evaluation_timestamp(
                connection,
                interval_minutes,
                lag_minutes=int(
                    spec["data"]["market"].get(
                        "evaluation_lag_minutes", 0
                    )
                ),
            )
        )

        resolved_timestamp = (
            parse_evaluation_timestamp(
                utc_iso(resolved_timestamp),
                interval_minutes,
            )
        )

        spec_id = ensure_strategy_spec(
            connection,
            spec,
        )

        resolved_run_id = (
            run_id
            if run_id is not None
            else default_run_id(
                spec,
                run_mode,
            )
        )

        ensure_paper_run(
            connection,
            run_id=resolved_run_id,
            spec_id=spec_id,
            run_mode=run_mode,
            initial_equity_usd=float(
                spec["capital"][
                    "initial_equity_usd"
                ]
            ),
            evaluation_timestamp=(
                resolved_timestamp
            ),
        )

        routes = load_relationship_routes(
            connection,
            resolved_timestamp.year,
        )

        news_coefficient = (
            extract_news_impulse_coefficient(
                spec
            )
        )

        created_at = utc_iso(utc_now())

        complete_count = 0
        relevant_news_count = 0
        selected_count = 0
        total_weight = 0.0
        fx_volatility_missing = 0

        for route in routes:
            market = build_market_features(
                connection,
                route=route,
                evaluation_timestamp=(
                    resolved_timestamp
                ),
                spec=spec,
            )

            news = build_news_features(
                connection,
                route=route,
                evaluation_timestamp=(
                    resolved_timestamp
                ),
                spec=spec,
            )

            upsert_feature_snapshot(
                connection,
                run_id=resolved_run_id,
                spec_id=spec_id,
                route=route,
                evaluation_timestamp=(
                    resolved_timestamp
                ),
                market=market,
                news=news,
                news_coefficient=(
                    news_coefficient
                ),
                created_at_utc=created_at,
            )

            complete_count += (
                market.market_data_complete
            )

            relevant_news_count += (
                news.relevant_news_count
            )

            selected_count += route.selected
            total_weight += (
                route.selection_weight
            )

            if (
                market
                .fx_realized_volatility_60m
                is None
            ):
                fx_volatility_missing += 1

        details = {
            "run_id": resolved_run_id,
            "spec_id": spec_id,
            "feature_timestamp_utc": utc_iso(
                resolved_timestamp
            ),
            "relationships_processed": len(
                routes
            ),
            "market_complete": complete_count,
            "market_incomplete": (
                len(routes)
                - complete_count
            ),
            "selected_relationships": (
                selected_count
            ),
            "relationship_weight_sum": (
                total_weight
            ),
            "relevant_news_impacts": (
                relevant_news_count
            ),
            "fx_volatility_missing": (
                fx_volatility_missing
            ),
        }

        heartbeat_status = (
            "healthy"
            if complete_count == len(routes)
            else "degraded"
        )

        update_heartbeat(
            connection,
            status=heartbeat_status,
            details=details,
        )

        foreign_key_errors = (
            connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
        )

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
            "Build one five-minute feature snapshot "
            "for every active commodity-FX "
            "relationship."
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
        default=(
            PROJECT_ROOT
            / "strategy"
            / "config"
            / "intraday"
            / "intraday_strategy_spec.yaml"
        ),
    )

    parser.add_argument(
        "--timestamp",
        help=(
            "UTC evaluation timestamp on a five-minute "
            "boundary. Defaults to the latest available "
            "completed market timestamp."
        ),
    )

    parser.add_argument(
        "--run-id",
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

    evaluation_timestamp = (
        None
        if args.timestamp is None
        else parse_utc_iso(
            args.timestamp
        )
    )

    details = build_feature_snapshots(
        database_path=args.database,
        spec_path=args.spec,
        evaluation_timestamp=(
            evaluation_timestamp
        ),
        run_id=args.run_id,
        run_mode=args.run_mode,
    )

    print(
        "Feature snapshot build completed "
        "successfully."
    )

    for key, value in details.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()