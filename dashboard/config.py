"""
Central configuration for commodity tickers, groups, and alert thresholds.
Alert thresholds are expressed as annualized volatility percentages.
"""

import os

# ---------------------------------------------------------------------------
# Data source: "pipeline" | "bloomberg" | "yahoo" | "mock" — env-overridable.
# "pipeline" (the default) tries the paper-trading pipeline's live
# TradingView feed first and falls back to Yahoo Finance per-instrument for
# whatever the pipeline doesn't track (see data/market_data.py) — this is
# the real, "best version" data path. "yahoo" forces Yahoo Finance only,
# useful if the pipeline API isn't reachable. Bloomberg mode can never work
# off a local Terminal, so it should stay unused anywhere but a workstation
# with Terminal access.
# ---------------------------------------------------------------------------
DATA_SOURCE: str = os.environ.get("DATA_SOURCE", "pipeline")

# The paper-trading pipeline's read-only API (data_collector's TradingView
# feed + UN Comtrade feed, via paper_trading/api/app.py's /market-data and
# /trade-data routes). Market data always tries this first and falls back
# to Yahoo Finance only for symbols it doesn't have — see data/market_data.py.
PIPELINE_API_BASE_URL: str = os.environ.get("PIPELINE_API_BASE_URL", "http://127.0.0.1:8000")

COMMODITIES: dict[str, dict] = {
    # Energy
    "WTI Crude":   {"bbg": "CL1 Comdty", "yahoo": "CL=F",  "tradingview": "NYMEX:CL1!", "comtrade": "Crude Oil / Brent", "group": "Energy",      "alert_vol": 50.0},
    "Brent Crude": {"bbg": "CO1 Comdty", "yahoo": "BZ=F",  "tradingview": "NYMEX:BZ1!", "comtrade": "Crude Oil / Brent", "group": "Energy",      "alert_vol": 45.0},
    "Natural Gas": {"bbg": "NG1 Comdty", "yahoo": "NG=F",  "tradingview": "NYMEX:NG1!", "comtrade": "Natural Gas / LNG", "group": "Energy",      "alert_vol": 70.0},
    # Metals
    "Gold":        {"bbg": "GC1 Comdty", "yahoo": "GC=F",  "tradingview": "COMEX:GC1!", "comtrade": "Gold",              "group": "Metals",      "alert_vol": 25.0},
    "Silver":      {"bbg": "SI1 Comdty", "yahoo": "SI=F",  "tradingview": "COMEX:SI1!", "comtrade": "Silver",            "group": "Metals",      "alert_vol": 35.0},
    "Copper":      {"bbg": "HG1 Comdty", "yahoo": "HG=F",  "tradingview": "COMEX:HG1!", "comtrade": "Copper",            "group": "Metals",      "alert_vol": 30.0},
    # Agriculture
    "Wheat":       {"bbg": "W 1 Comdty", "yahoo": "ZW=F",  "tradingview": "CBOT:ZW1!",  "comtrade": "Wheat",             "group": "Agriculture", "alert_vol": 35.0},
    "Corn":        {"bbg": "C 1 Comdty", "yahoo": "ZC=F",  "tradingview": "CBOT:ZC1!",  "comtrade": "Corn",              "group": "Agriculture", "alert_vol": 30.0},
    "Soybeans":    {"bbg": "S 1 Comdty", "yahoo": "ZS=F",  "tradingview": "CBOT:ZS1!",  "comtrade": "Soybeans",          "group": "Agriculture", "alert_vol": 28.0},
}

# Derived lookups
NAMES: list[str]   = list(COMMODITIES.keys())
GROUPS: list[str]  = ["Energy", "Metals", "Agriculture"]
ALERT_THRESHOLDS: dict[str, float] = {k: v["alert_vol"] for k, v in COMMODITIES.items()}

BBG_TICKERS: list[str]         = [v["bbg"]   for v in COMMODITIES.values()]
YAHOO_TICKERS: list[str]       = [v["yahoo"] for v in COMMODITIES.values()]

BBG_TO_NAME: dict[str, str]    = {v["bbg"]:   k for k, v in COMMODITIES.items()}
YAHOO_TO_NAME: dict[str, str]  = {v["yahoo"]: k for k, v in COMMODITIES.items()}

# Volatility windows (days)
VOL_WINDOWS: list[int] = [30, 60, 90]

# ---------------------------------------------------------------------------
# UI tokens — "departures board" design system (shared across app and views)
# The board is a physical information display: a warm-neutral near-black
# surface, off-white flap characters, and ONE reserved accent for board
# chrome/alerts. Gains/losses/informational data keep their own semantic
# hues — those encode facts, not brand decoration.
# ---------------------------------------------------------------------------
UI_BG      = "#12141a"   # board surface (page background)
UI_PANEL   = "#1a1d26"   # tile/panel surface, one step up from the board
UI_BORDER  = "#2b2f3d"   # hairline seam between flap tiles / table rows
UI_TEXT    = "#EAE7DC"   # flap-card off-white — primary text
UI_MUTED   = "#7d8394"   # secondary/muted text
UI_ACCENT  = "#F2A93B"   # board chrome + alerts: active tab, lit dot, threshold breach
UI_AMBER   = UI_ACCENT   # legacy alias
UI_RED     = "#FB5B6E"   # losses / net importer / danger
UI_GREEN   = "#34D399"   # gains / net exporter
UI_BLUE    = "#4EA1F7"   # informational / neutral data (exporters, FX reference)
UI_OCEAN   = "#0B0C10"   # map ocean fill, darker than the board surface
