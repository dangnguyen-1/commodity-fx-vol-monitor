"""
UN Comtrade API — live bilateral trade flows by commodity.

Uses the free public "preview" endpoint (no API key required). Two bugs
in the original version of this file meant it never actually returned
data (year was placed in the URL path instead of passed as the `period`
query param, and reporter countries were passed as ISO3 letters instead
of Comtrade's numeric UN M49 codes) — the app fell back to
`data/trade_data.py`'s static 2023 estimate table instead, silently.

Getting a single clean total per country+flow also requires two more
query params Comtrade doesn't default to zero: `partner2Code` (a
secondary "country of consignment" breakdown) and `motCode` (mode of
transport) both have to be pinned to "0" (world/all), or the same
country+flow comes back as a dozen partial rows instead of one.

Pulls monthly data (not annual) and sums a trailing-12-month (TTM)
window per country, rather than the last full calendar year. Comtrade's
finalized annual figures lag real time by well over a year; monthly
figures for major reporters (the US among them) are typically only
1-2 months behind. Not every country reports monthly on the same
cadence, though — some (Saudi Arabia among them) only publish monthly
data with several months' extra lag versus faster reporters. Rather
than forcing every country into a shared "most recent month" cutoff
(which would silently understate laggard reporters), the TTM window is
picked once from whichever recent month has data for the commodity's
country roster as a whole, and each country's 12-month sum uses
whatever of those specific months it has actually published — the same
"missing months means less counted, not guessed at" honesty already
used for bilateral partner detail below.

The preview endpoint allows exactly one `period` per request but does
allow multiple `reporterCode` values in one call, so a TTM window costs
~12 requests total per commodity (one per month, covering every country
in that commodity's roster at once) rather than 12 times the country
count.

Results are cached to disk per (HS code, month) for 7 days — a given
month's published figures rarely change once they've appeared.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_BASE = "https://comtradeapi.un.org/public/v1/preview/C/M/HS"
_REPORTERS_URL = "https://comtradeapi.un.org/files/v1/app/reference/Reporters.json"
_CACHE_DIR = Path("cache/comtrade")
_CACHE_TTL = 7 * 86_400        # 7 days — a published month's figures rarely change
_REPORTERS_CACHE_TTL = 30 * 86_400  # country code list barely ever changes
_REQUEST_TIMEOUT = 12
_MAX_RETRIES = 2
_RETRY_BACKOFF = 3  # seconds, doubled each retry

_TTM_MONTHS = 12    # trailing-12-month window summed per country
_PROBE_MONTHS = 18  # how far back to look for the window's most recent month
_MONTH_ABBR = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Individual commodity → HS code (for fine-grained lookups)
COMMODITY_HS: dict[str, str] = {
    "WTI Crude":   "2709",
    "Brent Crude": "2709",
    "Natural Gas": "2711",
    "Gold":        "7108",
    "Silver":      "7106",
    "Copper":      "7403",
    "Wheat":       "1001",
    "Corn":        "1005",
    "Soybeans":    "1201",
}

# Candidate reporter countries per commodity — the same real-world
# exporter/importer roster curated in data/trade_data.py, just used here
# to scope which countries are worth querying live rather than querying
# all ~250 Comtrade reporters for every commodity.
COMMODITY_COUNTRIES: dict[str, list[str]] = {
    "WTI Crude": [
        "SAU", "RUS", "IRQ", "USA", "UAE", "CAN", "NOR", "KWT", "KAZ", "NGA",
        "BRA", "LBY", "AGO", "MEX", "DZA", "CHN", "IND", "KOR", "JPN", "DEU",
        "NLD", "ITA", "SGP", "THA", "GBR", "FRA", "TUR", "ESP", "IDN",
    ],
    "Brent Crude": [
        "SAU", "RUS", "IRQ", "USA", "UAE", "CAN", "NOR", "KWT", "KAZ", "NGA",
        "CHN", "IND", "KOR", "JPN", "DEU", "NLD", "ITA", "SGP", "GBR",
    ],
    "Natural Gas": [
        "USA", "RUS", "QAT", "AUS", "NOR", "CAN", "DZA", "NGA", "MYS", "TKM",
        "CHN", "JPN", "DEU", "KOR", "ITA", "FRA", "GBR", "TUR", "IND", "NLD",
    ],
    "Gold": [
        "CHN", "AUS", "RUS", "CAN", "GHA", "ZAF", "USA", "UZB", "MEX", "PNG",
        "CHE", "GBR", "IND", "HKG", "ARE", "SGP", "TUR", "THA",
    ],
    "Silver": [
        "MEX", "PER", "CHN", "CHL", "POL", "RUS", "AUS", "BOL", "ARG", "KAZ",
        "IND", "GBR", "USA", "DEU", "KOR", "ITA", "CAN", "BEL",
    ],
    "Copper": [
        "CHL", "PER", "COD", "AUS", "ZMB", "RUS", "KAZ", "POL", "USA", "MEX",
        "CHN", "JPN", "KOR", "DEU", "ITA", "IND", "BEL", "TWN", "TUR",
    ],
    "Wheat": [
        "RUS", "AUS", "CAN", "USA", "UKR", "ARG", "KAZ", "FRA", "DEU", "ROM",
        "EGY", "IDN", "TUR", "NGA", "BGD", "PAK", "BRA", "MEX", "IRN", "PHL",
    ],
    "Corn": [
        "USA", "BRA", "ARG", "UKR", "IND", "ZAF", "HUN", "ROM", "RUS", "SRB",
        "CHN", "MEX", "JPN", "KOR", "VNM", "IRN", "EGY", "IDN", "COL", "ESP",
    ],
    "Soybeans": [
        "BRA", "USA", "ARG", "PRY", "CAN", "URY", "UKR", "BOL", "RUS", "IND",
        "CHN", "ARG", "NLD", "MEX", "EGY", "JPN", "THA", "DEU", "IDN", "TUR",
    ],
}


def _cache_path(key: str) -> Path:
    return _CACHE_DIR / f"{key}.json"


def _read_cache(key: str, ttl: float = _CACHE_TTL) -> list[dict] | None:
    path = _cache_path(key)
    if path.exists() and (time.time() - path.stat().st_mtime) < ttl:
        with open(path) as f:
            return json.load(f)
    return None


def _write_cache(key: str, data) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_cache_path(key), "w") as f:
        json.dump(data, f)


def _get_with_retry(url: str, params: dict) -> requests.Response | None:
    delay = _RETRY_BACKOFF
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=_REQUEST_TIMEOUT)
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", delay))
                logger.warning("Comtrade rate-limited, retrying in %.0fs", retry_after)
                time.sleep(retry_after)
                delay *= 2
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            if attempt == _MAX_RETRIES:
                logger.warning("Comtrade request failed after retries: %s", exc)
                return None
            time.sleep(delay)
            delay *= 2
    return None


def _reporter_code_map() -> dict[str, int]:
    """ISO3 -> Comtrade numeric reporterCode, from Comtrade's own official
    reference file (never hand-maintained — a typo in a hand-built
    ISO3→numeric table would silently query the wrong country)."""
    cached = _read_cache("reporters", ttl=_REPORTERS_CACHE_TTL)
    if cached is None:
        resp = _get_with_retry(_REPORTERS_URL, {})
        if resp is None:
            cached = _read_cache("reporters", ttl=float("inf"))  # any stale cache beats nothing
            if cached is None:
                return {}
        else:
            cached = resp.json().get("results", [])
            _write_cache("reporters", cached)
    # Comtrade's reference file also lists 3 regional aggregate groupings
    # (EU, ASEAN, "Other Asia, nes") alongside real countries. Excluded
    # here: a bilateral partner resolving to "Other Asia, nes" isn't a
    # named destination, it's the same "reporter didn't give us more
    # detail" signal as no bilateral row at all, and should be treated
    # that way rather than shown as if it were a specific country.
    return {
        r["reporterCodeIsoAlpha3"]: r["reporterCode"]
        for r in cached
        if r.get("reporterCodeIsoAlpha3") and not r.get("isGroup")
    }


def _month_str(year: int, month: int) -> str:
    return f"{year:04d}{month:02d}"


def _trailing_months(end_month: str, count: int) -> list[str]:
    """`count` consecutive months ending at (and including) `end_month`,
    most recent first."""
    year, month = int(end_month[:4]), int(end_month[4:6])
    months = []
    for _ in range(count):
        months.append(_month_str(year, month))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return months


def format_ttm_label(end_month: str) -> str:
    """(e.g. "202606" -> "Jul 2025 – Jun 2026 (TTM, live)") for the UI caption."""
    months = _trailing_months(end_month, _TTM_MONTHS)
    start_year, start_month = int(months[-1][:4]), int(months[-1][4:6])
    end_year, end_month_num = int(end_month[:4]), int(end_month[4:6])
    return (
        f"{_MONTH_ABBR[start_month]} {start_year} \u2013 "
        f"{_MONTH_ABBR[end_month_num]} {end_year} (TTM, live)"
    )


def _fetch_flows(hs_code: str, iso3_list: list[str], period: str) -> list[dict]:
    code_map = _reporter_code_map()
    numeric_codes = [str(code_map[iso]) for iso in iso3_list if iso in code_map]
    if not numeric_codes:
        return []

    cache_key = f"hs{hs_code}_{period}_{hash(tuple(sorted(numeric_codes))) & 0xffffffff}"
    cached = _read_cache(cache_key)
    if cached is not None:
        return cached

    resp = _get_with_retry(_BASE, {
        "reporterCode": ",".join(numeric_codes),
        "period": period,
        "cmdCode": hs_code,
        "flowCode": "X,M",
        "partnerCode": "0",
        "partner2Code": "0",
        "motCode": "0",
    })
    if resp is None:
        return []

    rows = resp.json().get("data", [])
    if rows:
        _write_cache(cache_key, rows)
    return rows


def _latest_month_with_data(hs_code: str, iso3_list: list[str]) -> str | None:
    """The most recent month, across this commodity's whole country
    roster, that Comtrade has published anything for — the end of the
    TTM window. Monthly data lags real time by 1-2 months for fast
    reporters, more for others, so probe backward rather than assuming."""
    year, month = date.today().year, date.today().month
    for _ in range(_PROBE_MONTHS):
        period = _month_str(year, month)
        if _fetch_flows(hs_code, iso3_list, period):
            return period
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return None


def trade_flows_for_commodity(commodity_name: str) -> tuple[pd.DataFrame, str | None]:
    """
    Live trailing-12-month trade flows for a commodity. Returns
    (DataFrame, end_month) where end_month is a "YYYYMM" string marking
    the end of the summed window (pass to format_ttm_label() for
    display, or to bilateral_export_partners_batch() to match windows).
    DataFrame columns: reporter_iso3, reporter_name, flow (Export/Import),
    trade_usd. Empty DataFrame + None end_month on failure, so callers
    can fall back to the static estimate table without crashing.
    """
    hs = COMMODITY_HS.get(commodity_name)
    iso3_list = COMMODITY_COUNTRIES.get(commodity_name, [])
    if not hs or not iso3_list:
        return pd.DataFrame(), None

    code_map = _reporter_code_map()
    numeric_to_iso3 = {}
    for iso in iso3_list:
        if iso in code_map:
            numeric_to_iso3[code_map[iso]] = iso

    end_month = _latest_month_with_data(hs, iso3_list)
    if end_month is None:
        return pd.DataFrame(), None

    # Sum each country's own reported months within the window — a
    # reporter that hasn't published its most recent 1-2 months yet
    # (Comtrade's monthly cadence varies a lot by country) contributes
    # less for those months rather than being guessed at or dropped
    # from the window entirely.
    totals: dict[tuple[str, str], float] = {}
    names: dict[str, str] = {}
    for period in _trailing_months(end_month, _TTM_MONTHS):
        rows = _fetch_flows(hs, iso3_list, period)
        # Some reporters (e.g. Nigeria) return both the "C00" grand-total
        # customs code and a specific-regime code (C01/C03/...) carrying
        # the identical value for a single-regime economy — collapse to
        # one value per reporter+flow for this month before accumulating,
        # so a duplicate row can't get summed into the TTM total twice.
        month_totals: dict[tuple[str, str], float] = {}
        for r in rows:
            if r.get("customsCode") != "C00":
                continue
            reporter_num = r.get("reporterCode")
            iso3 = numeric_to_iso3.get(reporter_num)
            if not iso3:
                continue
            value = r.get("primaryValue") or r.get("fobvalue") or 0
            if not value:
                continue
            flow = "Export" if r.get("flowCode") == "X" else "Import"
            month_totals[(iso3, flow)] = value
            names[iso3] = r.get("reporterDesc") or iso3
        for key, value in month_totals.items():
            totals[key] = totals.get(key, 0.0) + value

    if not totals:
        return pd.DataFrame(), None
    records = [
        {"reporter_iso3": iso3, "reporter_name": names[iso3], "flow": flow, "trade_usd": usd}
        for (iso3, flow), usd in totals.items()
    ]
    return pd.DataFrame(records), end_month


def _bilateral_partners_batch(
    commodity_name: str,
    reporter_iso3_list: list[str],
    end_month: str,
    flow_code: str,
    top_n_partners: int,
) -> dict[str, pd.DataFrame]:
    """
    Shared implementation behind bilateral_export_partners_batch (flow_code
    "X" — who a reporter ships to) and bilateral_import_partners_batch
    (flow_code "M" — who a reporter buys from). Same mechanics either way:
    summed over the trailing-12-month window ending at `end_month`, all
    reporters fetched together per month (Comtrade allows multiple
    reporterCode values per call, just one period), so this costs ~12
    requests total regardless of how many reporters are passed in.

    Returns {reporter_iso3: DataFrame[partner_iso3, partner_name,
    trade_usd]}. A reporter that only publishes a world total (no named
    partners) simply has no key in the returned dict, never a fabricated
    row. partnerCode "0" (World) is dropped; only named partner
    countries remain.
    """
    hs = COMMODITY_HS.get(commodity_name)
    code_map = _reporter_code_map()
    numeric_to_iso3 = {v: k for k, v in code_map.items()}
    reporter_nums = [str(code_map[iso]) for iso in reporter_iso3_list if iso in code_map]
    if not hs or not reporter_nums or not end_month:
        return {}

    totals: dict[tuple[str, str], float] = {}  # (reporter_iso3, partner_iso3) -> usd
    partner_names: dict[str, str] = {}

    for period in _trailing_months(end_month, _TTM_MONTHS):
        cache_key = (
            f"bilateral_{flow_code}_hs{hs}_{period}_"
            f"{hash(tuple(sorted(reporter_nums))) & 0xffffffff}"
        )
        rows = _read_cache(cache_key)
        if rows is None:
            resp = _get_with_retry(_BASE, {
                "reporterCode": ",".join(reporter_nums),
                "period": period,
                "cmdCode": hs,
                "flowCode": flow_code,
                "partner2Code": "0",
                "motCode": "0",
            })
            if resp is None:
                continue
            rows = resp.json().get("data", [])
            if rows:
                _write_cache(cache_key, rows)

        month_totals: dict[tuple[str, str], float] = {}
        for r in rows:
            if r.get("customsCode") != "C00":
                continue
            partner_num = r.get("partnerCode")
            if not partner_num:  # 0 == World total, already captured elsewhere
                continue
            reporter_iso3 = numeric_to_iso3.get(r.get("reporterCode"))
            partner_iso3 = numeric_to_iso3.get(partner_num)
            if not reporter_iso3 or not partner_iso3:
                continue
            value = r.get("primaryValue") or r.get("fobvalue") or 0
            if not value:
                continue
            month_totals[(reporter_iso3, partner_iso3)] = value
            partner_names[partner_iso3] = r.get("partnerDesc") or partner_iso3
        for key, value in month_totals.items():
            totals[key] = totals.get(key, 0.0) + value

    by_reporter: dict[str, list[dict]] = {}
    for (reporter_iso3, partner_iso3), usd in totals.items():
        by_reporter.setdefault(reporter_iso3, []).append({
            "partner_iso3": partner_iso3,
            "partner_name": partner_names[partner_iso3],
            "trade_usd": usd,
        })

    return {
        reporter_iso3: (
            pd.DataFrame(records)
            .sort_values("trade_usd", ascending=False)
            .head(top_n_partners)
            .reset_index(drop=True)
        )
        for reporter_iso3, records in by_reporter.items()
    }


def bilateral_export_partners_batch(
    commodity_name: str,
    exporter_iso3_list: list[str],
    end_month: str,
    top_n_partners: int = 5,
) -> dict[str, pd.DataFrame]:
    """Which specific countries each of the given exporters actually ships
    this commodity to. See _bilateral_partners_batch for the mechanics."""
    return _bilateral_partners_batch(
        commodity_name, exporter_iso3_list, end_month, flow_code="X", top_n_partners=top_n_partners,
    )


def bilateral_import_partners_batch(
    commodity_name: str,
    importer_iso3_list: list[str],
    end_month: str,
    top_n_partners: int = 5,
) -> dict[str, pd.DataFrame]:
    """Which specific countries each of the given importers actually buys
    this commodity from. See _bilateral_partners_batch for the mechanics."""
    return _bilateral_partners_batch(
        commodity_name, importer_iso3_list, end_month, flow_code="M", top_n_partners=top_n_partners,
    )


def top_traders(commodity_name: str, top_n: int = 15) -> dict:
    """{"exporters": df, "importers": df, "period": "YYYYMM"|None} — live
    TTM data (period is the window's end month), top_n countries each by
    trade value. Empty frames on failure."""
    flows, period = trade_flows_for_commodity(commodity_name)
    if flows.empty:
        return {"exporters": pd.DataFrame(), "importers": pd.DataFrame(), "period": None}

    exporters = (
        flows[flows["flow"] == "Export"]
        .sort_values("trade_usd", ascending=False)
        .head(top_n)
        .reset_index(drop=True)
    )
    importers = (
        flows[flows["flow"] == "Import"]
        .sort_values("trade_usd", ascending=False)
        .head(top_n)
        .reset_index(drop=True)
    )
    return {"exporters": exporters, "importers": importers, "period": period}


def net_positions(commodity_name: str) -> pd.DataFrame:
    """Net trade position per country (exports - imports, USD). Positive
    = net exporter. Empty DataFrame on failure — no `period` column since
    this shape matches data.trade_data.net_positions for a drop-in swap;
    callers that need the period should use top_traders() instead."""
    flows, _period = trade_flows_for_commodity(commodity_name)
    if flows.empty:
        return pd.DataFrame()

    pivot = flows.pivot_table(
        index=["reporter_iso3", "reporter_name"],
        columns="flow",
        values="trade_usd",
        aggfunc="sum",
    ).fillna(0)

    pivot.columns.name = None
    if "Export" not in pivot.columns:
        pivot["Export"] = 0
    if "Import" not in pivot.columns:
        pivot["Import"] = 0

    pivot["net_usd"] = pivot["Export"] - pivot["Import"]
    pivot = pivot.reset_index()
    return pivot.sort_values("net_usd", ascending=False)
