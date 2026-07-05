import os

import psycopg2
from dotenv import load_dotenv

load_dotenv()

INVERSE_PAIRS = {
    "FX:EURUSD": "DERIVED:USDEUR",
    "FX:GBPUSD": "DERIVED:USDGBP",
    "FX:NZDUSD": "DERIVED:USDNZD",
    "FX:AUDUSD": "DERIVED:USDAUD",
    "FX_IDC:BRLUSD": "DERIVED:USDBRL",
    "FX:EURGBP": "DERIVED:GBPEUR",
    "FX:EURJPY": "DERIVED:JPYEUR",
    "FX:EURCHF": "DERIVED:CHFEUR",
    "FX:EURCAD": "DERIVED:CADEUR",
    "FX:EURAUD": "DERIVED:AUDEUR",
    "FX:EURNZD": "DERIVED:NZDEUR",
    "FX:GBPJPY": "DERIVED:JPYGBP",
    "FX:GBPCHF": "DERIVED:CHFGBP",
    "FX:GBPCAD": "DERIVED:CADGBP",
    "FX:GBPAUD": "DERIVED:AUDGBP",
    "FX:GBPNZD": "DERIVED:NZDGBP",
    "FX:AUDJPY": "DERIVED:JPYAUD",
    "FX:AUDNZD": "DERIVED:NZDAUD",
    "FX:AUDCAD": "DERIVED:CADAUD",
    "FX:NZDJPY": "DERIVED:JPYNZD",
    "FX:NZDCAD": "DERIVED:CADNZD",
    "FX:CADJPY": "DERIVED:JPYCAD",
    "FX:CHFJPY": "DERIVED:JPYCHF",
    "FX:USDJPY": "DERIVED:JPYUSD",
    "FX:USDCHF": "DERIVED:CHFUSD",
    "FX:USDCAD": "DERIVED:CADUSD",
}


def get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise ValueError("Missing DATABASE_URL in .env")

    return database_url


def generate_inverse_rows(conn, source_symbol: str, inverse_symbol: str) -> int:
    query = """
        INSERT INTO market_data (
            symbol,
            asset_class,
            timestamp,
            datetime_utc,
            open,
            high,
            low,
            close,
            volume,
            provider,
            timeframe,
            received_at_utc
        )
        SELECT
            %s AS symbol,
            'fx' AS asset_class,
            timestamp,
            datetime_utc,
            CASE WHEN open != 0 THEN 1.0 / open ELSE NULL END AS open,
            CASE WHEN low != 0 THEN 1.0 / low ELSE NULL END AS high,
            CASE WHEN high != 0 THEN 1.0 / high ELSE NULL END AS low,
            CASE WHEN close != 0 THEN 1.0 / close ELSE NULL END AS close,
            volume,
            'tradingview_derived' AS provider,
            timeframe,
            NOW() AS received_at_utc
        FROM market_data
        WHERE symbol = %s
          AND asset_class = 'fx'
          AND provider = 'tradingview'
        ON CONFLICT (symbol, timeframe, timestamp)
        DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume,
            provider = EXCLUDED.provider,
            received_at_utc = NOW()
        RETURNING id;
    """

    with conn.cursor() as cur:
        cur.execute(query, (inverse_symbol, source_symbol))
        return cur.rowcount


def main() -> None:
    conn = psycopg2.connect(get_database_url())

    try:
        with conn:
            total_rows = 0

            for source_symbol, inverse_symbol in INVERSE_PAIRS.items():
                rows = generate_inverse_rows(conn, source_symbol, inverse_symbol)
                total_rows += rows
                print(f"{source_symbol} -> {inverse_symbol}: {rows} rows")

            print(f"Generated {total_rows} inverse FX rows.")

    finally:
        conn.close()


if __name__ == "__main__":
    main()