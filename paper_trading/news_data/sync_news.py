from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import (
    parse_qsl,
    urlencode,
    urlsplit,
    urlunsplit,
)

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

from paper_trading.database.init_database import (
    DEFAULT_DATABASE_PATH,
    configure_connection,
    initialize_database,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]

STATE_NAME = "postgres_news_pipeline"

PROVIDER = "openai"
PROMPT_VERSION = "collector_multi_asset_v1"

SOURCE_NAME_MAP = {
    "reuters": "Reuters",
    "marketwatch": "MarketWatch",
    "investing": "Investing.com",
}

ALLOWED_ASSET_TYPES = {
    "commodity",
    "currency",
}

ALLOWED_DIRECTIONS = {
    "bullish",
    "bearish",
    "neutral",
}

OVERLAP_MINUTES = 5


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(
            tzinfo=timezone.utc
        )

    return value.astimezone(
        timezone.utc
    ).isoformat()


def parse_utc(value: str) -> datetime:
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


def canonical_url(url: str) -> str:
    url = str(url).strip()

    if not url:
        raise ValueError(
            "Article URL cannot be blank."
        )

    parts = urlsplit(url)

    host = parts.netloc.lower()

    if host.startswith("www."):
        host = host[4:]

    excluded_parameters = {
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
    }

    query_items = []

    for key, value in parse_qsl(
        parts.query,
        keep_blank_values=True,
    ):
        key_lower = key.lower()

        if key_lower.startswith("utm_"):
            continue

        if key_lower in excluded_parameters:
            continue

        query_items.append(
            (key, value)
        )

    return urlunsplit(
        (
            parts.scheme.lower() or "https",
            host,
            parts.path,
            urlencode(
                sorted(query_items),
                doseq=True,
            ),
            "",
        )
    )


def deterministic_article_id(
    source_id: int,
) -> str:
    return f"postgres:{int(source_id)}"


def deduplication_key(
    canonical: str,
) -> str:
    return hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()


def validate_impact(
    impact: dict[str, Any],
) -> dict[str, Any]:
    asset = str(
        impact["asset"]
    ).strip()

    asset_type = str(
        impact["asset_type"]
    ).strip()

    direction = str(
        impact["direction"]
    ).strip()

    sentiment = float(
        impact["sentiment_score"]
    )

    confidence = float(
        impact["confidence"]
    )

    reasoning = (
        None
        if impact.get("reasoning") is None
        else str(impact["reasoning"])[:500]
    )

    if not asset:
        raise ValueError(
            "News impact asset is blank."
        )

    if asset_type not in ALLOWED_ASSET_TYPES:
        raise ValueError(
            f"Invalid asset type: {asset_type}"
        )

    if direction not in ALLOWED_DIRECTIONS:
        raise ValueError(
            f"Invalid direction: {direction}"
        )

    if not -1.0 <= sentiment <= 1.0:
        raise ValueError(
            f"Invalid sentiment: {sentiment}"
        )

    if not 0.0 <= confidence <= 1.0:
        raise ValueError(
            f"Invalid confidence: {confidence}"
        )

    return {
        "asset": asset,
        "asset_type": asset_type,
        "direction": direction,
        "sentiment_score": sentiment,
        "confidence": confidence,
        "reasoning": reasoning,
    }


def summary_sentiment(
    impacts: list[dict[str, Any]],
) -> tuple[float, float]:
    if not impacts:
        return 0.0, 0.0

    weights = [
        impact["confidence"]
        for impact in impacts
    ]

    weight_sum = sum(weights)

    if weight_sum > 0:
        sentiment = sum(
            impact["sentiment_score"]
            * impact["confidence"]
            for impact in impacts
        ) / weight_sum
    else:
        sentiment = sum(
            impact["sentiment_score"]
            for impact in impacts
        ) / len(impacts)

    confidence = max(
        impact["confidence"]
        for impact in impacts
    )

    return (
        max(-1.0, min(1.0, sentiment)),
        max(0.0, min(1.0, confidence)),
    )


def get_start_time(
    connection: sqlite3.Connection,
    *,
    cutoff: datetime,
    lookback_hours: int,
) -> datetime:
    row = connection.execute(
        """
        SELECT last_source_update_utc
        FROM news_ingestion_state
        WHERE source_name = ?
        """,
        (STATE_NAME,),
    ).fetchone()

    if row is None:
        return cutoff - timedelta(
            hours=lookback_hours
        )

    return parse_utc(
        row[0]
    ) - timedelta(
        minutes=OVERLAP_MINUTES
    )


