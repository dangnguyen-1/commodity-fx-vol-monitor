"""
Commodity trade flows for Sankey diagrams and net-position maps.

Three layers, tried in order: the paper-trading pipeline's own UN Comtrade
feed (data/pipeline_trade.py — pre-aggregated, no rate limits), the free
public Comtrade API (data/comtrade.py — real API calls, real rate limits)
for whatever the pipeline doesn't have, and this file's static table when
both live sources fail for a given commodity. The static table stays
labeled 2023 estimates because it is one: a fixed table sourced from IEA
Oil Market Report 2023, USDA WASDE 2023, USGS Minerals Yearbook 2023, and
UN Comtrade annual summaries, frozen at the time it was written.

The public functions at the bottom (`trade_flows_for_commodity`,
`top_traders`, `net_positions`) try live data first and fall back to this
static table per-commodity, so one commodity having no recent data
anywhere doesn't take down the other eight.
"""

from __future__ import annotations

import logging
import time

import pandas as pd

from config import COMMODITIES
from data import comtrade as _comtrade
from data import pipeline_trade as _pipeline_trade

logger = logging.getLogger(__name__)

_pipeline_commodity_names = {
    name: spec.get("comtrade") for name, spec in COMMODITIES.items()
}

# ---------------------------------------------------------------------------
# Raw trade data: {commodity: {exporters: [...], importers: [...]}}
# trade_usd is approximate USD for 2023 (some commodities use 2022 actuals)
# ---------------------------------------------------------------------------

