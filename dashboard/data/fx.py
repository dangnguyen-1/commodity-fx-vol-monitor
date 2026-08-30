"""
FX correlation analysis — how currency pairs move with commodity prices.
Prices come from the paper-trading pipeline's live TradingView feed where
it's tracked, Yahoo Finance as a fallback everywhere else (see
data/market_data.py) — TradingView is the priority source, not just an
equal alternative.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from data.market_data import fetch_prices as _fetch_prices

logger = logging.getLogger(__name__)

# Currency pairs (vs USD) most relevant to commodity economies.
# "tradingview" is the pipeline's derived/direct symbol already in the same
# USD-per-1-unit-of-currency convention as the Yahoo ticker (see
# data_collector/market_data/collectors/generate_fx_inverses.py) — None
# where his TradingView collector doesn't track that currency at all, in
# which case this pair is Yahoo-only.
CURRENCY_PAIRS: dict[str, dict] = {
    # Energy-linked
    "CAN": {"ticker": "CADUSD=X", "tradingview": "DERIVED:CADUSD",  "name": "Canadian Dollar",    "primary": "Energy"},
    "NOR": {"ticker": "NOKUSD=X", "tradingview": None,              "name": "Norwegian Krone",    "primary": "Energy"},
    "RUS": {"ticker": "RUBUSD=X", "tradingview": None,              "name": "Russian Ruble",      "primary": "Energy"},
    "MEX": {"ticker": "MXNUSD=X", "tradingview": None,              "name": "Mexican Peso",       "primary": "Energy"},
    "BRA": {"ticker": "BRLUSD=X", "tradingview": "FX_IDC:BRLUSD",   "name": "Brazilian Real",     "primary": "Agriculture"},
    "COL": {"ticker": "COPUSD=X", "tradingview": None,              "name": "Colombian Peso",     "primary": "Energy"},
    # Metals-linked
    "AUS": {"ticker": "AUDUSD=X", "tradingview": "FX:AUDUSD",       "name": "Australian Dollar",  "primary": "Metals"},
    "CHL": {"ticker": "CLPUSD=X", "tradingview": None,              "name": "Chilean Peso",       "primary": "Metals"},
    "ZAF": {"ticker": "ZARUSD=X", "tradingview": None,              "name": "South African Rand", "primary": "Metals"},
    "PER": {"ticker": "PENUSD=X", "tradingview": None,              "name": "Peruvian Sol",       "primary": "Metals"},
    # Agriculture-linked
    "ARG": {"ticker": "ARSUSD=X", "tradingview": None,              "name": "Argentine Peso",     "primary": "Agriculture"},
    "UKR": {"ticker": "UAHUSD=X", "tradingview": None,              "name": "Ukrainian Hryvnia",  "primary": "Agriculture"},
    "KAZ": {"ticker": "KZTUSD=X", "tradingview": None,              "name": "Kazakhstani Tenge",  "primary": "Agriculture"},
    # Safe-haven / reserve
    "CHE": {"ticker": "CHFUSD=X", "tradingview": "DERIVED:CHFUSD",  "name": "Swiss Franc",        "primary": "Metals"},
    "JPN": {"ticker": "JPYUSD=X", "tradingview": "DERIVED:JPYUSD",  "name": "Japanese Yen",       "primary": "Energy"},
}

# Which commodity each FX pair is most correlated with (for display grouping
# and as the candidate pool the opportunities screener draws from — see
# views/opportunities.py). WTI and Brent are two distinct benchmarks priced
# in different markets, so they get different currency pools rather than
# sharing one: WTI is the US/Western-Hemisphere benchmark (Cushing,
# Oklahoma), so Canada's and Mexico's crude exports price off WTI
# differentials and Colombia's Gulf-Coast-bound exports follow it too.
# Brent is the North Sea/global benchmark — Norway's crude *is* one of the
# streams that makes up the Brent basket, and Russia's Urals grade has
# historically been priced as a differential to dated Brent.
COMMODITY_FX_GROUPS: dict[str, list[str]] = {
    "WTI Crude":   ["CAN", "MEX", "COL"],
    "Brent Crude": ["NOR", "RUS"],
    "Natural Gas": ["RUS", "NOR", "AUS", "CAN"],
    "Gold":        ["AUS", "ZAF", "CAN", "CHE"],
    "Silver":      ["AUS", "ZAF", "CHL", "PER"],
    "Copper":      ["CHL", "PER", "AUS", "ZAF"],
    "Wheat":       ["AUS", "CAN", "RUS", "UKR", "ARG"],
    "Corn":        ["BRA", "ARG", "UKR"],
    "Soybeans":    ["BRA", "ARG"],
}

# Derived lookups for tracking the FX pairs as instruments in their own right
FX_NAMES: list[str] = [v["name"] for v in CURRENCY_PAIRS.values()]
FX_TICKER_TO_NAME: dict[str, str] = {v["ticker"]: v["name"] for v in CURRENCY_PAIRS.values()}
FX_NAME_TO_ISO3: dict[str, str] = {v["name"]: k for k, v in CURRENCY_PAIRS.items()}


def fetch_fx_prices(lookback_days: int = 365) -> pd.DataFrame:
    """
    Fetch the tracked currency pairs' own price history (USD per 1 unit of
    currency), so FX can be tracked as an instrument — same treatment as
    the commodities, not just an input to a correlation coefficient.
    Tries the pipeline's TradingView feed first per currency, Yahoo
    Finance fills in the rest (see data/market_data.py) — most of these
    15 currencies aren't in the TradingView collector at all, so this is
    normally a hybrid result, not a single-source one.
    """
    names_to_tv = {v["name"]: v["tradingview"] for v in CURRENCY_PAIRS.values()}
    names_to_yahoo = {v["name"]: v["ticker"] for v in CURRENCY_PAIRS.values()}
    raw = _fetch_prices(names_to_tv, names_to_yahoo, lookback_days)
    return raw[[n for n in FX_NAMES if n in raw.columns]]


def commodity_fx_relationship(
    commodity_prices: pd.DataFrame,
    window: int = 252,
    fx_prices: pd.DataFrame | None = None,
) -> dict[str, pd.DataFrame]:
    """
    52-week (trailing `window`-trading-day) rolling relationship between
    every currency and every commodity, computed from one shared aligned
    return series so the three stats are internally consistent:

      correlation — Pearson correlation of log-returns.
      beta        — the currency's sensitivity to a 1% commodity move
                    (regressing currency returns on commodity returns);
                    derived as corr * std(currency) / std(commodity), the
                    standard identity for simple univariate regression.
      r2          — share of the currency's return variance the commodity
                    move explains; equals correlation² for a univariate fit.

    `fx_prices`, if given, must be in the same shape fetch_fx_prices()
    returns (columns = currency names, e.g. what's already sitting in the
    store-fx-prices Dash Store) and cover at least `window` rows — reusing
    it here skips a fresh Yahoo Finance fetch of the same 15 tickers a
    caller may have already fetched moments earlier for the Currencies
    tab. Only fetches fresh when that's missing or too short for the
    requested window (a longer rolling window than what's cached).

    Returns {"correlation": df, "beta": df, "r2": df}, each index = ISO3,
    columns = commodity names. Empty DataFrames on failure (e.g. FX fetch
    down) rather than raising, so callers degrade the same way as every
    other lazy-loaded store in this app.
    """
    empty = {"correlation": pd.DataFrame(), "beta": pd.DataFrame(), "r2": pd.DataFrame()}

    if fx_prices is None or fx_prices.empty or len(fx_prices) < window:
        # `window` is trading days; fetch_prices' lookback is calendar
        # days, and weekends/holidays mean `window` calendar days back is
        # well short of `window` *trading* days (e.g. a "52-week"/252-
        # trading-day window only got ~180 trading days of FX history when
        # this passed `window` straight through). Buffer generously so the
        # rolling window is never silently starved of real data.
        calendar_days = int(window * 1.6) + 20
        fx_prices = fetch_fx_prices(calendar_days)
        if fx_prices.empty:
            return empty

    fx_cols_present = [n for n in FX_NAMES if n in fx_prices.columns]
    fx_prices = fx_prices[fx_cols_present]

    # Align dates, then take the trailing `window` rows so this is a true
    # rolling snapshot (recomputed on every refresh) rather than whatever
    # happens to be left over from two independently-fetched lookbacks.
    combined = commodity_prices.join(fx_prices, how="inner").tail(window)
    log_rets = np.log(combined / combined.shift(1)).dropna()
    if log_rets.empty:
        return empty

    commodity_cols = [c for c in commodity_prices.columns if c in log_rets.columns]
    fx_cols = [n for n in fx_cols_present if n in log_rets.columns]
    if not commodity_cols or not fx_cols:
        return empty

    corr_full = log_rets[commodity_cols + fx_cols].corr()
    corr = corr_full.loc[fx_cols, commodity_cols].copy()

    std = log_rets[commodity_cols + fx_cols].std()
    beta = corr.copy()
    for c in commodity_cols:
        denom = std[c]
        for f in fx_cols:
            beta.loc[f, c] = (corr.loc[f, c] * std[f] / denom) if denom else np.nan

    r2 = corr ** 2

    for df in (corr, beta, r2):
        df.index = [FX_NAME_TO_ISO3[n] for n in df.index]
        df.index.name = "iso3"

    return {
        "correlation": corr.round(3),
        "beta": beta.round(3),
        "r2": r2.round(3),
    }


def rolling_relationship_series(
    commodity_prices: pd.Series,
    fx_prices: pd.Series,
    window: int = 252,
) -> pd.DataFrame:
    """
    Rolling `window`-trading-day (52-week) beta, correlation, and R² between
    one commodity and one currency, recomputed at every point in history —
    not a single current snapshot. This is what actually shows a
    relationship strengthening or decaying over years (e.g. CAD's beta to
    oil has drifted toward zero since 2022), which a single trailing-window
    number cannot.

    beta is the currency's sensitivity to the commodity (regressing currency
    log-returns on commodity log-returns): cov(fx, commodity) / var(commodity).

    Returns a DataFrame indexed by date with columns
    ["correlation", "beta", "r2"], NaN-dropped (first `window` rows have no
    trailing window yet).
    """
    combined = pd.DataFrame({"commodity": commodity_prices, "fx": fx_prices}).dropna()
    log_rets = np.log(combined / combined.shift(1)).dropna()
    if len(log_rets) <= window:
        return pd.DataFrame(columns=["correlation", "beta", "r2"])

    roll_corr = log_rets["fx"].rolling(window).corr(log_rets["commodity"])
    roll_cov = log_rets["fx"].rolling(window).cov(log_rets["commodity"])
    roll_var = log_rets["commodity"].rolling(window).var()
    roll_beta = roll_cov / roll_var
    roll_r2 = roll_corr ** 2

    out = pd.DataFrame({"correlation": roll_corr, "beta": roll_beta, "r2": roll_r2}).dropna()
    return out


def price_shock_impact(
    net_positions_usd: dict[str, pd.DataFrame],
    wb_indicators: pd.DataFrame,
    shock_pct: float = 0.10,
) -> pd.DataFrame:
    """
    Estimate the trade-balance impact of a commodity price shock (default +10%).

    For each country × commodity:
      impact_usd = net_usd * shock_pct
      impact_pct_gdp = impact_usd / gdp_usd * 100

    Returns a DataFrame: index = iso3, columns = commodity names, values = % GDP impact.
    """
    gdp = wb_indicators.get("gdp_usd", pd.Series(dtype=float))
    records: dict[str, dict[str, float]] = {}

    for commodity, flows_df in net_positions_usd.items():
        if flows_df.empty:
            continue
        for _, row in flows_df.iterrows():
            iso3 = row.get("reporter_iso3", "")
            net = row.get("net_usd", 0)
            if not iso3 or len(iso3) != 3:
                continue
            impact_usd = net * shock_pct
            country_gdp = gdp.get(iso3, float("nan"))
            impact_pct = (impact_usd / country_gdp * 100) if country_gdp else float("nan")
            if iso3 not in records:
                records[iso3] = {}
            records[iso3][commodity] = round(impact_pct, 3)

    df = pd.DataFrame.from_dict(records, orient="index")
    df.index.name = "iso3"
    return df
