import os
from pathlib import Path

import pandas as pd
import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

load_dotenv()

DEFAULT_INPUT_FILE = Path(
    "data_collector/fundamental_data/output/trade_data_backfill_long.csv"
)

# One-row-at-a-time INSERTs over ~155k rows means ~155k network round
# trips in a single long-lived transaction — against a remote serverless
# Postgres instance (Neon), that reliably exceeds the connection's idle
# timeout partway through, so the load silently never completes (this is
# exactly what was happening: the backfill fetch itself succeeded every
# time, but the DB never actually got the new data). Batching into
# multi-row upserts with periodic commits turns that into a few hundred
# round trips instead of 155,000.
BATCH_SIZE = 5000


def get_input_file() -> Path:
    configured = os.getenv("COMTRADE_INPUT_FILE", "").strip()
    return Path(configured) if configured else DEFAULT_INPUT_FILE


def get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise ValueError("Missing DATABASE_URL in .env")
    return database_url


def clean_value(value):
    return None if pd.isna(value) else value


def load_trade_data() -> None:
    input_file = get_input_file()
    if not input_file.exists():
        raise FileNotFoundError(f"Missing input file: {input_file}")

    df = pd.read_csv(
        input_file,
        dtype={"country": str, "commodity": str, "period": str},
    )

    required = {
        "country",
        "commodity",
        "period",
        "exports_usd",
        "imports_usd",
        "net_usd",
        "note",
    }
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError("Missing columns: " + ", ".join(missing))

    df = df.drop_duplicates(
        subset=["country", "commodity", "period"],
        keep="last",
    )

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
        VALUES %s
        ON CONFLICT (country, commodity, period)
        DO UPDATE SET
            exports_usd = EXCLUDED.exports_usd,
            imports_usd = EXCLUDED.imports_usd,
            net_usd = EXCLUDED.net_usd,
            note = EXCLUDED.note,
            provider = EXCLUDED.provider,
            received_at_utc = NOW();
    """

    rows = [
        (
            row["country"],
            row["commodity"],
            str(row["period"]),
            clean_value(row["exports_usd"]),
            clean_value(row["imports_usd"]),
            clean_value(row["net_usd"]),
            clean_value(row.get("note")),
            "un_comtrade",
        )
        for _, row in df.iterrows()
    ]

    conn = psycopg2.connect(get_database_url())
    try:
        with conn.cursor() as cur:
            for start in range(0, len(rows), BATCH_SIZE):
                batch = rows[start : start + BATCH_SIZE]
                execute_values(cur, query, batch)
                conn.commit()
                print(
                    f"  loaded {min(start + BATCH_SIZE, len(rows))}/{len(rows)} rows"
                )

        print(
            f"Loaded {len(df)} rows from {input_file} "
            "into fundamental_trade_data"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    load_trade_data()