_FLOWS: dict[str, dict] = {

    "WTI Crude": {  # shares HS 2709 with Brent — same physical flows
        "exporters": [
            {"iso3": "SAU", "name": "Saudi Arabia",    "usd": 240_000_000_000},
            {"iso3": "RUS", "name": "Russia",          "usd": 130_000_000_000},
            {"iso3": "IRQ", "name": "Iraq",            "usd": 100_000_000_000},
            {"iso3": "USA", "name": "United States",   "usd":  90_000_000_000},
            {"iso3": "UAE", "name": "United Arab Emirates", "usd": 80_000_000_000},
            {"iso3": "CAN", "name": "Canada",          "usd":  75_000_000_000},
            {"iso3": "NOR", "name": "Norway",          "usd":  70_000_000_000},
            {"iso3": "KWT", "name": "Kuwait",          "usd":  55_000_000_000},
            {"iso3": "KAZ", "name": "Kazakhstan",      "usd":  40_000_000_000},
            {"iso3": "NGA", "name": "Nigeria",         "usd":  38_000_000_000},
            {"iso3": "BRA", "name": "Brazil",          "usd":  30_000_000_000},
            {"iso3": "LBY", "name": "Libya",           "usd":  25_000_000_000},
            {"iso3": "AGO", "name": "Angola",          "usd":  22_000_000_000},
            {"iso3": "MEX", "name": "Mexico",          "usd":  20_000_000_000},
            {"iso3": "DZA", "name": "Algeria",         "usd":  15_000_000_000},
        ],
        "importers": [
            {"iso3": "CHN", "name": "China",           "usd": 290_000_000_000},
            {"iso3": "IND", "name": "India",           "usd": 110_000_000_000},
            {"iso3": "USA", "name": "United States",   "usd":  80_000_000_000},
            {"iso3": "KOR", "name": "South Korea",     "usd":  70_000_000_000},
            {"iso3": "JPN", "name": "Japan",           "usd":  65_000_000_000},
            {"iso3": "DEU", "name": "Germany",         "usd":  40_000_000_000},
            {"iso3": "NLD", "name": "Netherlands",     "usd":  38_000_000_000},
            {"iso3": "ITA", "name": "Italy",           "usd":  30_000_000_000},
            {"iso3": "SGP", "name": "Singapore",       "usd":  28_000_000_000},
            {"iso3": "THA", "name": "Thailand",        "usd":  22_000_000_000},
            {"iso3": "GBR", "name": "United Kingdom",  "usd":  20_000_000_000},
            {"iso3": "FRA", "name": "France",          "usd":  18_000_000_000},
            {"iso3": "TUR", "name": "Turkey",          "usd":  15_000_000_000},
            {"iso3": "ESP", "name": "Spain",           "usd":  14_000_000_000},
            {"iso3": "IDN", "name": "Indonesia",       "usd":  12_000_000_000},
        ],
    },

    "Brent Crude": {  # same underlying crude flows
        "exporters": [
            {"iso3": "SAU", "name": "Saudi Arabia",    "usd": 240_000_000_000},
            {"iso3": "RUS", "name": "Russia",          "usd": 130_000_000_000},
            {"iso3": "IRQ", "name": "Iraq",            "usd": 100_000_000_000},
            {"iso3": "USA", "name": "United States",   "usd":  90_000_000_000},
            {"iso3": "UAE", "name": "United Arab Emirates", "usd": 80_000_000_000},
            {"iso3": "CAN", "name": "Canada",          "usd":  75_000_000_000},
            {"iso3": "NOR", "name": "Norway",          "usd":  70_000_000_000},
            {"iso3": "KWT", "name": "Kuwait",          "usd":  55_000_000_000},
            {"iso3": "KAZ", "name": "Kazakhstan",      "usd":  40_000_000_000},
            {"iso3": "NGA", "name": "Nigeria",         "usd":  38_000_000_000},
        ],
        "importers": [
            {"iso3": "CHN", "name": "China",           "usd": 290_000_000_000},
            {"iso3": "IND", "name": "India",           "usd": 110_000_000_000},
            {"iso3": "USA", "name": "United States",   "usd":  80_000_000_000},
            {"iso3": "KOR", "name": "South Korea",     "usd":  70_000_000_000},
            {"iso3": "JPN", "name": "Japan",           "usd":  65_000_000_000},
            {"iso3": "DEU", "name": "Germany",         "usd":  40_000_000_000},
            {"iso3": "NLD", "name": "Netherlands",     "usd":  38_000_000_000},
            {"iso3": "ITA", "name": "Italy",           "usd":  30_000_000_000},
            {"iso3": "SGP", "name": "Singapore",       "usd":  28_000_000_000},
            {"iso3": "THA", "name": "Thailand",        "usd":  22_000_000_000},
        ],
    },

    "Natural Gas": {
        "exporters": [
            {"iso3": "USA", "name": "United States",   "usd": 120_000_000_000},
            {"iso3": "RUS", "name": "Russia",          "usd":  95_000_000_000},
            {"iso3": "QAT", "name": "Qatar",           "usd":  80_000_000_000},
            {"iso3": "AUS", "name": "Australia",       "usd":  70_000_000_000},
            {"iso3": "NOR", "name": "Norway",          "usd":  60_000_000_000},
            {"iso3": "CAN", "name": "Canada",          "usd":  25_000_000_000},
            {"iso3": "DZA", "name": "Algeria",         "usd":  20_000_000_000},
            {"iso3": "NGA", "name": "Nigeria",         "usd":  15_000_000_000},
            {"iso3": "MYS", "name": "Malaysia",        "usd":  14_000_000_000},
            {"iso3": "TKM", "name": "Turkmenistan",    "usd":  10_000_000_000},
        ],
        "importers": [
            {"iso3": "CHN", "name": "China",           "usd":  90_000_000_000},
            {"iso3": "JPN", "name": "Japan",           "usd":  80_000_000_000},
            {"iso3": "DEU", "name": "Germany",         "usd":  50_000_000_000},
            {"iso3": "KOR", "name": "South Korea",     "usd":  40_000_000_000},
            {"iso3": "ITA", "name": "Italy",           "usd":  30_000_000_000},
            {"iso3": "FRA", "name": "France",          "usd":  25_000_000_000},
            {"iso3": "GBR", "name": "United Kingdom",  "usd":  22_000_000_000},
            {"iso3": "TUR", "name": "Turkey",          "usd":  18_000_000_000},
            {"iso3": "IND", "name": "India",           "usd":  15_000_000_000},
            {"iso3": "NLD", "name": "Netherlands",     "usd":  12_000_000_000},
        ],
    },

    "Gold": {
        "exporters": [
            {"iso3": "CHN", "name": "China",           "usd":  60_000_000_000},
            {"iso3": "AUS", "name": "Australia",       "usd":  55_000_000_000},
            {"iso3": "RUS", "name": "Russia",          "usd":  45_000_000_000},
            {"iso3": "CAN", "name": "Canada",          "usd":  28_000_000_000},
            {"iso3": "GHA", "name": "Ghana",           "usd":  12_000_000_000},
            {"iso3": "ZAF", "name": "South Africa",    "usd":  10_000_000_000},
            {"iso3": "USA", "name": "United States",   "usd":   9_000_000_000},
            {"iso3": "UZB", "name": "Uzbekistan",      "usd":   7_000_000_000},
            {"iso3": "MEX", "name": "Mexico",          "usd":   6_000_000_000},
            {"iso3": "PNG", "name": "Papua New Guinea","usd":   4_000_000_000},
        ],
        "importers": [
            {"iso3": "CHE", "name": "Switzerland",     "usd": 100_000_000_000},
            {"iso3": "GBR", "name": "United Kingdom",  "usd":  80_000_000_000},
            {"iso3": "IND", "name": "India",           "usd":  50_000_000_000},
            {"iso3": "CHN", "name": "China",           "usd":  45_000_000_000},
            {"iso3": "HKG", "name": "Hong Kong",       "usd":  40_000_000_000},
            {"iso3": "ARE", "name": "United Arab Emirates", "usd": 30_000_000_000},
            {"iso3": "USA", "name": "United States",   "usd":  28_000_000_000},
            {"iso3": "SGP", "name": "Singapore",       "usd":  15_000_000_000},
            {"iso3": "TUR", "name": "Turkey",          "usd":  12_000_000_000},
            {"iso3": "THA", "name": "Thailand",        "usd":   8_000_000_000},
        ],
    },

    "Silver": {
        "exporters": [
            {"iso3": "MEX", "name": "Mexico",          "usd":   5_000_000_000},
            {"iso3": "PER", "name": "Peru",            "usd":   3_500_000_000},
            {"iso3": "CHN", "name": "China",           "usd":   2_800_000_000},
            {"iso3": "CHL", "name": "Chile",           "usd":   1_800_000_000},
            {"iso3": "POL", "name": "Poland",          "usd":   1_500_000_000},
            {"iso3": "RUS", "name": "Russia",          "usd":   1_200_000_000},
            {"iso3": "AUS", "name": "Australia",       "usd":   1_000_000_000},
            {"iso3": "BOL", "name": "Bolivia",         "usd":     800_000_000},
            {"iso3": "ARG", "name": "Argentina",       "usd":     700_000_000},
            {"iso3": "KAZ", "name": "Kazakhstan",      "usd":     500_000_000},
        ],
        "importers": [
            {"iso3": "IND", "name": "India",           "usd":   7_000_000_000},
            {"iso3": "GBR", "name": "United Kingdom",  "usd":   4_000_000_000},
            {"iso3": "USA", "name": "United States",   "usd":   3_000_000_000},
            {"iso3": "CHN", "name": "China",           "usd":   2_500_000_000},
            {"iso3": "JPN", "name": "Japan",           "usd":   2_000_000_000},
            {"iso3": "DEU", "name": "Germany",         "usd":   1_500_000_000},
            {"iso3": "KOR", "name": "South Korea",     "usd":   1_200_000_000},
            {"iso3": "ITA", "name": "Italy",           "usd":   1_000_000_000},
            {"iso3": "CAN", "name": "Canada",          "usd":     800_000_000},
            {"iso3": "BEL", "name": "Belgium",         "usd":     600_000_000},
        ],
    },

    "Copper": {
        "exporters": [
            {"iso3": "CHL", "name": "Chile",           "usd":  42_000_000_000},
            {"iso3": "PER", "name": "Peru",            "usd":  14_000_000_000},
            {"iso3": "COD", "name": "DR Congo",        "usd":  10_000_000_000},
            {"iso3": "AUS", "name": "Australia",       "usd":   7_000_000_000},
            {"iso3": "ZMB", "name": "Zambia",          "usd":   6_000_000_000},
            {"iso3": "RUS", "name": "Russia",          "usd":   5_000_000_000},
            {"iso3": "KAZ", "name": "Kazakhstan",      "usd":   4_000_000_000},
            {"iso3": "POL", "name": "Poland",          "usd":   3_500_000_000},
            {"iso3": "USA", "name": "United States",   "usd":   3_000_000_000},
            {"iso3": "MEX", "name": "Mexico",          "usd":   2_500_000_000},
        ],
        "importers": [
            {"iso3": "CHN", "name": "China",           "usd":  55_000_000_000},
            {"iso3": "JPN", "name": "Japan",           "usd":   8_000_000_000},
            {"iso3": "KOR", "name": "South Korea",     "usd":   6_000_000_000},
            {"iso3": "DEU", "name": "Germany",         "usd":   5_000_000_000},
            {"iso3": "ITA", "name": "Italy",           "usd":   4_000_000_000},
            {"iso3": "USA", "name": "United States",   "usd":   3_500_000_000},
            {"iso3": "IND", "name": "India",           "usd":   3_000_000_000},
            {"iso3": "BEL", "name": "Belgium",         "usd":   2_500_000_000},
            {"iso3": "TWN", "name": "Taiwan",          "usd":   2_000_000_000},
            {"iso3": "TUR", "name": "Turkey",          "usd":   1_800_000_000},
        ],
    },

    "Wheat": {
        "exporters": [
            {"iso3": "RUS", "name": "Russia",          "usd":  14_000_000_000},
            {"iso3": "AUS", "name": "Australia",       "usd":   9_000_000_000},
            {"iso3": "CAN", "name": "Canada",          "usd":   8_500_000_000},
            {"iso3": "USA", "name": "United States",   "usd":   7_000_000_000},
            {"iso3": "UKR", "name": "Ukraine",         "usd":   5_000_000_000},
            {"iso3": "ARG", "name": "Argentina",       "usd":   4_000_000_000},
            {"iso3": "KAZ", "name": "Kazakhstan",      "usd":   2_500_000_000},
            {"iso3": "FRA", "name": "France",          "usd":   4_000_000_000},
            {"iso3": "DEU", "name": "Germany",         "usd":   2_000_000_000},
            {"iso3": "ROM", "name": "Romania",         "usd":   1_500_000_000},
        ],
        "importers": [
            {"iso3": "EGY", "name": "Egypt",           "usd":   3_500_000_000},
            {"iso3": "IDN", "name": "Indonesia",       "usd":   3_000_000_000},
            {"iso3": "TUR", "name": "Turkey",          "usd":   2_500_000_000},
            {"iso3": "NGA", "name": "Nigeria",         "usd":   2_200_000_000},
            {"iso3": "BGD", "name": "Bangladesh",      "usd":   1_800_000_000},
            {"iso3": "PAK", "name": "Pakistan",        "usd":   1_700_000_000},
            {"iso3": "BRA", "name": "Brazil",          "usd":   2_000_000_000},
            {"iso3": "MEX", "name": "Mexico",          "usd":   1_500_000_000},
            {"iso3": "IRN", "name": "Iran",            "usd":   1_400_000_000},
            {"iso3": "PHL", "name": "Philippines",     "usd":   1_200_000_000},
        ],
    },

    "Corn": {
        "exporters": [
            {"iso3": "USA", "name": "United States",   "usd":  15_000_000_000},
            {"iso3": "BRA", "name": "Brazil",          "usd":  14_000_000_000},
            {"iso3": "ARG", "name": "Argentina",       "usd":   7_000_000_000},
            {"iso3": "UKR", "name": "Ukraine",         "usd":   4_000_000_000},
            {"iso3": "IND", "name": "India",           "usd":   1_500_000_000},
            {"iso3": "ZAF", "name": "South Africa",    "usd":     900_000_000},
            {"iso3": "HUN", "name": "Hungary",         "usd":     800_000_000},
            {"iso3": "ROM", "name": "Romania",         "usd":     700_000_000},
            {"iso3": "RUS", "name": "Russia",          "usd":     600_000_000},
            {"iso3": "SRB", "name": "Serbia",          "usd":     500_000_000},
        ],
        "importers": [
            {"iso3": "CHN", "name": "China",           "usd":  10_000_000_000},
            {"iso3": "MEX", "name": "Mexico",          "usd":   5_000_000_000},
            {"iso3": "JPN", "name": "Japan",           "usd":   4_500_000_000},
            {"iso3": "KOR", "name": "South Korea",     "usd":   3_000_000_000},
            {"iso3": "VNM", "name": "Vietnam",         "usd":   2_500_000_000},
            {"iso3": "IRN", "name": "Iran",            "usd":   2_000_000_000},
            {"iso3": "EGY", "name": "Egypt",           "usd":   1_800_000_000},
            {"iso3": "IDN", "name": "Indonesia",       "usd":   1_500_000_000},
            {"iso3": "COL", "name": "Colombia",        "usd":   1_200_000_000},
            {"iso3": "ESP", "name": "Spain",           "usd":   1_000_000_000},
        ],
    },

    "Soybeans": {
        "exporters": [
            {"iso3": "BRA", "name": "Brazil",          "usd":  40_000_000_000},
            {"iso3": "USA", "name": "United States",   "usd":  25_000_000_000},
            {"iso3": "ARG", "name": "Argentina",       "usd":   6_000_000_000},
            {"iso3": "PRY", "name": "Paraguay",        "usd":   2_500_000_000},
            {"iso3": "CAN", "name": "Canada",          "usd":   1_500_000_000},
            {"iso3": "URY", "name": "Uruguay",         "usd":   1_200_000_000},
            {"iso3": "UKR", "name": "Ukraine",         "usd":     900_000_000},
            {"iso3": "BOL", "name": "Bolivia",         "usd":     800_000_000},
            {"iso3": "RUS", "name": "Russia",          "usd":     600_000_000},
            {"iso3": "IND", "name": "India",           "usd":     400_000_000},
        ],
        "importers": [
            {"iso3": "CHN", "name": "China",           "usd":  50_000_000_000},
            {"iso3": "ARG", "name": "Argentina",       "usd":   5_000_000_000},
            {"iso3": "NLD", "name": "Netherlands",     "usd":   4_000_000_000},
            {"iso3": "MEX", "name": "Mexico",          "usd":   2_500_000_000},
            {"iso3": "EGY", "name": "Egypt",           "usd":   2_000_000_000},
            {"iso3": "JPN", "name": "Japan",           "usd":   2_000_000_000},
            {"iso3": "THA", "name": "Thailand",        "usd":   1_800_000_000},
            {"iso3": "DEU", "name": "Germany",         "usd":   1_500_000_000},
            {"iso3": "IDN", "name": "Indonesia",       "usd":   1_500_000_000},
            {"iso3": "TUR", "name": "Turkey",          "usd":   1_200_000_000},
        ],
    },
}


