"""
FX correlation analysis — how currency pairs move with commodity prices.
Prices come from the pipeline's live TradingView feed where it's tracked,
Yahoo Finance as a fallback everywhere else (see data/market_data.py).
TradingView is the priority source, not just an equal alternative.
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
# where the TradingView collector doesn't track that currency at all, in
# which case this pair is Yahoo-only.
CURRENCY_PAIRS: dict[str, dict] = {
    # Energy-linked
    "CAN": {"ticker": "CADUSD=X", "tradingview": "DERIVED:CADUSD",  "name": "Canadian Dollar",    "primary": "Energy"},
    "NOR": {"ticker": "NOKUSD=X", "tradingview": "DERIVED:NOKUSD",  "name": "Norwegian Krone",    "primary": "Energy"},
    "RUS": {"ticker": "RUBUSD=X", "tradingview": "DERIVED:RUBUSD",  "name": "Russian Ruble",      "primary": "Energy"},
    "MEX": {"ticker": "MXNUSD=X", "tradingview": "DERIVED:MXNUSD",  "name": "Mexican Peso",       "primary": "Energy"},
    "BRA": {"ticker": "BRLUSD=X", "tradingview": "FX_IDC:BRLUSD",   "name": "Brazilian Real",     "primary": "Agriculture"},
    "COL": {"ticker": "COPUSD=X", "tradingview": "DERIVED:COPUSD",  "name": "Colombian Peso",     "primary": "Energy"},
    # Metals-linked
    "AUS": {"ticker": "AUDUSD=X", "tradingview": "FX:AUDUSD",       "name": "Australian Dollar",  "primary": "Metals"},
    "CHL": {"ticker": "CLPUSD=X", "tradingview": "DERIVED:CLPUSD",  "name": "Chilean Peso",       "primary": "Metals"},
    "ZAF": {"ticker": "ZARUSD=X", "tradingview": "DERIVED:ZARUSD",  "name": "South African Rand", "primary": "Metals"},
    "PER": {"ticker": "PENUSD=X", "tradingview": "DERIVED:PENUSD",  "name": "Peruvian Sol",       "primary": "Metals"},
    # World's #2 copper producer (after Chile) — Kamoa-Kakula alone has
    # made the DRC a bigger copper story than most tracked currencies here.
    "COD": {"ticker": "CDFUSD=X", "tradingview": "DERIVED:CDFUSD",  "name": "Congolese Franc",    "primary": "Metals"},
    # Africa's #2 copper producer, targeting 1M+ tonnes in 2026.
    "ZMB": {"ticker": "ZMWUSD=X", "tradingview": "DERIVED:ZMWUSD",  "name": "Zambian Kwacha",     "primary": "Metals"},
    # Africa's largest gold producer, ~6th globally and still growing.
    "GHA": {"ticker": "GHSUSD=X", "tradingview": "DERIVED:GHSUSD",  "name": "Ghanaian Cedi",      "primary": "Metals"},
    # Agriculture-linked
    "ARG": {"ticker": "ARSUSD=X", "tradingview": "DERIVED:ARSUSD",  "name": "Argentine Peso",     "primary": "Agriculture"},
    "UKR": {"ticker": "UAHUSD=X", "tradingview": "DERIVED:UAHUSD",  "name": "Ukrainian Hryvnia",  "primary": "Agriculture"},
    "KAZ": {"ticker": "KZTUSD=X", "tradingview": "DERIVED:KZTUSD",  "name": "Kazakhstani Tenge",  "primary": "Agriculture"},
    # World's #3 soybean exporter (~3.4M tonnes/year).
    "PRY": {"ticker": "PYGUSD=X", "tradingview": "DERIVED:PYGUSD",  "name": "Paraguayan Guarani", "primary": "Agriculture"},
    # Importers and clearing centres. Not commodity exporters, and here on
    # purpose: every other currency above is exporter-side, so without these
    # the terms-of-trade sign can only ever be tested in one direction. An
    # importer's currency should move the opposite way to the same commodity.
    #
    # GBR replaced CHE (Swiss Franc) here. Switzerland refines most of the
    # world's gold, which is why it was tracked, but it reports none of it:
    # all 5,226 of its Comtrade rows came back NULL, because Swiss customs
    # excludes precious metals from the headline statistics. The UK is the
    # other great gold clearing centre, at $766.9B of gross gold exports, and
    # unlike Switzerland it actually reports. It also carries the energy
    # import exposure the book was missing.
    "GBR": {"ticker": "GBPUSD=X", "tradingview": "FX:GBPUSD",       "name": "British Pound",      "primary": "Energy"},
    "JPN": {"ticker": "JPYUSD=X", "tradingview": "DERIVED:JPYUSD",  "name": "Japanese Yen",       "primary": "Energy"},
}

# Which commodity each FX pair is most correlated with (for display grouping
# and as the candidate pool the opportunities screener draws from — see
# views/opportunities.py). Verified against each grade's actual published
# pricing convention rather than assumed:
#   - CAD: Western Canadian Select's pricing formula references only WTI
#     (the WCS-WTI differential), but as a major oil-exporter currency
#     CAD also trades on broad global crude sentiment, which Brent sets
#     as the world benchmark — so it's tracked as dual rather than
#     WTI-only.
#   - MXN: Pemex's Maya formula is literally 0.65*WTI Houston + 0.35*ICE
#     Brent + a K factor — genuinely priced off both. Dual.
#   - COP: Colombian grades (Castilla, Vasconia) now benchmark mainly to
#     Brent as Gulf Coast exports have faded, but WTI is still an active
#     reference — dual, Brent-leaning.
#   - NOK: Norwegian crude (Ekofisk/Oseberg/Troll) is a physical component
#     of the Brent basket itself (the "BFOE" blend) — there is no WTI
#     linkage in its pricing convention. Brent-only.
#   - RUB: Urals is priced exclusively as a differential to Dated Brent.
#     No WTI reference exists in its pricing convention. Brent-only.
#   - JPY: Japan's dominant crude benchmark is actually Dubai (Middle
#     East sour), not Brent or WTI — but it's tracked here as WTI-linked
#     for its real, if secondary and growing, exposure to US crude
#     (record Japanese WTI purchase volumes as it diversifies away from
#     the Middle East).
COMMODITY_FX_GROUPS: dict[str, list[str]] = {
    "WTI Crude":   ["CAN", "MEX", "COL", "JPN"],
    # GBR is net -$117.6B in crude and the field this contract is named
    # after is British. It sits here as an importer, so its expected
    # sign is opposite to the exporters beside it.
    "Brent Crude": ["CAN", "NOR", "RUS", "MEX", "COL", "GBR"],
    "Natural Gas": ["RUS", "NOR", "AUS", "CAN", "GBR"],
    # Australia nets +$389B in coal over 2020-2025, more than double
    # Indonesia and its second largest export after iron ore. Indonesia
    # leads on volume among non-tracked currencies but the rupiah is not in
    # CURRENCY_PAIRS. COL is included on the same footing as in Coffee: a
    # genuine top-five seaborne exporter whose rows arrive with the widened
    # reporter list.
    "Coal":        ["AUS", "ZAF", "CAN", "RUS", "COL"],
    # RUS added: world's #2 gold producer. GHA added: Africa's #1 and
    # world's #6 gold producer (new currency, see CURRENCY_PAIRS).
    "Gold":        ["AUS", "ZAF", "CAN", "GBR", "RUS", "GHA"],
    # MEX added: world's #1 silver producer by a wide margin (~1/5 of
    # global supply) — was only tracked here for oil before.
    "Silver":      ["AUS", "ZAF", "CHL", "PER", "MEX"],
    # COD (DRC) added: world's #2 copper producer, ahead of the US, on
    # the back of Kamoa-Kakula. ZMB (Zambia) added: Africa's #2 producer,
    # targeting 1M+ tonnes in 2026. Both new currencies — see
    # CURRENCY_PAIRS.
    "Copper":      ["CHL", "PER", "AUS", "ZAF", "COD", "ZMB"],
    # KAZ added: top-10 exporter, one of the fastest-growing (+46% in
    # 2024-25) — was already a tracked currency, just missing here.
    "Wheat":       ["AUS", "CAN", "RUS", "UKR", "ARG", "KAZ"],
    # Corn's top 5 exporters (US, Brazil, Argentina, Ukraine, France) are
    # already covered by trackable currencies — US is the base currency
    # and France uses the euro, which isn't tracked as a France-specific
    # pair — so no changes here.
    "Corn":        ["BRA", "ARG", "UKR"],
    # CAN and UKR added: both already-tracked currencies, both among the
    # top soybean exporters (Brazil, US, Paraguay, Canada, Ukraine,
    # Argentina), just missing from this group. PRY (Paraguay) added as
    # a new currency: world's #3 exporter (~3.4M tonnes/year).
    "Soybeans":    ["BRA", "ARG", "CAN", "UKR", "PRY"],

    # The groups below were derived from the trade data rather than asserted:
    # net exports per country per commodity in fundamental_trade_data over
    # 2020 onward, keeping the tracked currencies that lead on net.
    #
    # Net, not gross, is what decides membership. On gross exports the UK
    # outranks South Africa in platinum and the US outranks everyone in
    # palladium, which is London and New York clearing metal they did not
    # mine. On net the picture inverts: ZAF +$13.5B against GBR +$4.4B in
    # platinum, ZAF +$18.7B against USA +$1.9B in palladium.
    "Platinum":    ["ZAF", "RUS"],
    "Palladium":   ["ZAF", "RUS"],
    # Australia is not merely first here, it is +$614B net against Brazil's
    # +$220B, the most lopsided relationship anywhere in this table.
    "Iron Ore":    ["AUS", "BRA", "ZAF", "CAN"],
    "Aluminium":   ["CAN", "AUS", "RUS"],
    # COL carries no Comtrade rows because Colombia is not one of the 30
    # reporters the collector requests, so it is included on the economics
    # (third largest coffee exporter globally) and contributes to the price
    # correlation view only. It will gain trade backing if the reporter list
    # is widened.
    "Coffee":      ["BRA", "COL"],
    "Sugar":       ["BRA", "MEX"],
    "Cotton":      ["BRA", "AUS", "ARG"],
    "Live Cattle": ["CAN", "AUS", "BRA", "MEX"],
}

# Cocoa and uranium were considered and deliberately left out, in both cases
# because the trade data cannot support them rather than because the economic
# link is weak.
#
# Cocoa: the leading "exporters" in fundamental_trade_data are the
# Netherlands and the United States, both deeply net negative (-$19.8B and
# -$10.1B). Those are grinders and re-exporters. Ghana and Cote d'Ivoire, who
# actually grow it, are not among the 30 reporters, so the series describes
# processing rather than production and would attach GHS to a number that
# says nothing about Ghana.
#
# Uranium: Kazakhstan produces roughly 40% of world supply and is likewise
# not a reporter. What remains reads as enriched fuel moving between France,
# the Netherlands and Germany rather than ore leaving the ground.
#
# Both become usable if the reporter list in
# data_collector/fundamental_data/config/countries.py is widened.

# Natural Gas is deliberately left at ["RUS", "NOR", "AUS", "CAN"] despite
# Qatar being a top-3 LNG exporter: the Qatari riyal is a hard USD peg, so
# its "FX correlation" with gas prices is structurally ~0 regardless of
# the real trade relationship — tracking it wouldn't show anything.

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
