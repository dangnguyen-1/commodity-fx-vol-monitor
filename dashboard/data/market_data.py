"""
Market data fetcher — sourced from the paper-trading pipeline's live
TradingView feed (via its read-only API, see paper_trading/api/app.py's
/market-data route), blending the pre-aggregated daily-bar table (long
history) with raw one-minute ticks (today's price, always live) so
nothing here is ever stuck waiting on a scheduled daily rebuild. Every
commodity and currency the dashboard tracks now has a live TradingView
symbol (see config.py / data/fx.py) — Yahoo Finance is kept only as a
last-resort fallback if the pipeline itself is unreachable, not a
routine per-instrument gap-filler.
"""

from __future__ import annotations

import logging

import pandas as pd
import requests

from config import PIPELINE_API_BASE_URL
from data.yahoo import fetch_prices as _yahoo_fetch

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 10


def _normalize_to_daily(series: pd.Series) -> pd.Series:
    """Collapse to one row per calendar date, keeping the last value for
    that day. Necessary because different symbols publish their "daily"
    close at different times of day — futures markets close at a
    different UTC hour than FX pairs, and Yahoo's own daily bars land at
    yet another convention. Combining series on their raw timestamps
    directly (as this function used to) creates a spurious interleaved
    pattern once two differently-timed series are aligned: each one has
    to forward-fill across the other's timestamps, injecting artificial
    zero-return rows that silently dilute any correlation/beta computed
    from the combined series by an order of magnitude or more."""
    normalized = series.copy()
    normalized.index = pd.to_datetime(normalized.index).normalize()
    return normalized.groupby(level=0).last()


def _fetch_pipeline_prices(tv_symbols: list[str], lookback_days: int, timeframe: str = "1D") -> pd.DataFrame:
    """One batched call to the pipeline API for however many TradingView
    symbols are requested, at the given TradingView timeframe ("1D" for
    pre-aggregated daily bars, "1" for raw one-minute ticks). Returns a
    DataFrame indexed by date/timestamp with one column per symbol
    (close); empty on any failure (pipeline unreachable, or nothing came
    back) so the caller falls back cleanly rather than partially
    succeeding with a confusing partial frame."""
    if not tv_symbols:
        return pd.DataFrame()
    try:
        resp = requests.get(
            f"{PIPELINE_API_BASE_URL}/market-data",
            params={
                "symbols": ",".join(tv_symbols),
                "timeframe": timeframe,
                "lookback_days": lookback_days,
            },
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
    except Exception as exc:
        logger.warning("Pipeline market-data fetch failed (timeframe=%s): %s", timeframe, exc)
        return pd.DataFrame()

    if not items:
        return pd.DataFrame()

    df = pd.DataFrame(items)
    df["datetime_utc"] = pd.to_datetime(df["datetime_utc"], utc=True).dt.tz_localize(None)
    pivot = df.pivot_table(index="datetime_utc", columns="symbol", values="close", aggfunc="last")
    pivot.index.name = "date"
    return pivot


_RECENT_LIVE_DAYS = 3  # how far back to pull raw 1-minute ticks for "today's" price


def _fetch_recent_live_prices(tv_symbols: list[str]) -> pd.DataFrame:
    """Pulls raw one-minute bars for the last few days and collapses them
    to one row per calendar day (the latest tick of each day). The
    pre-aggregated "1D" table (_fetch_pipeline_prices) only gets rebuilt
    once a day by a scheduled job — this is what keeps "today's" price
    genuinely live in between those rebuilds, straight from the same
    1-minute feed tv-stream is always writing, rather than waiting on
    the next scheduled daily-bar run or falling back to a delayed
    third-party source."""
    return _normalize_columns_to_daily(
        _fetch_pipeline_prices(tv_symbols, _RECENT_LIVE_DAYS, timeframe="1")
    )


def _normalize_columns_to_daily(pivot: pd.DataFrame) -> pd.DataFrame:
    if pivot.empty:
        return pivot
    return pd.DataFrame({col: _normalize_to_daily(pivot[col].dropna()) for col in pivot.columns})


def fetch_prices(
    names_to_tv: dict[str, str | None],
    names_to_yahoo: dict[str, str],
    lookback_days: int = 365,
) -> pd.DataFrame:
    """
    Returns a DataFrame indexed by date with one column per name in
    names_to_yahoo (every instrument the dashboard tracks) — same shape
    data/yahoo.py's fetch_prices alone used to return, so nothing
    downstream needs to know which source a given column came from.

    For each name: try the pipeline's TradingView symbol first (skip
    entirely if names_to_tv has None — that instrument isn't in the
    TradingView collector at all); whatever isn't covered — pipeline
    unreachable, or that symbol has no rows — falls back to Yahoo
    Finance for just that name.
    """
    tv_symbols = [s for s in names_to_tv.values() if s]
    tv_pivot = _fetch_pipeline_prices(tv_symbols, lookback_days)
    tv_recent_pivot = _fetch_recent_live_prices(tv_symbols)
    tv_symbol_to_name = {v: k for k, v in names_to_tv.items() if v}

    result: dict[str, pd.Series] = {}
    for tv_symbol in set(tv_pivot.columns) | set(tv_recent_pivot.columns):
        name = tv_symbol_to_name.get(tv_symbol)
        if not name:
            continue
        daily = _normalize_to_daily(tv_pivot[tv_symbol].dropna()) if tv_symbol in tv_pivot.columns else pd.Series(dtype=float)
        recent = tv_recent_pivot[tv_symbol].dropna() if tv_symbol in tv_recent_pivot.columns else pd.Series(dtype=float)
        # recent (from live 1-minute ticks) wins on any day both cover,
        # since it's always at least as fresh as the daily-bar table and
        # is the only one of the two that has *today* before the next
        # scheduled daily-bar rebuild runs.
        combined = recent.combine_first(daily)
        if not combined.empty:
            result[name] = combined

    missing_names = [n for n in names_to_yahoo if n not in result]
    if missing_names:
        yahoo_tickers = [names_to_yahoo[n] for n in missing_names]
        try:
            yahoo_df = _yahoo_fetch(yahoo_tickers, lookback_days)
        except Exception as exc:
            logger.warning("Yahoo fallback fetch failed for %s: %s", missing_names, exc)
            yahoo_df = pd.DataFrame()
        yahoo_ticker_to_name = {names_to_yahoo[n]: n for n in missing_names}
        for ticker in yahoo_df.columns:
            name = yahoo_ticker_to_name.get(ticker)
            if name:
                result[name] = _normalize_to_daily(yahoo_df[ticker])

    if not result:
        raise RuntimeError("No price data available from either the pipeline or Yahoo Finance.")

    df = pd.DataFrame(result).sort_index()
    df = df.ffill().dropna(how="all")
    df.index.name = "date"
    return df