# ---------------------------------------------------------------------------
# Static fallback implementation (2023 estimate table above)
# ---------------------------------------------------------------------------

def _static_trade_flows_for_commodity(commodity_name: str) -> pd.DataFrame:
    """Return a long-form DataFrame with columns: reporter_iso3, reporter_name, flow, trade_usd."""
    data = _FLOWS.get(commodity_name)
    if not data:
        return pd.DataFrame()
    records = [
        {**{"reporter_iso3": r["iso3"], "reporter_name": r["name"],
            "flow": "Export", "trade_usd": r["usd"]}}
        for r in data["exporters"]
    ] + [
        {**{"reporter_iso3": r["iso3"], "reporter_name": r["name"],
            "flow": "Import", "trade_usd": r["usd"]}}
        for r in data["importers"]
    ]
    return pd.DataFrame(records)


def _static_top_traders(commodity_name: str, top_n: int = 15) -> dict[str, pd.DataFrame]:
    data = _FLOWS.get(commodity_name)
    if not data:
        return {"exporters": pd.DataFrame(), "importers": pd.DataFrame()}

    def _df(rows: list[dict]) -> pd.DataFrame:
        df = pd.DataFrame([
            {"reporter_iso3": r["iso3"], "reporter_name": r["name"], "trade_usd": r["usd"]}
            for r in rows
        ])
        return df.sort_values("trade_usd", ascending=False).head(top_n).reset_index(drop=True)

    return {"exporters": _df(data["exporters"]), "importers": _df(data["importers"])}