def fetch_articles(
    source_connection,
    *,
    start_time: datetime,
    cutoff: datetime,
) -> list[dict[str, Any]]:
    with source_connection.cursor(
        cursor_factory=RealDictCursor
    ) as cursor:
        cursor.execute(
            """
            SELECT
                id,
                source,
                title,
                url,
                published,
                summary,
                received_at_utc
            FROM news_articles
            WHERE received_at_utc > %s
              AND received_at_utc <= %s
            ORDER BY
                received_at_utc,
                id
            """,
            (
                start_time,
                cutoff,
            ),
        )

        return list(cursor.fetchall())


def fetch_status_rows(
    source_connection,
    *,
    start_time: datetime,
    cutoff: datetime,
) -> list[dict[str, Any]]:
    with source_connection.cursor(
        cursor_factory=RealDictCursor
    ) as cursor:
        cursor.execute(
            """
            SELECT
                na.id,
                na.source,
                na.title,
                na.url,
                na.published,
                na.summary,
                na.received_at_utc,

                nss.model,
                nss.status,
                nss.impacts_count,
                nss.attempts,
                nss.error,
                nss.updated_at_utc,

                ns.asset,
                ns.asset_type,
                ns.direction,
                ns.sentiment_score,
                ns.confidence,
                ns.reasoning,
                ns.created_at_utc

            FROM news_sentiment_status nss

            JOIN news_articles na
              ON na.id = nss.article_id

            LEFT JOIN news_sentiment ns
              ON ns.article_id = nss.article_id
             AND ns.model = nss.model

            WHERE nss.updated_at_utc > %s
              AND nss.updated_at_utc <= %s

            ORDER BY
                nss.updated_at_utc,
                na.id,
                nss.model,
                ns.id
            """,
            (
                start_time,
                cutoff,
            ),
        )

        return list(cursor.fetchall())


def upsert_article(
    connection: sqlite3.Connection,
    *,
    row: dict[str, Any],
    processing_status: str,
    created_at: str,
) -> str:
    source_key = str(
        row["source"]
    ).strip().lower()

    source_name = SOURCE_NAME_MAP.get(
        source_key
    )

    if source_name is None:
        raise ValueError(
            f"Unexpected news source: {source_key}"
        )

    canonical = canonical_url(
        row["url"]
    )

    article_id = deterministic_article_id(
        row["id"]
    )

    published = (
        row["published"]
        if row["published"] is not None
        else row["received_at_utc"]
    )

    raw_payload = {
        "postgres_article_id": int(row["id"]),
        "source_key": source_key,
        "normalized_source_name": source_name,
        "published": to_utc_iso(
            row["published"]
        ),
        "received_at_utc": to_utc_iso(
            row["received_at_utc"]
        ),
    }

    connection.execute(
        """
        INSERT INTO news_articles (
            article_id,
            source_name,
            url,
            canonical_url,
            headline,
            summary,
            publication_timestamp_utc,
            retrieval_timestamp_utc,
            deduplication_key,
            processing_status,
            raw_payload_json,
            created_at_utc
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?
        )

        ON CONFLICT(article_id)
        DO UPDATE SET
            source_name =
                excluded.source_name,
            url =
                excluded.url,
            canonical_url =
                excluded.canonical_url,
            headline =
                excluded.headline,
            summary =
                excluded.summary,
            publication_timestamp_utc =
                excluded.publication_timestamp_utc,
            retrieval_timestamp_utc =
                excluded.retrieval_timestamp_utc,
            deduplication_key =
                excluded.deduplication_key,
            processing_status =
                excluded.processing_status,
            raw_payload_json =
                excluded.raw_payload_json
        """,
        (
            article_id,
            source_name,
            str(row["url"]).strip(),
            canonical,
            str(row["title"]).strip(),
            row["summary"],
            to_utc_iso(published),
            to_utc_iso(
                row["received_at_utc"]
            ),
            deduplication_key(
                canonical
            ),
            processing_status,
            json.dumps(
                raw_payload,
                sort_keys=True,
            ),
            created_at,
        ),
    )

    return article_id


