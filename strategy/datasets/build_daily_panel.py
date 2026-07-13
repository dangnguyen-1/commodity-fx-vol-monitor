from __future__ import annotations

import argparse
import os
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2
from dotenv import load_dotenv

from strategy.config.asset_fx_mapping import CANDIDATE_ASSET_FX_MAPPINGS
from strategy.config.market_symbols import COMMODITY_MARKET_SYMBOLS


OUTPUT_PATH = Path("strategy/output/daily_research_panel.csv")
FX_PRICE_OUTPUT_PATH = Path("strategy/output/daily_fx_prices.csv")

GDELT_PROXY_PATH = Path(
    "data_collector/news_data/output/gdelt_daily_sentiment.csv"
)
FUNDAMENTAL_PATH = Path(
    "strategy/output/daily_fundamental_features.csv"
)

RETURN_WINDOWS = [1, 3, 5]

DERIVED_FX_SOURCES = {
    "DERIVED:CADUSD": "FX:USDCAD",
}

VALID_SENTIMENT_MODES = {"gdelt_proxy", "llm"}

DEFAULT_SENTIMENT_MODE = os.getenv(
    "SENTIMENT_MODE",
    "gdelt_proxy",
).strip().lower()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the daily market, sentiment, and fundamental "
            "research panel."
        )
    )
    parser.add_argument(
        "--sentiment-mode",
        choices=sorted(VALID_SENTIMENT_MODES),
        default=DEFAULT_SENTIMENT_MODE,
        help=(
            "gdelt_proxy for historical backtesting or llm for "
            "deployment/live collected sentiment."
        ),
    )
    return parser.parse_args()


def get_connection():
    load_dotenv(dotenv_path=".env")

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set in your environment.")

    return psycopg2.connect(database_url)


def load_market_data(conn, symbols):
    symbols = sorted(set(symbols))

    if not symbols:
        return pd.DataFrame(
            columns=[
                "symbol",
                "timeframe",
                "datetime_utc",
                "open",
                "high",
                "low",
                "close",
            ]
        )

    placeholders = ", ".join(["%s"] * len(symbols))

    query = f"""
        SELECT
            symbol,
            timeframe,
            datetime_utc,
            open,
            high,
            low,
            close
        FROM market_data
        WHERE symbol IN ({placeholders})
          AND timeframe IN ('1D', 'D')
          AND datetime_utc >= TIMESTAMPTZ '2010-01-01 00:00:00+00'
        ORDER BY symbol, datetime_utc;
    """

    return pd.read_sql_query(query, conn, params=symbols)


