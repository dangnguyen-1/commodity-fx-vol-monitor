"""
Yahoo Finance data fetcher using the yfinance library.
No API key required. Uses continuous front-month futures tickers (e.g. CL=F).
"""

from __future__ import annotations

import logging
import threading
from datetime import date, timedelta

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# yfinance is not safe to call from multiple threads at once — concurrent
# yf.download() calls (e.g. two Dash callbacks lazily fetching FX data on
# the same tab switch) have been observed to corrupt each other's result
# shape ("Data must be 1-dimensional, got ndarray of shape (N, 2)"). Dash's
# dev server runs callbacks on separate threads, so every fetch funnels
# through this one lock.
_YFINANCE_LOCK = threading.Lock()


def fetch_prices(tickers: list[str], lookback_days: int = 365) -> pd.DataFrame:
    """
    Fetch daily close prices from Yahoo Finance.
    Fetches each ticker individually so a single failure doesn't drop the rest.
    Returns a DataFrame indexed by date with one column per ticker.
    """
    end = date.today()
    start = end - timedelta(days=lookback_days)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    frames: dict[str, pd.Series] = {}
    for ticker in tickers:
        try:
            with _YFINANCE_LOCK:
                raw = yf.download(
                    ticker,
                    start=start_str,
                    end=end_str,
                    auto_adjust=True,
                    progress=False,
                )
            if raw.empty:
                logger.warning("No data returned for %s", ticker)
                continue
            close = raw["Close"]
            if hasattr(close, "squeeze"):
                close = close.squeeze()
            frames[ticker] = close
        except Exception as exc:
            logger.warning("Failed to fetch %s: %s", ticker, exc)

    if not frames:
        raise RuntimeError("Yahoo Finance returned no data for any ticker.")

    df = pd.DataFrame(frames)
    df = df.ffill().dropna(how="all")
    df.index = pd.to_datetime(df.index)
    df.index.name = "date"
    return df
