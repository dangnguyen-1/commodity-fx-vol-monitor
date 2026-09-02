"""Read-only API over the collected commodity, FX, trade and news data.

Serves the dashboard. Three routes carry everything it draws: /market-data
for prices, /trade-data for Comtrade flows, /news/latest for classified
headlines. /health reports per-source freshness.

Bound to localhost. The dashboard calls it server-side, so it is never
exposed publicly.

Run:  uvicorn api.app:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

from typing import Annotated, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.database import (
    DatabaseUnavailable,
    read_connection,
    rows_to_dicts,
)


load_dotenv()

API_VERSION = "0.2.0"

app = FastAPI(
    title="Commodity-FX Volatility Monitor API",
    version=API_VERSION,
    description=(
        "Read-only access to collected market prices, UN Comtrade flows "
        "and classified commodity news."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost",
        "http://127.0.0.1",
    ],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.exception_handler(DatabaseUnavailable)
def database_unavailable_handler(
    _request,
    exc: DatabaseUnavailable,
) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": str(exc), "status": "unavailable"},
    )


@app.get("/", tags=["system"])
def root() -> dict:
    return {
        "name": "Commodity-FX Volatility Monitor API",
        "version": API_VERSION,
        "routes": ["/health", "/market-data", "/trade-data", "/news/latest"],
    }


@app.get("/health", tags=["system"])
def health() -> dict:
    """Freshness of each collected dataset.

    Reports the newest row per source rather than a bare "ok", because the
    failure that matters here is not the API being down, it is the API
    cheerfully serving data that stopped updating hours ago.
    """
    with read_connection() as cursor:
        cursor.execute(
            """
            SELECT
                (SELECT MAX(datetime_utc) FROM market_data
                  WHERE timeframe = '1')   AS newest_minute_bar,
                (SELECT MAX(datetime_utc) FROM market_data
                  WHERE timeframe = '1D')  AS newest_daily_bar,
                (SELECT MAX(period) FROM fundamental_trade_data)
                                           AS newest_trade_period,
                (SELECT MAX(published) FROM news_articles)
                                           AS newest_article,
                (SELECT COUNT(*) FROM news_sentiment)
                                           AS classified_articles
            """
        )
        row = dict(cursor.fetchone() or {})

    return {"status": "ok", **row}


@app.get("/market-data", tags=["market"])
def market_data(
    symbols: str,
    timeframe: str = "1D",
    lookback_days: Annotated[int, Query(ge=1, le=6000)] = 400,
) -> dict:
    """OHLCV bars from the TradingView feed.

    `symbols` is comma-separated, e.g. "NYMEX:CL1!,DERIVED:CADUSD". FX
    inverse pairs are precomputed by generate_fx_inverses.py under
    "DERIVED:<QUOTE><BASE>" rather than inverted on the fly here.
    """
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    if not symbol_list:
        raise HTTPException(status_code=400, detail="symbols is required")

    with read_connection() as cursor:
        cursor.execute(
            """
            SELECT symbol, datetime_utc, open, high, low, close, volume
            FROM market_data
            WHERE symbol = ANY(%s)
              AND timeframe = %s
              AND datetime_utc >= NOW() - (%s || ' days')::interval
            ORDER BY symbol, datetime_utc ASC
            """,
            (symbol_list, timeframe, lookback_days),
        )
        items = rows_to_dicts(cursor.fetchall())

    return {
        "symbols": symbol_list,
        "timeframe": timeframe,
        "lookback_days": lookback_days,
        "count": len(items),
        "items": items,
    }


@app.get("/trade-data", tags=["fundamentals"])
def trade_data(
    commodity: str,
    period_start: str | None = None,
    period_end: str | None = None,
) -> dict:
    """Per-country monthly export, import and net USD for one commodity.

    `period` is "YYYYMM", compared as text because that is how the column
    is stored and it sorts correctly in that format.
    """
    with read_connection() as cursor:
        cursor.execute(
            """
            SELECT country, commodity, period, exports_usd, imports_usd,
                   net_usd
            FROM fundamental_trade_data
            WHERE commodity = %s
              AND (%s::text IS NULL OR period >= %s)
              AND (%s::text IS NULL OR period <= %s)
            ORDER BY period DESC, country
            """,
            (commodity, period_start, period_start, period_end, period_end),
        )
        items = rows_to_dicts(cursor.fetchall())

    return {
        "commodity": commodity,
        "period_start": period_start,
        "period_end": period_end,
        "count": len(items),
        "items": items,
    }


@app.get("/news/latest", tags=["news"])
def latest_news(
    asset: str | None = None,
    asset_type: Literal["commodity", "currency"] | None = None,
    min_confidence: Annotated[float, Query(ge=0, le=1)] = 0.0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict:
    """Classified headlines for one asset, newest first.

    Column aliases are load-bearing: dashboard/data/news.py reads
    `headline`, `source_name`, `publication_timestamp_utc` and `sentiment`,
    while Postgres stores those as title, source, published and
    sentiment_score.
    """
    with read_connection() as cursor:
        # One row per (article, asset), newest classification wins.
        #
        # The model is part of news_sentiment's identity, so an article
        # classified under two models has a row for each. Without DISTINCT
        # ON the same headline renders twice with different sentiments.
        cursor.execute(
            """
            SELECT * FROM (
                SELECT DISTINCT ON (ns.article_id, ns.asset)
                    ns.id                AS classification_id,
                    ns.asset,
                    ns.asset_type,
                    ns.direction,
                    ns.sentiment_score   AS sentiment,
                    ns.confidence,
                    ns.reasoning,
                    ns.model             AS model_name,
                    a.id                 AS article_id,
                    a.source             AS source_name,
                    a.title              AS headline,
                    a.url,
                    a.published          AS publication_timestamp_utc,
                    a.received_at_utc    AS retrieval_timestamp_utc
                FROM news_sentiment ns
                JOIN news_articles a ON a.id = ns.article_id
                WHERE (%s::text IS NULL OR ns.asset = %s)
                  AND (%s::text IS NULL OR ns.asset_type = %s)
                  AND COALESCE(ns.confidence, 0) >= %s
                ORDER BY ns.article_id, ns.asset, ns.created_at_utc DESC
            ) latest
            ORDER BY publication_timestamp_utc DESC NULLS LAST
            LIMIT %s
            """,
            (
                asset,
                asset,
                asset_type,
                asset_type,
                min_confidence,
                limit,
            ),
        )
        items = rows_to_dicts(cursor.fetchall())

    return {
        "asset_filter": asset,
        "asset_type_filter": asset_type,
        "min_confidence": min_confidence,
        "count": len(items),
        "items": items,
    }