def upsert_classification(
    connection: sqlite3.Connection,
    *,
    article_id: str,
    model_name: str,
    status_row: dict[str, Any],
    impacts: list[dict[str, Any]],
) -> int:
    (
        article_sentiment,
        article_confidence,
    ) = summary_sentiment(
        impacts
    )

    commodity_names = sorted(
        {
            impact["asset"]
            for impact in impacts
            if impact["asset_type"]
            == "commodity"
        }
    )

    raw_response = {
        "status": status_row["status"],
        "impacts_count": int(
            status_row["impacts_count"]
        ),
        "attempts": int(
            status_row["attempts"]
        ),
        "error": status_row["error"],
        "impacts": impacts,
        "summary_sentiment_is_for_display_only": True,
    }

    existing = connection.execute(
        """
        SELECT classification_id
        FROM news_classifications
        WHERE article_id = ?
          AND provider = ?
          AND model_name = ?
          AND prompt_version = ?
        """,
        (
            article_id,
            PROVIDER,
            model_name,
            PROMPT_VERSION,
        ),
    ).fetchone()

    values = (
        1 if impacts else 0,
        article_sentiment,
        article_confidence,
        "multi_asset_news",
        json.dumps(
            commodity_names
        ),
        json.dumps(
            raw_response,
            sort_keys=True,
        ),
        to_utc_iso(
            status_row["updated_at_utc"]
        ),
    )

    if existing is None:
        cursor = connection.execute(
            """
            INSERT INTO news_classifications (
                article_id,
                provider,
                model_name,
                prompt_version,
                relevant,
                sentiment,
                confidence,
                event_type,
                commodities_json,
                raw_response_json,
                input_tokens,
                output_tokens,
                estimated_cost_usd,
                classified_at_utc
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, 0, 0, 0, ?
            )
            """,
            (
                article_id,
                PROVIDER,
                model_name,
                PROMPT_VERSION,
                *values,
            ),
        )

        classification_id = int(
            cursor.lastrowid
        )
    else:
        classification_id = int(
            existing[0]
        )

        connection.execute(
            """
            UPDATE news_classifications
            SET
                relevant = ?,
                sentiment = ?,
                confidence = ?,
                event_type = ?,
                commodities_json = ?,
                raw_response_json = ?,
                input_tokens = 0,
                output_tokens = 0,
                estimated_cost_usd = 0,
                classified_at_utc = ?
            WHERE classification_id = ?
            """,
            (
                *values,
                classification_id,
            ),
        )

    connection.execute(
        """
        DELETE FROM
            news_classification_assets
        WHERE classification_id = ?
        """,
        (classification_id,),
    )

    connection.execute(
        """
        DELETE FROM
            news_classification_commodities
        WHERE classification_id = ?
        """,
        (classification_id,),
    )

    for impact in impacts:
        connection.execute(
            """
            INSERT INTO
                news_classification_assets (
                    classification_id,
                    asset,
                    asset_type,
                    direction,
                    sentiment,
                    confidence,
                    reasoning
                )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                classification_id,
                impact["asset"],
                impact["asset_type"],
                impact["direction"],
                impact["sentiment_score"],
                impact["confidence"],
                impact["reasoning"],
            ),
        )

        if impact["asset_type"] == "commodity":
            connection.execute(
                """
                INSERT INTO
                    news_classification_commodities (
                        classification_id,
                        commodity
                    )
                VALUES (?, ?)
                """,
                (
                    classification_id,
                    impact["asset"],
                ),
            )

    return classification_id


def update_state(
    connection: sqlite3.Connection,
    *,
    cutoff: str,
    sync_time: str,
    articles_written: int,
    classifications_written: int,
) -> None:
    connection.execute(
        """
        INSERT INTO news_ingestion_state (
            source_name,
            last_source_update_utc,
            last_sync_at_utc,
            articles_written,
            classifications_written
        )
        VALUES (?, ?, ?, ?, ?)

        ON CONFLICT(source_name)
        DO UPDATE SET
            last_source_update_utc =
                excluded.last_source_update_utc,
            last_sync_at_utc =
                excluded.last_sync_at_utc,
            articles_written =
                news_ingestion_state.articles_written
                + excluded.articles_written,
            classifications_written =
                news_ingestion_state.classifications_written
                + excluded.classifications_written
        """,
        (
            STATE_NAME,
            cutoff,
            sync_time,
            articles_written,
            classifications_written,
        ),
    )


def update_heartbeat(
    connection: sqlite3.Connection,
    *,
    timestamp: str,
    details: dict[str, Any],
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
        VALUES (?, 'healthy', ?, ?, ?)

        ON CONFLICT(service_name)
        DO UPDATE SET
            status = 'healthy',
            last_heartbeat_utc =
                excluded.last_heartbeat_utc,
            details_json =
                excluded.details_json,
            updated_at_utc =
                excluded.updated_at_utc
        """,
        (
            "news_data_adapter",
            timestamp,
            json.dumps(
                details,
                sort_keys=True,
            ),
            timestamp,
        ),
    )


