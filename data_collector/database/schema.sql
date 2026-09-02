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


-- Per-article classification bookkeeping: which articles the sentiment
-- model has already looked at, and how that went. Mirrors
-- ensure_status_table() in news_sentiment.py, which creates this lazily on
-- its first run. It belongs here too because paper_trading/news_data/
-- sync_news.py SELECTs from it, and news-sync runs on a 5-minute cron that
-- can easily fire before the sentiment stream's first pass on a freshly
-- deployed database — leaving sync_news failing on a missing relation
-- until the two happen to interleave the right way. Both definitions are
-- CREATE TABLE IF NOT EXISTS, so whichever runs first wins and the other
-- is a no-op.
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

-- Per-call OpenAI token usage, so call volume can be tracked from this
-- system rather than only from the provider's dashboard.
--
-- Volume, not cost. Spend is bounded by the daily call cap and read off the
-- provider's invoice, which is authoritative; an estimate computed here
-- could only ever disagree with it.
CREATE TABLE IF NOT EXISTS openai_usage (
    id BIGSERIAL PRIMARY KEY,

    model TEXT NOT NULL,
    request_type TEXT NOT NULL DEFAULT 'classification',

    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,

    success BOOLEAN NOT NULL DEFAULT TRUE,
    error TEXT,

    created_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_openai_usage_created
ON openai_usage (created_at_utc);