def _static_net_positions(commodity_name: str) -> pd.DataFrame:
    flows = _static_trade_flows_for_commodity(commodity_name)
    if flows.empty:
        return pd.DataFrame()

    pivot = flows.pivot_table(
        index=["reporter_iso3", "reporter_name"],
        columns="flow",
        values="trade_usd",
        aggfunc="sum",
    ).fillna(0).reset_index()
    pivot.columns.name = None

    if "Export" not in pivot.columns:
        pivot["Export"] = 0
    if "Import" not in pivot.columns:
        pivot["Import"] = 0

    pivot["net_usd"] = pivot["Export"] - pivot["Import"]
    return pivot.sort_values("net_usd", ascending=False)


# ---------------------------------------------------------------------------
# Public API — live Comtrade first, static table as a per-commodity
# fallback. Memoized per process so a page visit that touches the same
# commodity from several views (Sankey, map coloring, country detail,
# price-shock calc across all 9 names) doesn't refetch live data 9 times.
#
# TTL'd rather than cached forever: this dict lives for the lifetime of the
# running process, and on a long-lived cloud deployment (not a locally
# restarted dev server) an unbounded cache would keep serving the same TTM
# window and "as of" label indefinitely, never noticing a new month rolled
# over. 24h matches how often the underlying window can actually change.
# ---------------------------------------------------------------------------

