from __future__ import annotations

import csv
import json
import os
import time
from pathlib import Path
from typing import Any

import psycopg2
from dotenv import load_dotenv
from openai import OpenAI

from data_collector.news_data.config.assets import ALL_ASSETS

load_dotenv()

MODEL_NAME = "gpt-5.5"

MAX_ARTICLES_PER_RUN = int(os.getenv("OPENAI_MAX_ARTICLES_PER_RUN", "0"))
MAX_RETRIES = int(os.getenv("OPENAI_MAX_RETRIES", "3"))
MAX_FAILED_ATTEMPTS_PER_ARTICLE = int(
    os.getenv("OPENAI_MAX_FAILED_ATTEMPTS_PER_ARTICLE", "3")
)
SLEEP_SECONDS = float(os.getenv("OPENAI_SLEEP_SECONDS", "1.0"))

OUTPUT_DIR = Path("data_collector/news_data/output")
FAILED_LOG_PATH = OUTPUT_DIR / "news_sentiment_failed_articles.csv"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


PROMPT_TEMPLATE = """
You are a commodities, FX, and macroeconomic analyst.

Analyze the following news article and identify which assets from the approved list are materially affected.

Allowed assets:
{assets}

Rules:
- Return at most 5 assets.
- Only use assets from the approved list.
- Do not invent assets.
- If no approved asset is materially affected, return an empty impacts array.
- Focus on expected price impact for each asset.
- Prefer the most directly affected assets.
- Avoid selecting more than 3 assets unless the article clearly impacts multiple markets.
- Do not score broad market tone unless it directly affects one of the approved assets.
- For oil-related geopolitical or supply news, score Brent Oil and Crude Oil separately only when both are directly affected.
- Use neutral only when the direction is genuinely unclear.
- Keep reasoning to one short sentence.

Return ONLY valid JSON in this format:

{{
  "impacts": [
    {{
      "asset": "...",
      "asset_type": "commodity",
      "direction": "bullish",
      "sentiment_score": 0.0,
      "confidence": 0.0,
      "reasoning": "..."
    }}
  ]
}}

Valid asset_type values:
- commodity
- currency

TITLE:
{title}

SUMMARY:
{summary}
"""


def get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise ValueError("Missing DATABASE_URL")

    return database_url


def get_openai_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        raise ValueError("Missing OPENAI_API_KEY")

    return OpenAI(api_key=api_key)


def is_quota_or_rate_limit_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    message = str(exc).lower()

    return (
        status_code == 429
        or "rate limit" in message
        or "rate_limit" in message
        or "quota" in message
        or "insufficient_quota" in message
    )


def ensure_status_table() -> None:
    query = """
        CREATE TABLE IF NOT EXISTS news_sentiment_status (
            id BIGSERIAL PRIMARY KEY,
            article_id BIGINT NOT NULL REFERENCES news_articles(id) ON DELETE CASCADE,
            model TEXT NOT NULL,
            status TEXT NOT NULL,
            impacts_count INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(article_id, model)
        );
    """

    conn = psycopg2.connect(get_database_url())

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(query)
    finally:
        conn.close()


def get_unscored_articles():
    query = """
        SELECT
            na.id,
            na.title,
            COALESCE(na.summary, '') AS summary
        FROM news_articles na
        LEFT JOIN news_sentiment_status nss
            ON nss.article_id = na.id
           AND nss.model = %s
        WHERE
            nss.article_id IS NULL
            OR (
                nss.status != 'success'
                AND nss.attempts < %s
            )
        ORDER BY na.id;
    """

    conn = psycopg2.connect(get_database_url())

    try:
        with conn.cursor() as cur:
            cur.execute(query, (MODEL_NAME, MAX_FAILED_ATTEMPTS_PER_ARTICLE))
            articles = cur.fetchall()
    finally:
        conn.close()

    if MAX_ARTICLES_PER_RUN > 0:
        articles = articles[:MAX_ARTICLES_PER_RUN]

    return articles


def score_article_once(client: OpenAI, title: str, summary: str) -> dict[str, Any]:
    prompt = PROMPT_TEMPLATE.format(
        assets=", ".join(ALL_ASSETS),
        title=title,
        summary=summary,
    )

    response = client.responses.create(
        model=MODEL_NAME,
        input=prompt,
    )

    text = response.output_text.strip()

    return json.loads(text)


def score_article(client: OpenAI, title: str, summary: str) -> dict[str, Any]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return score_article_once(client, title, summary)

        except Exception as exc:
            if is_quota_or_rate_limit_error(exc):
                raise RuntimeError("openai_quota_or_rate_limit") from exc

            if attempt == MAX_RETRIES:
                raise

            wait_time = SLEEP_SECONDS * attempt
            print(f"    OpenAI error attempt {attempt}/{MAX_RETRIES}: {exc}")
            print(f"    Retrying in {wait_time:.1f}s")
            time.sleep(wait_time)

    raise RuntimeError("unreachable_score_article_state")