def build_daily_market_data(market_df):
    if market_df.empty:
        return pd.DataFrame(
            columns=[
                "symbol",
                "date",
                "open",
                "high",
                "low",
                "close",
                "return_1d",
                "return_3d",
                "return_5d",
            ]
        )

    market_df = market_df.copy()

    market_df["datetime_utc"] = pd.to_datetime(
        market_df["datetime_utc"],
        utc=True,
    )

    utc_timestamp = market_df["datetime_utc"]

    session_date = utc_timestamp.dt.normalize()

    # TradingView often timestamps FX and futures daily bars at the
    # evening opening time of the following trading session.
    evening_session_mask = utc_timestamp.dt.hour >= 20

    session_date = session_date.where(
        ~evening_session_mask,
        session_date + pd.Timedelta(days=1),
    )

    # Repair occasional exchange timestamps that still land on weekends.
    # Saturday observations are generally the preceding Friday session;
    # Sunday observations are generally the following Monday session.
    saturday_mask = session_date.dt.dayofweek == 5
    sunday_mask = session_date.dt.dayofweek == 6

    session_date = session_date.where(
        ~saturday_mask,
        session_date - pd.Timedelta(days=1),
    )

    session_date = session_date.where(
        ~sunday_mask,
        session_date + pd.Timedelta(days=1),
    )

    market_df["date"] = session_date.dt.date

    price_cols = ["open", "high", "low", "close"]

    for col in price_cols:
        market_df[col] = pd.to_numeric(
            market_df[col],
            errors="coerce",
        )

    market_df["timeframe_priority"] = (
        market_df["timeframe"]
        .map({"1D": 0, "D": 1})
        .fillna(2)
        .astype(int)
    )

    market_df = market_df.sort_values(
        [
            "symbol",
            "date",
            "timeframe_priority",
            "datetime_utc",
        ],
        ascending=[True, True, True, False],
    )

    duplicate_count = int(
        market_df.duplicated(
            subset=["symbol", "date"],
            keep=False,
        ).sum()
    )

    if duplicate_count > 0:
        print(
            "Duplicate daily market rows detected before deduplication: "
            f"{duplicate_count}"
        )

    daily = (
        market_df.drop_duplicates(
            subset=["symbol", "date"],
            keep="first",
        )
        [
            [
                "symbol",
                "date",
                "open",
                "high",
                "low",
                "close",
            ]
        ]
        .sort_values(["symbol", "date"])
        .reset_index(drop=True)
    )

    # Confirm that normalized session dates are Monday through Friday.
    daily_dates = pd.to_datetime(daily["date"])

    weekend_mask = daily_dates.dt.dayofweek >= 5

    if weekend_mask.any():
        bad_rows = daily.loc[
            weekend_mask,
            ["symbol", "date"],
        ]

        raise ValueError(
            "Weekend trading-session dates remain after normalization:\n"
            f"{bad_rows.head(20).to_string(index=False)}"
        )

    missing_ohlc = daily[price_cols].isna().any(axis=1)

    if missing_ohlc.any():
        bad_rows = daily.loc[
            missing_ohlc,
            ["symbol", "date", *price_cols],
        ]

        raise ValueError(
            "Daily market rows with missing OHLC values found:\n"
            f"{bad_rows.head(20).to_string(index=False)}"
        )

    invalid_ohlc = (
        (daily["high"] < daily["low"])
        | (daily["high"] < daily[["open", "close"]].max(axis=1))
        | (daily["low"] > daily[["open", "close"]].min(axis=1))
    )

    if invalid_ohlc.any():
        bad_rows = daily.loc[
            invalid_ohlc,
            ["symbol", "date", *price_cols],
        ]

        raise ValueError(
            "Invalid daily OHLC relationships found:\n"
            f"{bad_rows.head(20).to_string(index=False)}"
        )

    for window in RETURN_WINDOWS:
        daily[f"return_{window}d"] = (
            daily.groupby("symbol")["close"].pct_change(
                window,
                fill_method=None,
            )
        )

    return daily


def add_derived_fx_series(
    daily_market: pd.DataFrame,
) -> pd.DataFrame:
    if daily_market.empty:
        return daily_market

    output_parts = [daily_market]

    for derived_symbol, source_symbol in DERIVED_FX_SOURCES.items():
        source = daily_market.loc[
            daily_market["symbol"].eq(source_symbol)
        ].copy()

        if source.empty:
            print(
                f"Unable to create {derived_symbol}: "
                f"missing source {source_symbol}"
            )
            continue

        source = source.sort_values("date").reset_index(drop=True)

        price_columns = ["open", "high", "low", "close"]

        invalid_prices = (
            source[price_columns].isna().any(axis=1)
            | source[price_columns].le(0).any(axis=1)
        )

        if invalid_prices.any():
            bad = source.loc[
                invalid_prices,
                ["symbol", "date", *price_columns],
            ]

            raise ValueError(
                f"Invalid source prices for {derived_symbol}:\n"
                f"{bad.head(20).to_string(index=False)}"
            )

        derived = source.copy()

        derived["symbol"] = derived_symbol

        # CADUSD = 1 / USDCAD.
        #
        # Inverting OHLC requires swapping the source high and low:
        # derived high = 1 / source low
        # derived low  = 1 / source high
        derived["open"] = 1.0 / source["open"]
        derived["high"] = 1.0 / source["low"]
        derived["low"] = 1.0 / source["high"]
        derived["close"] = 1.0 / source["close"]

        for window in RETURN_WINDOWS:
            derived[f"return_{window}d"] = (
                derived["close"].pct_change(
                    window,
                    fill_method=None,
                )
            )

        output_parts.append(
            derived[daily_market.columns]
        )

    combined = (
        pd.concat(output_parts, ignore_index=True)
        .sort_values(["symbol", "date"])
        .reset_index(drop=True)
    )

    duplicate_rows = combined.duplicated(
        subset=["symbol", "date"],
        keep=False,
    )

    if duplicate_rows.any():
        bad = combined.loc[
            duplicate_rows,
            ["symbol", "date"],
        ]

        raise ValueError(
            "Duplicate market rows after adding derived FX series:\n"
            f"{bad.head(20).to_string(index=False)}"
        )

    return combined


