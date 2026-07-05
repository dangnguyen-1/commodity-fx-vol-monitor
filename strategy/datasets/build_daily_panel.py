import os
from pathlib import Path

import pandas as pd
import psycopg2
from dotenv import load_dotenv

from strategy.config.asset_fx_mapping import CANDIDATE_ASSET_FX_MAPPINGS
from strategy.config.market_symbols import COMMODITY_MARKET_SYMBOLS


OUTPUT_PATH = Path("strategy/output/daily_research_panel.csv")
RETURN_WINDOWS = [1, 3, 5]


def get_connection():
    load_dotenv()

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set in your environment.")

    return psycopg2.connect(database_url)


def load_market_data(conn, symbols):
    symbols = sorted(set(symbols))

    if not symbols:
        return pd.DataFrame(columns=["symbol", "datetime_utc", "close"])

    placeholders = ", ".join(["%s"] * len(symbols))

    query = f"""
        SELECT
            symbol,
            datetime_utc,
            close
        FROM market_data
        WHERE symbol IN ({placeholders})
        ORDER BY symbol, datetime_utc;
    """

    return pd.read_sql_query(query, conn, params=symbols)


def build_daily_market_data(market_df):
    if market_df.empty:
        return pd.DataFrame(
            columns=[
                "symbol",
                "date",
                "close",
                "return_1d",
                "return_3d",
                "return_5d",
            ]
        )

    market_df = market_df.copy()
    market_df["datetime_utc"] = pd.to_datetime(market_df["datetime_utc"], utc=True)
    market_df["date"] = market_df["datetime_utc"].dt.date

    market_df = market_df.sort_values(["symbol", "datetime_utc"])

    daily = (
        market_df.groupby(["symbol", "date"], as_index=False)
        .agg(close=("close", "last"))
        .sort_values(["symbol", "date"])
    )

    for window in RETURN_WINDOWS:
        daily[f"return_{window}d"] = (
            daily.groupby("symbol")["close"].pct_change(window)
        )

    return daily


def load_sentiment_data(conn):
    query = """
        SELECT
            ns.asset,
            ns.asset_type,
            ns.direction,
            ns.sentiment_score,
            ns.confidence,
            COALESCE(na.published, na.received_at_utc) AS article_time
        FROM news_sentiment ns
        JOIN news_articles na
            ON ns.article_id = na.id;
    """

    return pd.read_sql_query(query, conn)


def build_daily_sentiment(sentiment_df):
    if sentiment_df.empty:
        return pd.DataFrame(
            columns=[
                "asset",
                "date",
                "sentiment_score_daily",
                "news_count",
                "avg_confidence",
            ]
        )

    sentiment_df = sentiment_df.copy()
    sentiment_df["article_time"] = pd.to_datetime(
        sentiment_df["article_time"], utc=True
    )
    sentiment_df["date"] = sentiment_df["article_time"].dt.date

    def weighted_sentiment(group):
        weights = group["confidence"].fillna(0)

        if weights.sum() == 0:
            score = group["sentiment_score"].mean()
        else:
            score = (group["sentiment_score"] * weights).sum() / weights.sum()

        return pd.Series(
            {
                "sentiment_score_daily": score,
                "news_count": len(group),
                "avg_confidence": group["confidence"].mean(),
            }
        )

    daily_sentiment = (
        sentiment_df.groupby(["asset", "date"])
        .apply(weighted_sentiment)
        .reset_index()
    )

    return daily_sentiment


