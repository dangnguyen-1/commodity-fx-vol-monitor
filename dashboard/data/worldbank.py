"""
World Bank API, country-level economic and trade indicators.
No API key required. Data is cached to disk for 24 hours.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_WB_BASE = "https://api.worldbank.org/v2"
_CACHE_FILE = Path("cache/worldbank_indicators.json")
_CACHE_TTL_SECONDS = 86_400  # 24 hours

# Indicators we pull per country
INDICATORS = {
    "gdp_usd":               "NY.GDP.MKTP.CD",          # GDP in current USD
    "merch_exports_usd":     "TX.VAL.MRCH.CD.WT",        # Merchandise exports USD
    "merch_imports_usd":     "TM.VAL.MRCH.CD.WT",        # Merchandise imports USD
    "fuel_exports_pct":      "TX.VAL.FUEL.ZS.UN",        # Fuel exports % of merch exports
    "fuel_imports_pct":      "TM.VAL.FUEL.ZS.UN",        # Fuel imports % of merch imports
    "agri_exports_pct":      "TX.VAL.AGRI.ZS.UN",        # Agri raw materials exports %
    "agri_imports_pct":      "TM.VAL.AGRI.ZS.UN",        # Agri raw materials imports %
    "metal_exports_pct":     "TX.VAL.MMTL.ZS.UN",        # Ores & metals exports %
    "metal_imports_pct":     "TM.VAL.MMTL.ZS.UN",        # Ores & metals imports %
    "political_stability":   "PV.EST",                    # WGI Political Stability (-2.5 to +2.5)
}


def _fetch_indicator(indicator_code: str) -> dict[str, float]:
    """Fetch latest value for one indicator across all countries."""
    url = (
        f"{_WB_BASE}/country/all/indicator/{indicator_code}"
        f"?format=json&mrv=3&per_page=500"
    )
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        if len(payload) < 2 or not payload[1]:
            return {}
        # mrv=3 returns up to 3 years; take the most recent non-null per country
        latest: dict[str, float] = {}
        for entry in payload[1]:
            iso3 = entry.get("countryiso3code", "")
            val = entry.get("value")
            if len(iso3) == 3 and val is not None and iso3 not in latest:
                latest[iso3] = val
        return latest
    except Exception as exc:
        logger.warning("World Bank fetch failed for %s: %s", indicator_code, exc)
        return {}


def fetch_country_indicators(force_refresh: bool = False) -> pd.DataFrame:
    """
    Return a DataFrame indexed by ISO-3 country code with all INDICATORS as columns,
    plus 'country_name'. Cached to disk for 24 hours.
    """
    if not force_refresh and _CACHE_FILE.exists():
        age = time.time() - _CACHE_FILE.stat().st_mtime
        if age < _CACHE_TTL_SECONDS:
            logger.info("Using cached World Bank data (%.0fh old)", age / 3600)
            df = pd.read_json(_CACHE_FILE, orient="index")
            df.index.name = "iso3"
            return df

    logger.info("Fetching World Bank indicators for all countries…")

    # Fetch country names separately
    names_url = f"{_WB_BASE}/country?format=json&per_page=500"
    names: dict[str, str] = {}
    try:
        r = requests.get(names_url, timeout=30)
        r.raise_for_status()
        payload = r.json()
        if len(payload) >= 2:
            for c in payload[1]:
                if c.get("iso2Code") and len(c.get("id", "")) == 3:
                    names[c["id"]] = c["name"]
    except Exception as exc:
        logger.warning("Could not fetch country names: %s", exc)

    # Collect all indicator values
    all_data: dict[str, dict] = {}
    for key, code in INDICATORS.items():
        values = _fetch_indicator(code)
        for iso3, val in values.items():
            if iso3 not in all_data:
                all_data[iso3] = {"country_name": names.get(iso3, iso3)}
            all_data[iso3][key] = val
        time.sleep(0.2)  # be polite to the API

    df = pd.DataFrame.from_dict(all_data, orient="index")
    df.index.name = "iso3"

    # Persist cache
    _CACHE_FILE.parent.mkdir(exist_ok=True)
    df.to_json(_CACHE_FILE, orient="index")
    logger.info("World Bank data cached (%d countries)", len(df))
    return df


def commodity_exposure_pct(df: pd.DataFrame) -> pd.DataFrame:
    """
    Derive commodity-group exposure metrics from World Bank indicators.

    Returns columns:
      energy_net_pct:   (fuel_exports - fuel_imports) as % of GDP
      agri_net_pct:     (agri_exports - agri_imports) as % of GDP
      metals_net_pct:   (metal_exports - metal_imports) as % of GDP
    """
    out = pd.DataFrame(index=df.index)

    def _col(col: str) -> pd.Series:
        return df[col].fillna(0) if col in df.columns else pd.Series(0.0, index=df.index)

    for group, exp_col, imp_col in [
        ("energy", "fuel_exports_pct",  "fuel_imports_pct"),
        ("agri",   "agri_exports_pct",  "agri_imports_pct"),
        ("metals", "metal_exports_pct", "metal_imports_pct"),
    ]:
        exports_usd = _col("merch_exports_usd") * _col(exp_col) / 100
        imports_usd = _col("merch_imports_usd") * _col(imp_col) / 100
        net_usd = exports_usd - imports_usd
        gdp = _col("gdp_usd").replace(0, float("nan"))
        out[f"{group}_net_pct"] = (net_usd / gdp * 100).round(2)

    out["country_name"] = df.get("country_name", df.index)
    return out
