PRAGMA foreign_keys = ON;


CREATE TABLE IF NOT EXISTS schema_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);


CREATE TABLE IF NOT EXISTS strategy_specs (
    spec_id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    specification_version TEXT NOT NULL,
    status TEXT NOT NULL,
    spec_path TEXT NOT NULL,
    spec_sha256 TEXT NOT NULL UNIQUE,
    spec_yaml TEXT NOT NULL,
    loaded_at_utc TEXT NOT NULL
);


CREATE TABLE IF NOT EXISTS paper_runs (
    run_id TEXT PRIMARY KEY,
    spec_id INTEGER NOT NULL,
    run_mode TEXT NOT NULL CHECK (
        run_mode IN (
            'local_replay',
            'shadow',
            'live_paper'
        )
    ),
    status TEXT NOT NULL CHECK (
        status IN (
            'created',
            'running',
            'paused',
            'stopped',
            'failed'
        )
    ),
    initial_equity_usd REAL NOT NULL CHECK (
        initial_equity_usd > 0
    ),
    started_at_utc TEXT NOT NULL,
    stopped_at_utc TEXT,
    notes TEXT,
    FOREIGN KEY (spec_id)
        REFERENCES strategy_specs(spec_id)
);


CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT PRIMARY KEY,
    instrument_name TEXT NOT NULL,
    instrument_type TEXT NOT NULL CHECK (
        instrument_type IN (
            'commodity',
            'fx'
        )
    ),
    venue TEXT,
    base_currency TEXT,
    quote_currency TEXT,
    timezone_name TEXT,
    source_name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (
        active IN (0, 1)
    ),
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);


CREATE TABLE IF NOT EXISTS relationships (
    relationship_id TEXT PRIMARY KEY,
    commodity TEXT NOT NULL,
    currency TEXT NOT NULL,
    commodity_symbol TEXT,
    fx_symbol TEXT NOT NULL,
    relationship_type TEXT NOT NULL,
    fx_direction_multiplier INTEGER CHECK (
        fx_direction_multiplier IN (-1, 1)
        OR fx_direction_multiplier IS NULL
    ),
    active INTEGER NOT NULL DEFAULT 1 CHECK (
        active IN (0, 1)
    ),
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (commodity_symbol)
        REFERENCES instruments(symbol),
    FOREIGN KEY (fx_symbol)
        REFERENCES instruments(symbol)
);


CREATE TABLE IF NOT EXISTS relationship_weights (
    selection_year INTEGER NOT NULL,
    relationship_id TEXT NOT NULL,
    selected INTEGER NOT NULL CHECK (
        selected IN (0, 1)
    ),
    selection_weight REAL NOT NULL CHECK (
        selection_weight >= 0
        AND selection_weight <= 1
    ),
    trailing_trades INTEGER NOT NULL DEFAULT 0 CHECK (
        trailing_trades >= 0
    ),
    trailing_net_return_pct REAL,
    trailing_profit_factor REAL,
    source_schedule_path TEXT NOT NULL,
    loaded_at_utc TEXT NOT NULL,

    PRIMARY KEY (
        selection_year,
        relationship_id
    ),

    FOREIGN KEY (relationship_id)
        REFERENCES relationships(relationship_id)
);


-- Fast-reacting supplement to relationship_weights: that table only
-- updates once a year from a 2-year rolling daily backtest, so a
-- relationship that starts failing live could keep trading at full
-- weight for up to 11 months before the next annual cycle catches it.
-- This tracks each relationship's own trailing live paper-trading
-- performance and can derate it (never above 1.0 — it can only ever
-- reduce exposure, not increase it beyond what the annual process
-- already decided) well before the next annual reweight. Stays at 1.0
-- (no effect) until a relationship has accumulated enough closed
-- trades to say anything statistically meaningful.
CREATE TABLE IF NOT EXISTS relationship_live_derate (
    relationship_id TEXT PRIMARY KEY,
    as_of_utc TEXT NOT NULL,
    window_days INTEGER NOT NULL,
    trailing_trades INTEGER NOT NULL DEFAULT 0 CHECK (
        trailing_trades >= 0
    ),
    trailing_net_pnl_usd REAL,
    trailing_profit_factor REAL,
    derate_multiplier REAL NOT NULL DEFAULT 1.0 CHECK (
        derate_multiplier >= 0
        AND derate_multiplier <= 1.0
    ),
    updated_at_utc TEXT NOT NULL,

    FOREIGN KEY (relationship_id)
        REFERENCES relationships(relationship_id)
);


