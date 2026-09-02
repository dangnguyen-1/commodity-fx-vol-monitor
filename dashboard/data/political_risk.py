"""
Political risk scoring per country (1–10 scale, 10 = highest risk).

Combines:
  1. World Bank Political Stability indicator (baseline structural risk)
  2. Active conflicts / sanctions (static list, updated periodically)
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static conflict / sanctions penalties  (adds to base risk score)
# Values represent additional risk points on top of WB baseline.
# Sources: UCDP Armed Conflict Database, OFAC sanctions list, 2024.
# ---------------------------------------------------------------------------
CONFLICT_PENALTIES: dict[str, dict] = {
    "RUS": {"label": "International sanctions + Ukraine war",   "penalty": 4.0},
    "UKR": {"label": "Active conflict (Russia-Ukraine war)",    "penalty": 3.5},
    "IRN": {"label": "Nuclear sanctions + US/EU restrictions",  "penalty": 3.5},
    "SYR": {"label": "Active civil conflict",                   "penalty": 4.0},
    "YEM": {"label": "Active civil war",                        "penalty": 4.0},
    "LBY": {"label": "Political instability + factional conflict", "penalty": 3.0},
    "SDN": {"label": "Active civil conflict (2023–)",           "penalty": 3.5},
    "IRQ": {"label": "Political instability + militia activity","penalty": 2.5},
    "NGA": {"label": "Boko Haram + Niger Delta instability",    "penalty": 2.0},
    "VEN": {"label": "US sanctions + economic crisis",          "penalty": 2.5},
    "PRK": {"label": "Heavy sanctions (nuclear programme)",     "penalty": 5.0},
    "BLR": {"label": "EU/US sanctions (post-2020 election)",    "penalty": 2.5},
    "MMR": {"label": "Military coup + civil conflict (2021–)",  "penalty": 3.0},
    "AFG": {"label": "Taliban rule + international isolation",  "penalty": 4.0},
    "ETH": {"label": "Tigray conflict / political instability", "penalty": 2.5},
    "MLI": {"label": "Military junta + jihadist insurgency",    "penalty": 3.0},
    "BFA": {"label": "Jihadist insurgency + military junta",    "penalty": 3.0},
    "NER": {"label": "Military coup (2023)",                    "penalty": 2.5},
    "SOM": {"label": "Ongoing civil conflict",                  "penalty": 3.5},
    "COD": {"label": "Eastern DRC conflict",                    "penalty": 2.5},
    "HTI": {"label": "Gang violence + political crisis",        "penalty": 3.0},
    "PSE": {"label": "Active conflict (Gaza 2023–)",            "penalty": 4.0},
    "ISR": {"label": "Regional conflict exposure (2023–)",      "penalty": 2.0},
}

# Commodity-specific geopolitical sensitivities: which countries matter most
# for each commodity's price stability (weight for risk aggregation)
COMMODITY_COUNTRY_WEIGHTS: dict[str, dict[str, float]] = {
    "WTI Crude":   {"SAU": 0.20, "RUS": 0.15, "IRQ": 0.10, "USA": 0.12, "IRN": 0.08,
                    "UAE": 0.07, "KWT": 0.06, "NGA": 0.05, "LBY": 0.04, "VEN": 0.04},
    "Brent Crude": {"SAU": 0.20, "RUS": 0.15, "IRQ": 0.10, "NOR": 0.08, "IRN": 0.08,
                    "UAE": 0.07, "KWT": 0.06, "NGA": 0.05, "LBY": 0.04, "DZA": 0.03},
    "Natural Gas": {"RUS": 0.25, "QAT": 0.15, "USA": 0.10, "NOR": 0.10, "AUS": 0.08,
                    "DZA": 0.06, "NGA": 0.05, "TKM": 0.04, "IRN": 0.05, "MYS": 0.04},
    "Gold":        {"CHN": 0.12, "AUS": 0.10, "RUS": 0.10, "USA": 0.08, "CAN": 0.08,
                    "GHA": 0.07, "ZAF": 0.07, "UZB": 0.05, "MEX": 0.05, "PNG": 0.04},
    "Silver":      {"MEX": 0.20, "PER": 0.15, "CHN": 0.12, "CHL": 0.10, "RUS": 0.08,
                    "POL": 0.08, "AUS": 0.07, "BOL": 0.06, "ARG": 0.05, "KAZ": 0.04},
    "Copper":      {"CHL": 0.28, "PER": 0.12, "COD": 0.10, "AUS": 0.08, "ZMB": 0.07,
                    "RUS": 0.06, "KAZ": 0.05, "POL": 0.05, "USA": 0.05, "MEX": 0.04},
    "Wheat":       {"RUS": 0.20, "UKR": 0.15, "USA": 0.10, "CAN": 0.10, "AUS": 0.09,
                    "ARG": 0.07, "KAZ": 0.06, "FRA": 0.06, "DEU": 0.04, "ROM": 0.03},
    "Corn":        {"USA": 0.25, "BRA": 0.18, "ARG": 0.12, "UKR": 0.10, "IND": 0.06,
                    "ZAF": 0.04, "HUN": 0.04, "ROM": 0.04, "RUS": 0.03, "SRB": 0.02},
    "Soybeans":    {"BRA": 0.35, "USA": 0.25, "ARG": 0.15, "PRY": 0.06, "CAN": 0.05,
                    "URY": 0.04, "UKR": 0.03, "BOL": 0.03, "RUS": 0.02, "IND": 0.02},
}


def country_risk_scores(wb_df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute a 1–10 political risk score per country.

    Uses World Bank Political Stability (WGI) indicator:
      PV.EST ranges from -2.5 (most unstable) to +2.5 (most stable)

    Falls back to a neutral score of 5 if WB data is unavailable.
    Returns DataFrame with columns: [risk_score, risk_label, conflict_note]
    """
    records = []

    # World Bank political stability indicator (if available in wb_df)
    wb_stability = {}
    if "political_stability" in wb_df.columns:
        wb_stability = wb_df["political_stability"].dropna().to_dict()

    all_iso3 = set(wb_df.index.tolist()) | set(CONFLICT_PENALTIES.keys())

    for iso3 in all_iso3:
        # Base score from WB (−2.5→+2.5 mapped to 1→9)
        wb_val = wb_stability.get(iso3)
        if wb_val is not None:
            base = 5.0 - (float(wb_val) / 2.5) * 4.0  # +2.5→1, −2.5→9
        else:
            base = 5.0

        # Conflict/sanctions penalty
        conflict = CONFLICT_PENALTIES.get(iso3, {})
        penalty = conflict.get("penalty", 0.0)

        raw = min(base + penalty, 10.0)
        score = max(1.0, round(raw, 1))

        records.append({
            "iso3": iso3,
            "risk_score": score,
            "risk_label": _risk_label(score),
            "conflict_note": conflict.get("label", ""),
        })

    df = pd.DataFrame(records).set_index("iso3")
    return df


