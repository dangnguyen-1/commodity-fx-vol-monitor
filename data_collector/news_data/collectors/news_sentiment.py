import json
import os

import psycopg2
from dotenv import load_dotenv
from openai import OpenAI

from data_collector.news_data.config.assets import ALL_ASSETS

load_dotenv()

MODEL_NAME = "gpt-5.5"


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


def get_unscored_articles():
    query = """
        SELECT
            na.id,
            na.title,
            COALESCE(na.summary, '')
        FROM news_articles na
        WHERE NOT EXISTS (
            SELECT 1
            FROM news_sentiment ns
            WHERE ns.article_id = na.id
              AND ns.model = %s
        )
        ORDER BY na.id;
    """

    conn = psycopg2.connect(get_database_url())

    try:
        with conn.cursor() as cur:
            cur.execute(query, (MODEL_NAME,))
            return cur.fetchall()
    finally:
        conn.close()


def score_article(client: OpenAI, title: str, summary: str):
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


def insert_impacts(article_id: int, impacts: list):
    query = """
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

    conn = psycopg2.connect(get_database_url())

    try:
        with conn:
            with conn.cursor() as cur:
                for impact in impacts:
                    cur.execute(
                        query,
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
    finally:
        conn.close()


def main():
    client = get_openai_client()

    articles = get_unscored_articles()

    print(f"Found {len(articles)} articles to score")

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

            impacts = result.get("impacts", [])

            insert_impacts(article_id, impacts)

            print(f"Inserted {len(impacts)} impacts")

        except Exception as exc:
            print(f"ERROR article {article_id}: {exc}")


if __name__ == "__main__":
    main()