def load_llm_sentiment_data(conn):
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


def build_daily_llm_sentiment(sentiment_df):
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
        sentiment_df["article_time"],
        utc=True,
        errors="coerce",
    )

    sentiment_df = sentiment_df[
        sentiment_df["article_time"].notna()
    ].copy()

    sentiment_df["date"] = sentiment_df[
        "article_time"
    ].dt.date

    sentiment_df["sentiment_score"] = pd.to_numeric(
        sentiment_df["sentiment_score"],
        errors="coerce",
    )

    sentiment_df["confidence"] = pd.to_numeric(
        sentiment_df["confidence"],
        errors="coerce",
    )

    def weighted_sentiment(group):
        valid = group["sentiment_score"].notna()

        scores = group.loc[valid, "sentiment_score"]
        weights = (
            group.loc[valid, "confidence"]
            .fillna(0.0)
            .clip(lower=0.0)
        )

        if scores.empty:
            score = np.nan
        elif weights.sum() == 0:
            score = scores.mean()
        else:
            score = (scores * weights).sum() / weights.sum()

        return pd.Series(
            {
                "sentiment_score_daily": score,
                "news_count": int(valid.sum()),
                "avg_confidence": (
                    group.loc[valid, "confidence"].mean()
                    if valid.any()
                    else np.nan
                ),
            }
        )

    return (
        sentiment_df.groupby(["asset", "date"])
        .apply(weighted_sentiment, include_groups=False)
        .reset_index()
    )


def build_relationship_panel(daily_market):
    panel_parts = []

    for mapping in CANDIDATE_ASSET_FX_MAPPINGS:
        commodity = mapping["commodity"]
        commodity_symbol = COMMODITY_MARKET_SYMBOLS[commodity]
        fx_symbol = mapping["fx_symbol"]

        commodity_daily = daily_market[
            daily_market["symbol"] == commodity_symbol
        ].copy()

        fx_daily = daily_market[
            daily_market["symbol"] == fx_symbol
        ].copy()

        if commodity_daily.empty or fx_daily.empty:
            continue

        commodity_daily = commodity_daily.rename(
            columns={
                "open": "commodity_open",
                "high": "commodity_high",
                "low": "commodity_low",
                "close": "commodity_close",
                "return_1d": "commodity_return_1d",
                "return_3d": "commodity_return_3d",
                "return_5d": "commodity_return_5d",
            }
        )

        fx_daily = fx_daily.rename(
            columns={
                "open": "fx_open",
                "high": "fx_high",
                "low": "fx_low",
                "close": "fx_close",
                "return_1d": "fx_return_1d",
                "return_3d": "fx_return_3d",
                "return_5d": "fx_return_5d",
            }
        )

        commodity_daily = commodity_daily.drop(columns=["symbol"])
        fx_daily = fx_daily.drop(columns=["symbol"])

        merged = commodity_daily.merge(
            fx_daily,
            on="date",
            how="inner",
        )

        if merged.empty:
            continue

        currency = mapping["currency"]

        merged["commodity"] = commodity
        merged["commodity_symbol"] = commodity_symbol
        merged["currency"] = currency
        merged["fx_symbol"] = fx_symbol
        merged["expected_sign"] = mapping["expected_sign"]
        merged["relationship_type"] = mapping[
            "relationship_type"
        ]
        merged["priority"] = mapping["priority"]

        merged["relationship_id"] = (
            commodity
            + "__"
            + currency
            + "__"
            + fx_symbol
        )

        panel_parts.append(merged)

    if not panel_parts:
        return pd.DataFrame()

    panel = pd.concat(panel_parts, ignore_index=True)

    front_cols = [
        "date",
        "relationship_id",
        "commodity",
        "commodity_symbol",
        "currency",
        "fx_symbol",
        "expected_sign",
        "relationship_type",
        "priority",
    ]

    remaining_cols = [
        col for col in panel.columns
        if col not in front_cols
    ]

    panel = panel[front_cols + remaining_cols]

    return panel.sort_values(
        ["date", "commodity", "currency"]
    ).reset_index(drop=True)