_LIVE_CACHE_TTL = 86_400  # 24 hours
_LIVE_CACHE: dict[str, tuple[pd.DataFrame, str | None, str, float]] = {}


def _live_flows(commodity_name: str) -> tuple[pd.DataFrame, str | None, str]:
    """Returns (flows, period, source) — source is "pipeline" or
    "comtrade_api" so data_source_label() can say which one actually
    served the data, rather than a blanket "UN Comtrade" claim that's
    right for one path and misleading for the other."""
    cached = _LIVE_CACHE.get(commodity_name)
    if cached is not None and (time.time() - cached[3]) < _LIVE_CACHE_TTL:
        return cached[0], cached[1], cached[2]

    # The pipeline's pre-aggregated, rate-limit-free feed is tried first;
    # the free public Comtrade API (data/comtrade.py — TTM window, real
    # rate limits) only fills in a commodity the pipeline doesn't have.
    flows, period, source = pd.DataFrame(), None, "pipeline"
    comtrade_name = _pipeline_commodity_names.get(commodity_name)
    if comtrade_name:
        try:
            flows, period = _pipeline_trade.fetch_trade_flows(comtrade_name)
        except Exception as exc:
            logger.warning("Pipeline trade-data fetch failed for %s: %s", commodity_name, exc)

    if flows.empty:
        source = "comtrade_api"
        try:
            flows, period = _comtrade.trade_flows_for_commodity(commodity_name)
        except Exception as exc:
            logger.warning("Live Comtrade fetch failed for %s: %s", commodity_name, exc)
            flows, period = pd.DataFrame(), None

    _LIVE_CACHE[commodity_name] = (flows, period, source, time.time())
    return flows, period, source