CREATE TABLE IF NOT EXISTS market_bars_1m (
    bar_id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    bar_timestamp_utc TEXT NOT NULL,

    open_price REAL NOT NULL CHECK (
        open_price > 0
    ),
    high_price REAL NOT NULL CHECK (
        high_price > 0
    ),
    low_price REAL NOT NULL CHECK (
        low_price > 0
    ),
    close_price REAL NOT NULL CHECK (
        close_price > 0
    ),
    volume REAL,

    source_name TEXT NOT NULL,
    received_at_utc TEXT NOT NULL,

    is_complete INTEGER NOT NULL CHECK (
        is_complete IN (0, 1)
    ),

    raw_payload_json TEXT,

    UNIQUE (
        symbol,
        bar_timestamp_utc,
        source_name
    ),

    CHECK (
        high_price >= low_price
    ),

    FOREIGN KEY (symbol)
        REFERENCES instruments(symbol)
);


CREATE INDEX IF NOT EXISTS idx_market_bars_symbol_time
ON market_bars_1m (
    symbol,
    bar_timestamp_utc
);


CREATE TABLE IF NOT EXISTS market_quotes (
    quote_id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    quote_timestamp_utc TEXT NOT NULL,

    bid_price REAL NOT NULL CHECK (
        bid_price > 0
    ),
    ask_price REAL NOT NULL CHECK (
        ask_price > 0
    ),
    mid_price REAL NOT NULL CHECK (
        mid_price > 0
    ),

    spread_bps REAL NOT NULL CHECK (
        spread_bps >= 0
    ),

    source_name TEXT NOT NULL,
    received_at_utc TEXT NOT NULL,

    UNIQUE (
        symbol,
        quote_timestamp_utc,
        source_name
    ),

    CHECK (
        ask_price >= bid_price
    ),

    FOREIGN KEY (symbol)
        REFERENCES instruments(symbol)
);


CREATE INDEX IF NOT EXISTS idx_market_quotes_symbol_time
ON market_quotes (
    symbol,
    quote_timestamp_utc
);


CREATE TABLE IF NOT EXISTS news_articles (
    article_id TEXT PRIMARY KEY,

    source_name TEXT NOT NULL CHECK (
        source_name IN (
            'Reuters',
            'MarketWatch',
            'Investing.com'
        )
    ),

    url TEXT NOT NULL,
    canonical_url TEXT,
    headline TEXT NOT NULL,
    summary TEXT,

    publication_timestamp_utc TEXT NOT NULL,
    retrieval_timestamp_utc TEXT NOT NULL,

    deduplication_key TEXT NOT NULL UNIQUE,

    processing_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            processing_status IN (
                'pending',
                'classified',
                'irrelevant',
                'failed'
            )
        ),

    raw_payload_json TEXT,
    created_at_utc TEXT NOT NULL
);


CREATE INDEX IF NOT EXISTS idx_news_publication_time
ON news_articles (
    publication_timestamp_utc
);


CREATE INDEX IF NOT EXISTS idx_news_source_time
ON news_articles (
    source_name,
    publication_timestamp_utc
);