def load_gdelt_proxy(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"Missing historical GDELT proxy: {path}"
        )

    gdelt = pd.read_csv(path)

    required = {
        "date",
        "news_sentiment_raw",
        "news_sentiment_z",
        "news_article_count",
        "news_source_count",
        "normalized_source_count",
        "news_available",
        "days_since_news",
        "sentiment_provider",
    }

    missing = sorted(required - set(gdelt.columns))
    if missing:
        raise ValueError(
            "GDELT proxy is missing required columns: "
            + ", ".join(missing)
        )

    gdelt["date"] = pd.to_datetime(
        gdelt["date"],
        errors="coerce",
    ).dt.date

    gdelt = gdelt[gdelt["date"].notna()].copy()

    duplicate_dates = gdelt.duplicated(
        subset=["date"],
        keep=False,
    )

    if duplicate_dates.any():
        bad = gdelt.loc[
            duplicate_dates,
            ["date"],
        ]

        raise ValueError(
            "Duplicate dates in GDELT proxy:\n"
            f"{bad.head(20).to_string(index=False)}"
        )

    numeric_columns = [
        "news_sentiment_raw",
        "news_sentiment_z",
        "news_article_count",
        "news_source_count",
        "normalized_source_count",
        "news_available",
        "days_since_news",
    ]

    for column in numeric_columns:
        gdelt[column] = pd.to_numeric(
            gdelt[column],
            errors="coerce",
        )

    # Put the historical GDELT proxy on the same bounded interface used
    # by the deployment LLM relationship score.
    gdelt["relationship_sentiment_score"] = np.tanh(
        gdelt["news_sentiment_z"] / 1.5
    )

    gdelt["relationship_news_count"] = (
        gdelt["news_article_count"]
        .fillna(0)
        .astype(int)
    )

    gdelt["relationship_sentiment_confidence"] = (
        gdelt["normalized_source_count"]
        .fillna(0)
        .div(3.0)
        .clip(0.0, 1.0)
    )

    gdelt["relationship_sentiment_available"] = (
        gdelt["news_available"].eq(1)
        & gdelt["relationship_sentiment_score"].notna()
    ).astype(int)

    gdelt.loc[
        gdelt["relationship_sentiment_available"].eq(0),
        "relationship_sentiment_score",
    ] = np.nan

    return gdelt[
        [
            "date",
            "relationship_sentiment_score",
            "relationship_news_count",
            "relationship_sentiment_confidence",
            "relationship_sentiment_available",
            "sentiment_provider",
            "news_sentiment_raw",
            "news_sentiment_z",
            "news_source_count",
            "normalized_source_count",
            "days_since_news",
        ]
    ].rename(
        columns={
            "news_sentiment_raw": "gdelt_sentiment_raw",
            "news_sentiment_z": "gdelt_sentiment_z",
            "news_source_count": "gdelt_source_count",
            "normalized_source_count": (
                "gdelt_normalized_source_count"
            ),
            "days_since_news": "gdelt_days_since_news",
        }
    )


def attach_gdelt_sentiment(
    panel: pd.DataFrame,
    gdelt: pd.DataFrame,
) -> pd.DataFrame:
    if panel.empty:
        return panel

    panel = panel.merge(
        gdelt,
        on="date",
        how="left",
        validate="many_to_one",
    )

    count_columns = [
        "relationship_news_count",
        "gdelt_source_count",
        "gdelt_normalized_source_count",
    ]

    for column in count_columns:
        panel[column] = (
            pd.to_numeric(panel[column], errors="coerce")
            .fillna(0)
            .astype(int)
        )

    panel["relationship_sentiment_confidence"] = (
        pd.to_numeric(
            panel["relationship_sentiment_confidence"],
            errors="coerce",
        )
        .fillna(0.0)
        .clip(0.0, 1.0)
    )

    panel["relationship_sentiment_available"] = (
        pd.to_numeric(
            panel["relationship_sentiment_available"],
            errors="coerce",
        )
        .fillna(0)
        .astype(int)
    )

    panel["sentiment_provider"] = (
        panel["sentiment_provider"]
        .fillna("gdelt_proxy")
    )

    # Keep the legacy component columns without pretending that the GDELT
    # macro proxy is commodity- or currency-specific.
    panel["commodity_sentiment_score"] = np.nan
    panel["commodity_news_count"] = 0
    panel["commodity_sentiment_confidence"] = 0.0

    panel["currency_sentiment_score"] = np.nan
    panel["currency_news_count"] = 0
    panel["currency_sentiment_confidence"] = 0.0

    return panel


