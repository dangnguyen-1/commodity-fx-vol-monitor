import os
from pathlib import Path

import pandas as pd
import psycopg2
from dotenv import load_dotenv

load_dotenv()

INPUT_FILE = Path("data_collector/fundamental_data/output/trade_data_long.csv")


def get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise ValueError("Missing DATABASE_URL in .env")

    return database_url


def clean_value(value):
    if pd.isna(value):
        return None
    return value


def load_trade_data() -> None:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"Missing input file: {INPUT_FILE}")

    df = pd.read_csv(INPUT_FILE)

    query = """
        INSERT INTO fundamental_trade_data (
            country,
            commodity,
            period,
            exports_usd,
            imports_usd,
            net_usd,
            note,
            provider
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (country, commodity, period)
        DO UPDATE SET
            exports_usd = EXCLUDED.exports_usd,
            imports_usd = EXCLUDED.imports_usd,
            net_usd = EXCLUDED.net_usd,
            note = EXCLUDED.note,
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
                            row["country"],
                            row["commodity"],
                            str(row["period"]),
                            clean_value(row["exports_usd"]),
                            clean_value(row["imports_usd"]),
                            clean_value(row["net_usd"]),
                            clean_value(row.get("note")),
                            "un_comtrade",
                        ),
                    )

        print(f"Loaded {len(df)} rows into fundamental_trade_data")

    finally:
        conn.close()


if __name__ == "__main__":
    load_trade_data()