CREATE TABLE IF NOT EXISTS news_classifications (
    classification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id TEXT NOT NULL,

    provider TEXT NOT NULL,
    model_name TEXT NOT NULL,
    prompt_version TEXT NOT NULL,

    relevant INTEGER NOT NULL CHECK (
        relevant IN (0, 1)
    ),

    sentiment REAL NOT NULL CHECK (
        sentiment >= -1
        AND sentiment <= 1
    ),

    confidence REAL NOT NULL CHECK (
        confidence >= 0
        AND confidence <= 1
    ),

    event_type TEXT,
    commodities_json TEXT NOT NULL,
    raw_response_json TEXT,

    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (
        input_tokens >= 0
    ),

    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (
        output_tokens >= 0
    ),

    estimated_cost_usd REAL NOT NULL DEFAULT 0 CHECK (
        estimated_cost_usd >= 0
    ),

    classified_at_utc TEXT NOT NULL,

    UNIQUE (
        article_id,
        model_name,
        prompt_version
    ),

    FOREIGN KEY (article_id)
        REFERENCES news_articles(article_id)
        ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS news_classification_commodities (
    classification_id INTEGER NOT NULL,
    commodity TEXT NOT NULL,

    PRIMARY KEY (
        classification_id,
        commodity
    ),

    FOREIGN KEY (classification_id)
        REFERENCES news_classifications(
            classification_id
        )
        ON DELETE CASCADE
);


CREATE INDEX IF NOT EXISTS idx_news_commodity
ON news_classification_commodities (
    commodity
);


CREATE TABLE IF NOT EXISTS feature_snapshots (
    feature_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    spec_id INTEGER NOT NULL,
    relationship_id TEXT NOT NULL,
    feature_timestamp_utc TEXT NOT NULL,

    commodity_return_15m REAL,
    commodity_return_60m REAL,
    commodity_return_240m REAL,
    fx_return_15m REAL,
    realized_volatility_60m REAL,

    normalized_commodity_return_15m REAL,
    normalized_commodity_return_60m REAL,
    normalized_commodity_return_240m REAL,
    normalized_fx_return_15m REAL,

    commodity_impulse REAL,
    news_impulse REAL,
    expected_fx_impulse REAL,
    observed_fx_impulse REAL,
    divergence_score REAL,

    relevant_news_count INTEGER NOT NULL DEFAULT 0 CHECK (
        relevant_news_count >= 0
    ),

    market_window_coverage_pct REAL NOT NULL CHECK (
        market_window_coverage_pct >= 0
        AND market_window_coverage_pct <= 100
    ),

    market_data_complete INTEGER NOT NULL CHECK (
        market_data_complete IN (0, 1)
    ),

    created_at_utc TEXT NOT NULL,

    UNIQUE (
        run_id,
        relationship_id,
        feature_timestamp_utc
    ),

    FOREIGN KEY (run_id)
        REFERENCES paper_runs(run_id),

    FOREIGN KEY (spec_id)
        REFERENCES strategy_specs(spec_id),

    FOREIGN KEY (relationship_id)
        REFERENCES relationships(relationship_id)
);


CREATE INDEX IF NOT EXISTS idx_features_run_time
ON feature_snapshots (
    run_id,
    feature_timestamp_utc
);


CREATE TABLE IF NOT EXISTS signal_decisions (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    spec_id INTEGER NOT NULL,
    feature_id INTEGER,
    relationship_id TEXT NOT NULL,
    decision_timestamp_utc TEXT NOT NULL,

    decision_type TEXT NOT NULL CHECK (
        decision_type IN (
            'enter_long',
            'enter_short',
            'hold',
            'exit',
            'reject',
            'no_action'
        )
    ),

    signal_mode TEXT CHECK (
        signal_mode IN (
            'confirmed',
            'divergence',
            'risk',
            'none'
        )
    ),

    signal_strength REAL,
    approved INTEGER NOT NULL CHECK (
        approved IN (0, 1)
    ),

    reason_code TEXT NOT NULL,
    reason_detail TEXT,
    decision_snapshot_json TEXT,

    created_at_utc TEXT NOT NULL,

    FOREIGN KEY (run_id)
        REFERENCES paper_runs(run_id),

    FOREIGN KEY (spec_id)
        REFERENCES strategy_specs(spec_id),

    FOREIGN KEY (feature_id)
        REFERENCES feature_snapshots(feature_id),

    FOREIGN KEY (relationship_id)
        REFERENCES relationships(relationship_id)
);


CREATE INDEX IF NOT EXISTS idx_decisions_run_time
ON signal_decisions (
    run_id,
    decision_timestamp_utc
);


CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    relationship_id TEXT NOT NULL,

    side TEXT NOT NULL CHECK (
        side IN (
            'buy',
            'sell'
        )
    ),

    order_action TEXT NOT NULL CHECK (
        order_action IN (
            'open',
            'close'
        )
    ),

    order_type TEXT NOT NULL CHECK (
        order_type = 'simulated_market'
    ),

    notional_usd REAL NOT NULL CHECK (
        notional_usd > 0
    ),

    quantity REAL,
    signal_price REAL NOT NULL CHECK (
        signal_price > 0
    ),

    expected_round_trip_cost_bps REAL NOT NULL CHECK (
        expected_round_trip_cost_bps >= 0
    ),

    submitted_at_utc TEXT NOT NULL,

    status TEXT NOT NULL CHECK (
        status IN (
            'created',
            'submitted',
            'filled',
            'cancelled',
            'rejected'
        )
    ),

    rejection_reason TEXT,

    FOREIGN KEY (run_id)
        REFERENCES paper_runs(run_id),

    FOREIGN KEY (decision_id)
        REFERENCES signal_decisions(decision_id),

    FOREIGN KEY (relationship_id)
        REFERENCES relationships(relationship_id)
);


CREATE INDEX IF NOT EXISTS idx_orders_run_time
ON orders (
    run_id,
    submitted_at_utc
);


CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,

    fill_timestamp_utc TEXT NOT NULL,
    fill_price REAL NOT NULL CHECK (
        fill_price > 0
    ),

    filled_notional_usd REAL NOT NULL CHECK (
        filled_notional_usd > 0
    ),

    bid_price REAL,
    ask_price REAL,

    spread_cost_usd REAL NOT NULL DEFAULT 0 CHECK (
        spread_cost_usd >= 0
    ),

    slippage_cost_usd REAL NOT NULL DEFAULT 0 CHECK (
        slippage_cost_usd >= 0
    ),

    total_transaction_cost_usd REAL NOT NULL DEFAULT 0
        CHECK (
            total_transaction_cost_usd >= 0
        ),

    fill_source TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,

    FOREIGN KEY (order_id)
        REFERENCES orders(order_id)
);


CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    relationship_id TEXT NOT NULL,

    direction INTEGER NOT NULL CHECK (
        direction IN (-1, 1)
    ),

    status TEXT NOT NULL CHECK (
        status IN (
            'open',
            'closed'
        )
    ),

    opened_at_utc TEXT NOT NULL,
    entry_fill_id TEXT NOT NULL,

    entry_price REAL NOT NULL CHECK (
        entry_price > 0
    ),

    position_size_usd REAL NOT NULL CHECK (
        position_size_usd > 0
    ),

    closed_at_utc TEXT,
    exit_fill_id TEXT,
    exit_price REAL,

    gross_pnl_usd REAL,
    transaction_cost_usd REAL,
    net_pnl_usd REAL,
    exit_reason TEXT,

    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,

    FOREIGN KEY (run_id)
        REFERENCES paper_runs(run_id),

    FOREIGN KEY (relationship_id)
        REFERENCES relationships(relationship_id),

    FOREIGN KEY (entry_fill_id)
        REFERENCES fills(fill_id),

    FOREIGN KEY (exit_fill_id)
        REFERENCES fills(fill_id)
);


CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_position
ON positions (
    run_id,
    relationship_id
)
WHERE status = 'open';


CREATE TABLE IF NOT EXISTS position_marks (
    mark_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    position_id TEXT NOT NULL,
    mark_timestamp_utc TEXT NOT NULL,

    mark_price REAL NOT NULL CHECK (
        mark_price > 0
    ),

    unrealized_pnl_usd REAL NOT NULL,
    mark_source TEXT NOT NULL,

    UNIQUE (
        run_id,
        position_id,
        mark_timestamp_utc
    ),

    FOREIGN KEY (run_id)
        REFERENCES paper_runs(run_id),

    FOREIGN KEY (position_id)
        REFERENCES positions(position_id)
        ON DELETE CASCADE
);


CREATE INDEX IF NOT EXISTS idx_position_marks_time
ON position_marks (
    run_id,
    mark_timestamp_utc
);


CREATE TABLE IF NOT EXISTS equity_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    snapshot_timestamp_utc TEXT NOT NULL,

    cash_usd REAL NOT NULL,
    realized_pnl_usd REAL NOT NULL,
    unrealized_pnl_usd REAL NOT NULL,
    transaction_cost_usd REAL NOT NULL,

    total_equity_usd REAL NOT NULL,
    gross_exposure_usd REAL NOT NULL,
    net_exposure_usd REAL NOT NULL,
    drawdown_pct REAL NOT NULL,

    open_positions INTEGER NOT NULL CHECK (
        open_positions >= 0
    ),

    UNIQUE (
        run_id,
        snapshot_timestamp_utc
    ),

    FOREIGN KEY (run_id)
        REFERENCES paper_runs(run_id)
);


CREATE INDEX IF NOT EXISTS idx_equity_run_time
ON equity_snapshots (
    run_id,
    snapshot_timestamp_utc
);


CREATE TABLE IF NOT EXISTS api_usage (
    usage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    provider TEXT NOT NULL,
    model_name TEXT,
    request_type TEXT NOT NULL,
    request_timestamp_utc TEXT NOT NULL,

    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (
        input_tokens >= 0
    ),

    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (
        output_tokens >= 0
    ),

    estimated_cost_usd REAL NOT NULL DEFAULT 0 CHECK (
        estimated_cost_usd >= 0
    ),

    request_id TEXT,
    success INTEGER NOT NULL CHECK (
        success IN (0, 1)
    ),

    error_message TEXT,

    FOREIGN KEY (run_id)
        REFERENCES paper_runs(run_id)
);


CREATE INDEX IF NOT EXISTS idx_api_usage_time
ON api_usage (
    provider,
    request_timestamp_utc
);