def attach_llm_sentiment(
    panel: pd.DataFrame,
    daily_sentiment: pd.DataFrame,
) -> pd.DataFrame:
    if panel.empty:
        return panel

    panel = panel.copy()

    commodity_sentiment = daily_sentiment.rename(
        columns={
            "asset": "commodity",
            "sentiment_score_daily": (
                "commodity_sentiment_score"
            ),
            "news_count": "commodity_news_count",
            "avg_confidence": (
                "commodity_sentiment_confidence"
            ),
        }
    )

    panel = panel.merge(
        commodity_sentiment,
        on=["commodity", "date"],
        how="left",
        validate="many_to_one",
    )

    currency_sentiment = daily_sentiment.rename(
        columns={
            "asset": "currency",
            "sentiment_score_daily": (
                "currency_sentiment_score"
            ),
            "news_count": "currency_news_count",
            "avg_confidence": (
                "currency_sentiment_confidence"
            ),
        }
    )

    panel = panel.merge(
        currency_sentiment,
        on=["currency", "date"],
        how="left",
        validate="many_to_one",
    )

    count_columns = [
        "commodity_news_count",
        "currency_news_count",
    ]

    for column in count_columns:
        panel[column] = (
            pd.to_numeric(panel[column], errors="coerce")
            .fillna(0)
            .astype(int)
        )

    confidence_columns = [
        "commodity_sentiment_confidence",
        "currency_sentiment_confidence",
    ]

    for column in confidence_columns:
        panel[column] = (
            pd.to_numeric(panel[column], errors="coerce")
            .fillna(0.0)
            .clip(0.0, 1.0)
        )

    score_columns = [
        "commodity_sentiment_score",
        "currency_sentiment_score",
    ]

    for column in score_columns:
        panel[column] = pd.to_numeric(
            panel[column],
            errors="coerce",
        )

    commodity_active = (
        panel["commodity_news_count"].gt(0)
        & panel["commodity_sentiment_score"].notna()
    )

    currency_active = (
        panel["currency_news_count"].gt(0)
        & panel["currency_sentiment_score"].notna()
    )

    commodity_aligned = (
        panel["expected_sign"]
        * panel["commodity_sentiment_score"]
    )

    currency_fx_sign = pd.Series(
        1.0,
        index=panel.index,
    )

    usd_inverse = (
        panel["currency"].eq("USD")
        & panel["fx_symbol"].eq("FX:EURUSD")
    )

    currency_fx_sign.loc[usd_inverse] = -1.0

    currency_aligned = (
        currency_fx_sign
        * panel["currency_sentiment_score"]
    )

    component_count = (
        commodity_active.astype(int)
        + currency_active.astype(int)
    )

    relationship_score_numerator = (
        commodity_aligned.where(commodity_active, 0.0)
        + currency_aligned.where(currency_active, 0.0)
    )

    panel["relationship_sentiment_score"] = (
        relationship_score_numerator
        / component_count.replace(0, np.nan)
    ).clip(-1.0, 1.0)

    panel["relationship_news_count"] = (
        panel["commodity_news_count"]
        + panel["currency_news_count"]
    )

    confidence_numerator = (
        panel["commodity_sentiment_confidence"]
        * panel["commodity_news_count"]
        + panel["currency_sentiment_confidence"]
        * panel["currency_news_count"]
    )

    panel["relationship_sentiment_confidence"] = (
        confidence_numerator
        / panel["relationship_news_count"].replace(0, np.nan)
    ).fillna(0.0)

    panel["relationship_sentiment_available"] = (
        component_count.gt(0)
    ).astype(int)

    panel["sentiment_provider"] = "llm"

    panel["gdelt_sentiment_raw"] = np.nan
    panel["gdelt_sentiment_z"] = np.nan
    panel["gdelt_source_count"] = 0
    panel["gdelt_normalized_source_count"] = 0
    panel["gdelt_days_since_news"] = pd.NA

    return panel


