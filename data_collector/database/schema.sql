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


CREATE TABLE IF NOT EXISTS fundamental_trade_data (
    id BIGSERIAL PRIMARY KEY,

    country TEXT NOT NULL,
    commodity TEXT NOT NULL,
    period TEXT NOT NULL,

    exports_usd DOUBLE PRECISION,
    imports_usd DOUBLE PRECISION,
    net_usd DOUBLE PRECISION,

    note TEXT,
    provider TEXT DEFAULT 'un_comtrade',
    received_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_fundamental_trade_unique
ON fundamental_trade_data(country, commodity, period);

CREATE INDEX IF NOT EXISTS idx_fundamental_trade_period
ON fundamental_trade_data(period);

CREATE INDEX IF NOT EXISTS idx_fundamental_trade_commodity
ON fundamental_trade_data(commodity);


CREATE TABLE IF NOT EXISTS news_articles (
    id BIGSERIAL PRIMARY KEY,

    source TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    published TIMESTAMPTZ,
    summary TEXT,

    received_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_news_source
ON news_articles(source);

CREATE INDEX IF NOT EXISTS idx_news_published
ON news_articles(published);


CREATE TABLE IF NOT EXISTS news_sentiment (
    id BIGSERIAL PRIMARY KEY,

    article_id BIGINT NOT NULL REFERENCES news_articles(id) ON DELETE CASCADE,

    asset TEXT NOT NULL,
    asset_type TEXT NOT NULL,

    direction TEXT NOT NULL,

    sentiment_score DOUBLE PRECISION NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,

    reasoning TEXT,

    model TEXT NOT NULL,

    created_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_news_sentiment_unique
ON news_sentiment(article_id, asset, model);

CREATE INDEX IF NOT EXISTS idx_news_sentiment_asset
ON news_sentiment(asset);

CREATE INDEX IF NOT EXISTS idx_news_sentiment_direction
ON news_sentiment(direction);