def normalize_impact(impact: dict[str, Any]) -> dict[str, Any] | None:
    asset = impact.get("asset")
    asset_type = impact.get("asset_type")
    direction = impact.get("direction")

    if asset not in ALL_ASSETS:
        return None

    if asset_type not in {"commodity", "currency"}:
        return None

    if direction not in {"bullish", "bearish", "neutral"}:
        return None

    sentiment_score = float(impact.get("sentiment_score", 0.0))
    confidence = float(impact.get("confidence", 0.0))

    sentiment_score = max(-1.0, min(1.0, sentiment_score))
    confidence = max(0.0, min(1.0, confidence))

    reasoning = str(impact.get("reasoning", ""))[:500]

    return {
        "asset": asset,
        "asset_type": asset_type,
        "direction": direction,
        "sentiment_score": sentiment_score,
        "confidence": confidence,
        "reasoning": reasoning,
    }


def normalize_impacts(raw_impacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    impacts = []

    for raw_impact in raw_impacts:
        impact = normalize_impact(raw_impact)

        if impact is not None:
            impacts.append(impact)

    return impacts


def save_success(article_id: int, impacts: list[dict[str, Any]]) -> None:
    insert_impact_query = """
        INSERT INTO news_sentiment (
            article_id,
            asset,
            asset_type,
            direction,
            sentiment_score,
            confidence,
            reasoning,
            model
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (article_id, asset, model)
        DO NOTHING;
    """

    upsert_status_query = """
        INSERT INTO news_sentiment_status (
            article_id,
            model,
            status,
            impacts_count,
            attempts,
            error,
            updated_at_utc
        )
        VALUES (%s,%s,'success',%s,1,NULL,NOW())
        ON CONFLICT (article_id, model)
        DO UPDATE SET
            status = 'success',
            impacts_count = EXCLUDED.impacts_count,
            attempts = news_sentiment_status.attempts + 1,
            error = NULL,
            updated_at_utc = NOW();
    """

    conn = psycopg2.connect(get_database_url())

    try:
        with conn:
            with conn.cursor() as cur:
                for impact in impacts:
                    cur.execute(
                        insert_impact_query,
                        (
                            article_id,
                            impact["asset"],
                            impact["asset_type"],
                            impact["direction"],
                            impact["sentiment_score"],
                            impact["confidence"],
                            impact["reasoning"],
                            MODEL_NAME,
                        ),
                    )

                cur.execute(
                    upsert_status_query,
                    (article_id, MODEL_NAME, len(impacts)),
                )
    finally:
        conn.close()


def save_failure(article_id: int, error: str) -> None:
    upsert_status_query = """
        INSERT INTO news_sentiment_status (
            article_id,
            model,
            status,
            impacts_count,
            attempts,
            error,
            updated_at_utc
        )
        VALUES (%s,%s,'failed',0,1,%s,NOW())
        ON CONFLICT (article_id, model)
        DO UPDATE SET
            status = 'failed',
            attempts = news_sentiment_status.attempts + 1,
            error = EXCLUDED.error,
            updated_at_utc = NOW();
    """

    conn = psycopg2.connect(get_database_url())

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(upsert_status_query, (article_id, MODEL_NAME, error[:1000]))
    finally:
        conn.close()


def append_failure_log(article_id: int, title: str, error: str) -> None:
    file_exists = FAILED_LOG_PATH.exists()

    with FAILED_LOG_PATH.open("a", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)

        if not file_exists:
            writer.writerow(["article_id", "title", "model", "error"])

        writer.writerow([article_id, title, MODEL_NAME, error[:1000]])


def main():
    ensure_status_table()

    client = get_openai_client()
    articles = get_unscored_articles()

    print(f"Found {len(articles)} articles to score")
    print(f"Model: {MODEL_NAME}")

    if MAX_ARTICLES_PER_RUN > 0:
        print(f"MAX_ARTICLES_PER_RUN={MAX_ARTICLES_PER_RUN}")

    scored = 0
    failed = 0

    for article_id, title, summary in articles:
        print()
        print(f"Scoring article {article_id}")
        print(title)

        try:
            result = score_article(
                client=client,
                title=title,
                summary=summary,
            )

            raw_impacts = result.get("impacts", [])

            if not isinstance(raw_impacts, list):
                raise ValueError("Model response field 'impacts' is not a list")

            impacts = normalize_impacts(raw_impacts)

            save_success(article_id, impacts)

            scored += 1
            print(f"Inserted {len(impacts)} impacts")

            time.sleep(SLEEP_SECONDS)

        except RuntimeError as exc:
            if str(exc) == "openai_quota_or_rate_limit":
                print("OpenAI quota/rate limit hit. Stopping safely.")
                print("Already-scored articles have been saved.")
                break

            failed += 1
            error = str(exc)
            print(f"ERROR article {article_id}: {error}")
            save_failure(article_id, error)
            append_failure_log(article_id, title, error)

        except Exception as exc:
            failed += 1
            error = str(exc)
            print(f"ERROR article {article_id}: {error}")
            save_failure(article_id, error)
            append_failure_log(article_id, title, error)

    print()
    print("Done.")
    print(f"Scored successfully: {scored}")
    print(f"Failed: {failed}")


if __name__ == "__main__":
    main()