CREATE TABLE IF NOT EXISTS service_heartbeats (
    service_name TEXT PRIMARY KEY,

    status TEXT NOT NULL CHECK (
        status IN (
            'starting',
            'healthy',
            'degraded',
            'offline',
            'failed'
        )
    ),

    last_heartbeat_utc TEXT NOT NULL,
    details_json TEXT,
    updated_at_utc TEXT NOT NULL
);


CREATE TABLE IF NOT EXISTS system_alerts (
    alert_id TEXT PRIMARY KEY,
    run_id TEXT,

    alert_timestamp_utc TEXT NOT NULL,

    severity TEXT NOT NULL CHECK (
        severity IN (
            'info',
            'warning',
            'critical'
        )
    ),

    service_name TEXT,
    alert_type TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT,

    resolved INTEGER NOT NULL DEFAULT 0 CHECK (
        resolved IN (0, 1)
    ),

    resolved_at_utc TEXT,

    FOREIGN KEY (run_id)
        REFERENCES paper_runs(run_id)
);


CREATE INDEX IF NOT EXISTS idx_alerts_unresolved
ON system_alerts (
    resolved,
    severity,
    alert_timestamp_utc
);

CREATE TABLE IF NOT EXISTS live_instrument_registry (
    relationship_id TEXT PRIMARY KEY,

    live_commodity_symbol TEXT NOT NULL,
    commodity_venue TEXT NOT NULL,
    commodity_timezone TEXT NOT NULL,

    commodity_contract_mode TEXT NOT NULL CHECK (
        commodity_contract_mode IN (
            'provider_continuous',
            'proxy',
            'spot',
            'other'
        )
    ),

    live_fx_symbol TEXT NOT NULL,
    fx_venue TEXT NOT NULL,
    fx_timezone TEXT NOT NULL,

    fx_price_transform TEXT NOT NULL CHECK (
        fx_price_transform IN (
            'identity',
            'inverse'
        )
    ),

    fx_direction_multiplier INTEGER NOT NULL CHECK (
        fx_direction_multiplier IN (-1, 1)
    ),

    market_source_name TEXT NOT NULL,

    active INTEGER NOT NULL DEFAULT 1 CHECK (
        active IN (0, 1)
    ),

    notes TEXT,
    updated_at_utc TEXT NOT NULL,

    FOREIGN KEY (relationship_id)
        REFERENCES relationships(relationship_id),

    FOREIGN KEY (live_commodity_symbol)
        REFERENCES instruments(symbol),

    FOREIGN KEY (live_fx_symbol)
        REFERENCES instruments(symbol)
);


CREATE INDEX IF NOT EXISTS idx_live_registry_symbols
ON live_instrument_registry (
    live_commodity_symbol,
    live_fx_symbol
);

CREATE TABLE IF NOT EXISTS market_ingestion_state (
    source_name TEXT NOT NULL,
    source_symbol TEXT NOT NULL,
    target_symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,

    last_bar_timestamp_utc TEXT NOT NULL,
    rows_written INTEGER NOT NULL DEFAULT 0 CHECK (
        rows_written >= 0
    ),

    last_sync_at_utc TEXT NOT NULL,

    PRIMARY KEY (
        source_name,
        source_symbol,
        target_symbol,
        timeframe
    )
);

CREATE TABLE IF NOT EXISTS news_classification_assets (
    classification_id INTEGER NOT NULL,

    asset TEXT NOT NULL,

    asset_type TEXT NOT NULL CHECK (
        asset_type IN ('commodity', 'currency')
    ),

    direction TEXT NOT NULL CHECK (
        direction IN ('bullish', 'bearish', 'neutral')
    ),

    sentiment REAL NOT NULL CHECK (
        sentiment >= -1.0
        AND sentiment <= 1.0
    ),

    confidence REAL NOT NULL CHECK (
        confidence >= 0.0
        AND confidence <= 1.0
    ),

    reasoning TEXT,

    PRIMARY KEY (
        classification_id,
        asset
    ),

    FOREIGN KEY (classification_id)
        REFERENCES news_classifications(classification_id)
        ON DELETE CASCADE
);


CREATE INDEX IF NOT EXISTS idx_news_classification_asset
ON news_classification_assets (
    asset,
    asset_type
);


CREATE TABLE IF NOT EXISTS news_ingestion_state (
    source_name TEXT PRIMARY KEY,

    last_source_update_utc TEXT NOT NULL,
    last_sync_at_utc TEXT NOT NULL,

    articles_written INTEGER NOT NULL DEFAULT 0 CHECK (
        articles_written >= 0
    ),

    classifications_written INTEGER NOT NULL DEFAULT 0 CHECK (
        classifications_written >= 0
    )
);