def commodity_geopolitical_risk(
    country_risks: pd.DataFrame,
    commodity: str,
) -> float:
    """
    Weighted-average political risk score for a commodity (1–10).
    Uses supply-chain country weights from COMMODITY_COUNTRY_WEIGHTS.
    """
    weights = COMMODITY_COUNTRY_WEIGHTS.get(commodity, {})
    if not weights:
        return 5.0

    total_w, weighted_sum = 0.0, 0.0
    for iso3, w in weights.items():
        score = country_risks["risk_score"].get(iso3)
        if score is not None:
            weighted_sum += float(score) * w
            total_w += w

    if total_w == 0:
        return 5.0
    return round(weighted_sum / total_w, 1)


def _risk_label(score: float) -> str:
    if score <= 2.5:  return "Very Low"
    if score <= 4.0:  return "Low"
    if score <= 5.5:  return "Moderate"
    if score <= 7.0:  return "High"
    if score <= 8.5:  return "Very High"
    return "Extreme"


def risk_color(score: float) -> str:
    """Return a CSS color string for the risk score, on the board's own scale."""
    if score <= 2.5:  return "#34D399"   # green
    if score <= 4.0:  return "#8FBF8F"   # sage
    if score <= 5.5:  return "#F2A93B"   # amber (board accent)
    if score <= 7.0:  return "#E8873B"   # deep amber
    if score <= 8.5:  return "#FB5B6E"   # red
    return "#8f1d2a"                     # dark red
