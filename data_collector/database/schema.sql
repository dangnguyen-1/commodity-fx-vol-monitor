CREATE TABLE IF NOT EXISTS market_data (
    id BIGSERIAL PRIMARY KEY,

    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,

    timestamp BIGINT NOT NULL,
    datetime_utc TIMESTAMPTZ NOT NULL,

    open DOUBLE PRECISION NOT NULL,
    high DOUBLE PRECISION NOT NULL,
    low DOUBLE PRECISION NOT NULL,
    close DOUBLE PRECISION NOT NULL,

    volume DOUBLE PRECISION,

    provider TEXT,
    timeframe TEXT,

    received_at_utc TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_market_symbol_time
ON market_data(symbol, timestamp);

CREATE UNIQUE INDEX IF NOT EXISTS idx_market_unique_candle
ON market_data(symbol, timeframe, timestamp);