"""
Volatility and returns calculations on price DataFrames.
All inputs are DataFrames indexed by date with commodity names as columns.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def log_returns(prices: pd.DataFrame) -> pd.DataFrame:
    # how="all" (not the default "any"): a single sparse-history column
    # (e.g. a currency added to the collector only recently) must not
    # wipe out every other column's return for the same date. Only drop
    # a date when literally nothing has a price on it (e.g. a shared
    # market holiday) — anything else should surface as a per-column
    # NaN that rolling()/iloc[-1] already handle correctly on their own.
    return np.log(prices / prices.shift(1)).dropna(how="all")


def historical_volatility(
    prices: pd.DataFrame, windows: list[int] = (30, 60, 90)
) -> dict[int, pd.DataFrame]:
    """
    Annualized rolling historical volatility (%) for each window.
    Returns {window: DataFrame(date x commodity)}.
    """
    rets = log_returns(prices)
    return {
        w: rets.rolling(w).std() * np.sqrt(TRADING_DAYS) * 100
        for w in windows
    }


def price_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """
    Latest 1-day, 5-day, and 30-day percentage returns.
    Returns a DataFrame with index = commodity name and columns [1d, 5d, 30d].
    """
    pct = prices.pct_change()
    rows = {
        "1d":  pct.iloc[-1] * 100,
        "5d":  (prices.iloc[-1] / prices.iloc[-6] - 1) * 100
               if len(prices) >= 6 else pd.Series(np.nan, index=prices.columns),
        "30d": (prices.iloc[-1] / prices.iloc[-31] - 1) * 100
               if len(prices) >= 31 else pd.Series(np.nan, index=prices.columns),
    }
    return pd.DataFrame(rows).T  # columns = commodities, index = period


def moving_averages(prices: pd.DataFrame, windows: list[int] = (20, 50)) -> dict[int, pd.Series]:
    """Latest moving average price for each window."""
    return {w: prices.rolling(w).mean().iloc[-1] for w in windows}


def correlation_matrix(prices: pd.DataFrame, window: int = 60) -> pd.DataFrame:
    """Rolling correlation of log returns over the last `window` trading days."""
    rets = log_returns(prices)
    return rets.tail(window).corr()


def current_vol_summary(hv_dict: dict[int, pd.DataFrame]) -> pd.DataFrame:
    """
    Latest HV value for every window, returned as DataFrame:
      index = commodity, columns = [HV30, HV60, HV90, ...]
    """
    frames = {
        f"HV{w}": hv.iloc[-1].rename(f"HV{w}")
        for w, hv in hv_dict.items()
    }
    return pd.concat(frames.values(), axis=1)


def is_breaching(current_vol, threshold: float) -> bool:
    """Whether a commodity counts as breaching its volatility threshold.

    The single definition used by every surface that shows alert state:
    the banner, the board tiles, the Alerts table and its count. Each of
    those used to test the raw figure while displaying it rounded to one
    decimal, so Platinum at 35.04% against a 35.0% threshold lit up
    everywhere while every number on screen read 35.0% against 35.0%.
    Fixing only check_alerts left the banner disagreeing with the tiles.

    The test runs on the displayed value, so what a reader is shown always
    justifies what they are told.
    """
    if pd.isna(current_vol):
        return False
    return round(float(current_vol), 1) > threshold


def check_alerts(
    hv_dict: dict[int, pd.DataFrame],
    thresholds: dict[str, float],
    window: int = 30,
) -> list[dict]:
    """
    Return list of triggered alerts where current HV{window} > threshold.
    Each dict: {name, current_vol, threshold, excess}.

    The comparison uses the rounded figure, not the raw one, so the banner
    never contradicts itself. Comparing raw and displaying rounded put
    "Platinum: 35.0% (threshold 35.0%)" on screen, which reads as a bug: the
    true value was 35.04% and genuinely above the threshold, but nothing
    shown to the reader said so.
    """
    if window not in hv_dict:
        return []
    current = hv_dict[window].iloc[-1]
    alerts = []
    for name, threshold in thresholds.items():
        if name not in current or pd.isna(current[name]):
            continue
        shown = round(current[name], 1)
        if is_breaching(current[name], threshold):
            alerts.append(
                {
                    "name": name,
                    "current_vol": shown,
                    "threshold": threshold,
                    "excess": round(shown - threshold, 1),
                }
            )
    return sorted(alerts, key=lambda x: x["excess"], reverse=True)


def trend_signal(prices: pd.DataFrame) -> pd.Series:
    """
    Simple trend: +1 (price > MA20 > MA50), -1 (price < MA20 < MA50), 0 otherwise.
    """
    last = prices.iloc[-1]
    ma20 = prices.rolling(20).mean().iloc[-1]
    ma50 = prices.rolling(50).mean().iloc[-1]
    signal = pd.Series(0, index=prices.columns)
    signal[(last > ma20) & (ma20 > ma50)] = 1
    signal[(last < ma20) & (ma20 < ma50)] = -1
    return signal
