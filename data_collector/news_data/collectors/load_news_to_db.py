import os
from pathlib import Path

import pandas as pd
import psycopg2
from dotenv import load_dotenv

load_dotenv()

INPUT_FILE = Path("data_collector/news_data/output/news_articles.csv")


def get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise ValueError("Missing DATABASE_URL in .env")

    return database_url


def clean_value(value):
    if pd.isna(value):
        return None
    return value


def load_news_articles() -> None:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"Missing input file: {INPUT_FILE}")

    df = pd.read_csv(INPUT_FILE)

    query = """
        INSERT INTO news_articles (
            source,
            title,
            url,
            published,
            summary
        )
        VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT (url)
        DO UPDATE SET
            source = EXCLUDED.source,
            title = EXCLUDED.title,
            published = EXCLUDED.published,
            summary = EXCLUDED.summary,
            received_at_utc = NOW();
    """

    conn = psycopg2.connect(get_database_url())

    try:
        with conn:
            with conn.cursor() as cur:
                for _, row in df.iterrows():
                    cur.execute(
                        query,
                        (
                            row["source"],
                            row["title"],
                            row["url"],
                            clean_value(row["published"]),
                            clean_value(row["summary"]),
                        ),
                    )

        print(f"Loaded {len(df)} rows into news_articles")

    finally:
        conn.close()


if __name__ == "__main__":
    load_news_articles()