def load_fundamental_features(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"Missing daily fundamental features: {path}"
        )

    fundamentals = pd.read_csv(
        path,
        dtype={"period": str},
    )

    required = {
        "date",
        "relationship_id",
        "period",
        "report_available_date",
        "fundamental_layer_score",
        "fundamental_layer_direction",
        "fundamental_available",
    }

    missing = sorted(required - set(fundamentals.columns))
    if missing:
        raise ValueError(
            "Daily fundamental features are missing columns: "
            + ", ".join(missing)
        )

    fundamentals["date"] = pd.to_datetime(
        fundamentals["date"],
        errors="coerce",
    ).dt.date

    duplicate_rows = fundamentals.duplicated(
        subset=["relationship_id", "date"],
        keep=False,
    )

    if duplicate_rows.any():
        bad = fundamentals.loc[
            duplicate_rows,
            ["relationship_id", "date"],
        ]

        raise ValueError(
            "Duplicate relationship/date rows in fundamentals:\n"
            f"{bad.head(20).to_string(index=False)}"
        )

    fundamentals = fundamentals.rename(
        columns={"period": "fundamental_period"}
    )

    drop_static_columns = {
        "commodity",
        "currency",
        "fx_symbol",
    }

    keep_columns = [
        column
        for column in fundamentals.columns
        if column not in drop_static_columns
    ]

    return fundamentals[keep_columns]