def sync_news(
    *,
    lookback_hours: int,
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> dict[str, int]:
    if lookback_hours < 1:
        raise ValueError(
            "lookback_hours must be positive."
        )

    initialize_database(
        database_path=database_path
    )

    load_dotenv(
        PROJECT_ROOT / ".env"
    )

    database_url = os.getenv(
        "DATABASE_URL"
    )

    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is not set."
        )

    cutoff = utc_now()
    sync_time = to_utc_iso(cutoff)

    source_connection = psycopg2.connect(
        database_url
    )

    try:
        with sqlite3.connect(
            database_path
        ) as destination:
            configure_connection(
                destination
            )

            start_time = get_start_time(
                destination,
                cutoff=cutoff,
                lookback_hours=lookback_hours,
            )

            article_rows = fetch_articles(
                source_connection,
                start_time=start_time,
                cutoff=cutoff,
            )

            status_rows = fetch_status_rows(
                source_connection,
                start_time=start_time,
                cutoff=cutoff,
            )

            articles_written = 0

            for row in article_rows:
                upsert_article(
                    destination,
                    row=row,
                    processing_status="pending",
                    created_at=sync_time,
                )

                articles_written += 1

            grouped: dict[
                tuple[int, str],
                dict[str, Any],
            ] = defaultdict(
                lambda: {
                    "row": None,
                    "impacts": [],
                }
            )

            for row in status_rows:
                key = (
                    int(row["id"]),
                    str(row["model"]),
                )

                grouped[key]["row"] = row

                if row["asset"] is not None:
                    grouped[key]["impacts"].append(
                        validate_impact(row)
                    )

            classifications_written = 0
            failed_articles = 0

            for group in grouped.values():
                row = group["row"]

                if row is None:
                    continue

                status = str(
                    row["status"]
                ).strip().lower()

                processing_status = (
                    "classified"
                    if status == "success"
                    else "failed"
                )

                article_id = upsert_article(
                    destination,
                    row=row,
                    processing_status=(
                        processing_status
                    ),
                    created_at=sync_time,
                )

                if status == "success":
                    upsert_classification(
                        destination,
                        article_id=article_id,
                        model_name=str(
                            row["model"]
                        ),
                        status_row=row,
                        impacts=group["impacts"],
                    )

                    classifications_written += 1
                else:
                    failed_articles += 1

            details = {
                "article_rows": len(
                    article_rows
                ),
                "status_groups": len(
                    grouped
                ),
                "classifications_written":
                    classifications_written,
                "failed_articles":
                    failed_articles,
                "start_time_utc":
                    to_utc_iso(start_time),
                "cutoff_time_utc":
                    sync_time,
            }

            update_state(
                destination,
                cutoff=sync_time,
                sync_time=sync_time,
                articles_written=(
                    articles_written
                ),
                classifications_written=(
                    classifications_written
                ),
            )

            update_heartbeat(
                destination,
                timestamp=sync_time,
                details=details,
            )

            foreign_key_errors = (
                destination.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
            )

            if foreign_key_errors:
                destination.rollback()

                raise RuntimeError(
                    "Foreign-key validation failed: "
                    f"{foreign_key_errors[:10]}"
                )

            destination.commit()

    finally:
        source_connection.close()

    return {
        "articles_written":
            articles_written,
        "status_groups":
            len(grouped),
        "classifications_written":
            classifications_written,
        "failed_articles":
            failed_articles,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Synchronize normalized news and "
            "multi-asset sentiment from PostgreSQL "
            "into the paper-trading SQLite database."
        )
    )

    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=168,
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    result = sync_news(
        lookback_hours=args.lookback_hours
    )

    print(
        "News sync completed successfully."
    )

    for key, value in result.items():
        print(
            f"{key}: {value}"
        )


if __name__ == "__main__":
    main()