def build_relationship_panel(daily_market):
    panel_parts = []

    for mapping in CANDIDATE_ASSET_FX_MAPPINGS:
        commodity = mapping["commodity"]
        commodity_symbol = COMMODITY_MARKET_SYMBOLS[commodity]
        fx_symbol = mapping["fx_symbol"]

        commodity_daily = daily_market[daily_market["symbol"] == commodity_symbol].copy()
        fx_daily = daily_market[daily_market["symbol"] == fx_symbol].copy()

        if commodity_daily.empty or fx_daily.empty:
            continue

        commodity_daily = commodity_daily.rename(
            columns={
                "close": "commodity_close",
                "return_1d": "commodity_return_1d",
                "return_3d": "commodity_return_3d",
                "return_5d": "commodity_return_5d",
            }
        )

        fx_daily = fx_daily.rename(
            columns={
                "close": "fx_close",
                "return_1d": "fx_return_1d",
                "return_3d": "fx_return_3d",
                "return_5d": "fx_return_5d",
            }
        )

        commodity_daily = commodity_daily.drop(columns=["symbol"])
        fx_daily = fx_daily.drop(columns=["symbol"])

        merged = commodity_daily.merge(fx_daily, on="date", how="inner")

        if merged.empty:
            continue

        merged["commodity"] = commodity
        merged["commodity_symbol"] = commodity_symbol
        merged["currency"] = mapping["currency"]
        merged["fx_symbol"] = fx_symbol
        merged["expected_sign"] = mapping["expected_sign"]
        merged["relationship_type"] = mapping["relationship_type"]
        merged["priority"] = mapping["priority"]

        panel_parts.append(merged)

    if not panel_parts:
        return pd.DataFrame()

    panel = pd.concat(panel_parts, ignore_index=True)

    front_cols = [
        "date",
        "commodity",
        "commodity_symbol",
        "currency",
        "fx_symbol",
        "expected_sign",
        "relationship_type",
        "priority",
    ]

    remaining_cols = [col for col in panel.columns if col not in front_cols]
    panel = panel[front_cols + remaining_cols]

    return panel.sort_values(["date", "commodity", "currency"]).reset_index(drop=True)


def attach_sentiment(panel, daily_sentiment):
    if panel.empty:
        return panel

    panel = panel.copy()

    commodity_sentiment = daily_sentiment.rename(
        columns={
            "asset": "commodity",
            "sentiment_score_daily": "commodity_sentiment_score",
            "news_count": "commodity_news_count",
            "avg_confidence": "commodity_sentiment_confidence",
        }
    )

    panel = panel.merge(
        commodity_sentiment,
        on=["commodity", "date"],
        how="left",
    )

    currency_sentiment = daily_sentiment.rename(
        columns={
            "asset": "currency",
            "sentiment_score_daily": "currency_sentiment_score",
            "news_count": "currency_news_count",
            "avg_confidence": "currency_sentiment_confidence",
        }
    )

    panel = panel.merge(
        currency_sentiment,
        on=["currency", "date"],
        how="left",
    )

    fill_zero_cols = [
        "commodity_sentiment_score",
        "commodity_news_count",
        "commodity_sentiment_confidence",
        "currency_sentiment_score",
        "currency_news_count",
        "currency_sentiment_confidence",
    ]

    for col in fill_zero_cols:
        if col in panel.columns:
            panel[col] = panel[col].fillna(0)

    return panel


def main():
    required_symbols = set()

    for mapping in CANDIDATE_ASSET_FX_MAPPINGS:
        required_symbols.add(COMMODITY_MARKET_SYMBOLS[mapping["commodity"]])
        required_symbols.add(mapping["fx_symbol"])

    with get_connection() as conn:
        market_df = load_market_data(conn, required_symbols)
        sentiment_df = load_sentiment_data(conn)

    available_symbols = set(market_df["symbol"].unique()) if not market_df.empty else set()
    missing_symbols = sorted(required_symbols - available_symbols)

    if missing_symbols:
        print("Missing market symbols:")
        for symbol in missing_symbols:
            print(f"- {symbol}")

    daily_market = build_daily_market_data(market_df)
    daily_sentiment = build_daily_sentiment(sentiment_df)

    panel = build_relationship_panel(daily_market)
    panel = attach_sentiment(panel, daily_sentiment)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    panel.to_csv(OUTPUT_PATH, index=False)

    print(f"Saved daily research panel to {OUTPUT_PATH}")
    print(f"Rows: {len(panel)}")
    print(f"Columns: {len(panel.columns) if not panel.empty else 0}")


if __name__ == "__main__":
    main()