def data_source_label(commodity_name: str) -> str:
    """For the UI caption — tells the user whether they're looking at the
    paper-trading pipeline's live feed, the free Comtrade API fallback, or
    the static fallback, and which window, rather than a blanket claim
    that's wrong for whichever mode isn't active."""
    flows, period, source = _live_flows(commodity_name)
    if not flows.empty and period:
        window_label = _comtrade.format_ttm_label(period)
        if source == "pipeline":
            return f"Paper-trading pipeline (UN Comtrade), {window_label}"
        return f"UN Comtrade, {window_label}"
    return "IEA/USDA/USGS 2023 estimates (static fallback — live fetch unavailable)"


def trade_flows_for_commodity(commodity_name: str) -> pd.DataFrame:
    """Long-form DataFrame: reporter_iso3, reporter_name, flow, trade_usd."""
    flows, _period, _source = _live_flows(commodity_name)
    if not flows.empty:
        return flows
    return _static_trade_flows_for_commodity(commodity_name)


def top_traders(commodity_name: str, top_n: int = 15) -> dict:
    """{"exporters": df, "importers": df, "period": "YYYYMM"|None} sorted
    by trade_usd descending. `period` is the live TTM window's end month
    (pipeline or Comtrade API, whichever served it), or None when this
    fell back to the static table (bilateral breakdown — see
    data.comtrade.bilateral_export_partners_batch — only makes sense for
    a real period regardless of source, so callers should treat
    period=None as "no bilateral data")."""
    flows, period, _source = _live_flows(commodity_name)
    if not flows.empty:
        exporters = (
            flows[flows["flow"] == "Export"]
            .sort_values("trade_usd", ascending=False).head(top_n).reset_index(drop=True)
        )
        importers = (
            flows[flows["flow"] == "Import"]
            .sort_values("trade_usd", ascending=False).head(top_n).reset_index(drop=True)
        )
        return {"exporters": exporters, "importers": importers, "period": period}
    result = _static_top_traders(commodity_name, top_n)
    result["period"] = None
    return result


def net_positions(commodity_name: str) -> pd.DataFrame:
    """Net trade position per country (exports − imports). Positive = net exporter."""
    flows, _period, _source = _live_flows(commodity_name)
    if flows.empty:
        return _static_net_positions(commodity_name)

    pivot = flows.pivot_table(
        index=["reporter_iso3", "reporter_name"],
        columns="flow",
        values="trade_usd",
        aggfunc="sum",
    ).fillna(0).reset_index()
    pivot.columns.name = None

    if "Export" not in pivot.columns:
        pivot["Export"] = 0
    if "Import" not in pivot.columns:
        pivot["Import"] = 0

    pivot["net_usd"] = pivot["Export"] - pivot["Import"]
    return pivot.sort_values("net_usd", ascending=False)