def attach_fundamentals(
    panel: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    if panel.empty:
        return panel

    panel = panel.merge(
        fundamentals,
        on=["relationship_id", "date"],
        how="left",
        validate="one_to_one",
    )

    panel["has_fundamental_mapping"] = (
        panel["trade_country"].notna()
    ).astype(int)

    panel["fundamental_available"] = (
        pd.to_numeric(
            panel["fundamental_available"],
            errors="coerce",
        )
        .fillna(0)
        .astype(int)
    )

    panel["fundamental_layer_direction"] = (
        pd.to_numeric(
            panel["fundamental_layer_direction"],
            errors="coerce",
        )
        .fillna(0)
        .astype(int)
    )

    panel["fundamental_layer_score"] = pd.to_numeric(
        panel["fundamental_layer_score"],
        errors="coerce",
    )

    future_report = (
        panel["report_available_date"].notna()
        & (
            pd.to_datetime(panel["report_available_date"])
            > pd.to_datetime(panel["date"])
        )
    )

    if future_report.any():
        bad = panel.loc[
            future_report,
            [
                "date",
                "relationship_id",
                "fundamental_period",
                "report_available_date",
            ],
        ]

        raise ValueError(
            "Look-ahead detected in merged fundamentals:\n"
            f"{bad.head(20).to_string(index=False)}"
        )

    invalid_score = (
        panel["fundamental_layer_score"]
        .dropna()
        .abs()
        .gt(1.0)
    )

    if invalid_score.any():
        raise ValueError(
            "Merged fundamental scores fall outside [-1, 1]."
        )

    unavailable_with_score = (
        panel["fundamental_available"].eq(0)
        & panel["fundamental_layer_score"].notna()
    )

    if unavailable_with_score.any():
        raise ValueError(
            "Unavailable fundamental rows contain active scores."
        )

    return panel


def build_daily_fx_prices(
    daily_market: pd.DataFrame,
    required_fx_symbols: set[str],
) -> pd.DataFrame:
    daily_fx_prices = (
        daily_market[
            daily_market["symbol"].isin(required_fx_symbols)
        ]
        .rename(
            columns={
                "symbol": "fx_symbol",
                "open": "fx_open",
                "high": "fx_high",
                "low": "fx_low",
                "close": "fx_close",
            }
        )[
            [
                "date",
                "fx_symbol",
                "fx_open",
                "fx_high",
                "fx_low",
                "fx_close",
            ]
        ]
        .sort_values(["date", "fx_symbol"])
        .reset_index(drop=True)
    )

    duplicate_fx = daily_fx_prices.duplicated(
        subset=["date", "fx_symbol"],
        keep=False,
    )

    if duplicate_fx.any():
        bad = daily_fx_prices.loc[
            duplicate_fx,
            ["date", "fx_symbol"],
        ]

        raise ValueError(
            "Duplicate FX price rows found:\n"
            f"{bad.head(20).to_string(index=False)}"
        )

    return daily_fx_prices


def validate_panel(panel: pd.DataFrame) -> None:
    duplicate_panel = panel.duplicated(
        subset=["relationship_id", "date"],
        keep=False,
    )

    if duplicate_panel.any():
        bad = panel.loc[
            duplicate_panel,
            ["relationship_id", "date"],
        ]

        raise ValueError(
            "Duplicate relationship-panel rows found:\n"
            f"{bad.head(20).to_string(index=False)}"
        )

    invalid_sentiment = (
        panel["relationship_sentiment_score"]
        .dropna()
        .abs()
        .gt(1.0)
    )

    if invalid_sentiment.any():
        raise ValueError(
            "Relationship sentiment scores fall outside [-1, 1]."
        )

    unavailable_sentiment_with_score = (
        panel["relationship_sentiment_available"].eq(0)
        & panel["relationship_sentiment_score"].notna()
    )

    if unavailable_sentiment_with_score.any():
        raise ValueError(
            "Unavailable sentiment rows contain active scores."
        )


def main() -> None:
    args = parse_args()
    sentiment_mode = args.sentiment_mode

    required_market_symbols = set()
    required_fx_symbols = set()

    for mapping in CANDIDATE_ASSET_FX_MAPPINGS:
        commodity_symbol = COMMODITY_MARKET_SYMBOLS[
            mapping["commodity"]
        ]

        fx_symbol = mapping["fx_symbol"]

        source_fx_symbol = DERIVED_FX_SOURCES.get(
            fx_symbol,
            fx_symbol,
        )

        required_market_symbols.add(commodity_symbol)
        required_market_symbols.add(source_fx_symbol)

        # Keep the desired output symbol here. This includes
        # DERIVED:CADUSD rather than the raw FX:USDCAD source.
        required_fx_symbols.add(fx_symbol)

    with get_connection() as conn:
        market_df = load_market_data(
            conn,
            required_market_symbols,
        )

        if sentiment_mode == "llm":
            llm_sentiment_df = load_llm_sentiment_data(conn)
        else:
            llm_sentiment_df = pd.DataFrame()

    available_symbols = (
        set(market_df["symbol"].unique())
        if not market_df.empty
        else set()
    )

    missing_symbols = sorted(
        required_market_symbols - available_symbols
    )

    if missing_symbols:
        print("Missing market symbols:")
        for symbol in missing_symbols:
            print(f"- {symbol}")

    daily_market = build_daily_market_data(market_df)

    daily_market = add_derived_fx_series(
        daily_market
    )

    daily_fx_prices = build_daily_fx_prices(
        daily_market,
        required_fx_symbols,
    )

    panel = build_relationship_panel(daily_market)

    if sentiment_mode == "gdelt_proxy":
        gdelt = load_gdelt_proxy(GDELT_PROXY_PATH)
        panel = attach_gdelt_sentiment(panel, gdelt)
    elif sentiment_mode == "llm":
        daily_llm_sentiment = build_daily_llm_sentiment(
            llm_sentiment_df
        )
        panel = attach_llm_sentiment(
            panel,
            daily_llm_sentiment,
        )
    else:
        raise ValueError(
            f"Unsupported sentiment mode: {sentiment_mode}"
        )

    fundamentals = load_fundamental_features(
        FUNDAMENTAL_PATH
    )

    panel = attach_fundamentals(
        panel,
        fundamentals,
    )

    validate_panel(panel)

    FX_PRICE_OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    daily_fx_prices.to_csv(
        FX_PRICE_OUTPUT_PATH,
        index=False,
    )

    OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    panel.to_csv(
        OUTPUT_PATH,
        index=False,
    )

    print(
        f"Saved daily FX prices to {FX_PRICE_OUTPUT_PATH}"
    )
    print(f"FX price rows: {len(daily_fx_prices):,}")
    print(
        "FX symbols: "
        f"{daily_fx_prices['fx_symbol'].nunique()}"
    )

    print(
        f"\nSaved daily research panel to {OUTPUT_PATH}"
    )
    print(f"Sentiment mode: {sentiment_mode}")
    print(f"Rows: {len(panel):,}")
    print(
        "Relationships: "
        f"{panel['relationship_id'].nunique()}"
    )
    print(
        "Date range: "
        f"{panel['date'].min()} to {panel['date'].max()}"
    )
    print(f"Columns: {len(panel.columns)}")
    print(
        "Rows with active sentiment: "
        f"{int(panel['relationship_sentiment_available'].sum()):,}"
    )
    print(
        "Relationships with a fundamental mapping: "
        f"{panel.loc[panel['has_fundamental_mapping'].eq(1), 'relationship_id'].nunique()}"
    )
    print(
        "Rows with active fundamentals: "
        f"{int(panel['fundamental_available'].sum()):,}"
    )


if __name__ == "__main__":
    main()