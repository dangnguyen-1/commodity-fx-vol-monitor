"""
Aggregate trade totals from the pipeline's UN Comtrade feed
(data_collector/fundamental_data/, via api/app.py's
/trade-data route) — real, pre-aggregated per-country monthly data with no
rate limits, unlike the free public Comtrade API data/comtrade.py falls
back to.

This only covers aggregate totals (each country's exports/imports to the
world) — his fundamental_trade_data table has no partner-country column,
so bilateral (country-to-country) detail for the Trade Flows tab's zoom
view still comes from data.comtrade.bilateral_export_partners_batch /
bilateral_import_partners_batch, unchanged.
"""

from __future__ import annotations

import logging

import pandas as pd
import requests

from config import PIPELINE_API_BASE_URL

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 15
_TTM_MONTHS = 12  # matches data.comtrade's trailing-12-month window


def fetch_trade_flows(comtrade_commodity: str) -> tuple[pd.DataFrame, str | None]:
    """
    Trailing-12-month sum of export/import totals per country, from the
    pipeline. Returns (DataFrame, end_period) — same shape
    data.comtrade.trade_flows_for_commodity returns (columns
    reporter_iso3, reporter_name, flow, trade_usd; end_period is a
    "YYYYMM" string usable with data.comtrade.format_ttm_label), so
    callers can't tell which source served them. Empty DataFrame + None
    on failure, so callers fall back the same way they already do for a
    static-table miss.
    """
    try:
        resp = requests.get(
            f"{PIPELINE_API_BASE_URL}/trade-data",
            params={"commodity": comtrade_commodity},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
    except Exception as exc:
        logger.warning("Pipeline trade-data fetch failed for %s: %s", comtrade_commodity, exc)
        return pd.DataFrame(), None

    if not items:
        return pd.DataFrame(), None

    df = pd.DataFrame(items)
    periods = sorted(df["period"].unique(), reverse=True)
    end_period = periods[0]
    window = set(periods[:_TTM_MONTHS])
    windowed = df[df["period"].isin(window)]

    grouped = windowed.groupby("country", as_index=False)[["exports_usd", "imports_usd"]].sum(min_count=1)

    records = []
    for row in grouped.itertuples():
        if row.exports_usd and row.exports_usd > 0:
            records.append({
                "reporter_iso3": row.country, "reporter_name": row.country,
                "flow": "Export", "trade_usd": float(row.exports_usd),
            })
        if row.imports_usd and row.imports_usd > 0:
            records.append({
                "reporter_iso3": row.country, "reporter_name": row.country,
                "flow": "Import", "trade_usd": float(row.imports_usd),
            })

    if not records:
        return pd.DataFrame(), None
    return pd.DataFrame(records), end_period
