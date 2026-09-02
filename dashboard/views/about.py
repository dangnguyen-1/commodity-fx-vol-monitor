"""About tab: what the monitor is, what each tab holds, where data comes from.

Last in the tab order on purpose. A reader who has worked left to right has
already seen the argument; this is the reference they come back to, and the
first thing a visitor opens when they want to know what they are looking at
before clicking anything.

Every count on this page is computed from config rather than written down.
Hardcoded figures in this project have gone stale twice already, describing
"9 commodities" and "14 currency pairs" long after both had changed, so
nothing here states a number it cannot derive.
"""

from __future__ import annotations

from dash import html
import dash_bootstrap_components as dbc

from config import COMMODITIES, UI_BORDER, UI_MUTED, UI_PANEL, UI_TEXT
from data.fx import COMMODITY_FX_GROUPS, CURRENCY_PAIRS


TAB_GUIDE: list[tuple[str, str]] = [
    (
        "Volatility",
        "Annualised realised volatility over 30, 60 and 90 days for every "
        "commodity, with the board tiles above showing the latest price, "
        "daily move and HV30.",
    ),
    (
        "Returns & Trends",
        "Returns over 1, 5 and 30 days against the 20 and 50 day moving "
        "averages, with a trend read of up, down or flat.",
    ),
    (
        "Alerts",
        "Which commodities are above their own volatility threshold. Each "
        "threshold is roughly 1.25 times that contract's full-sample "
        "annualised volatility, so it reflects what is unusual for that "
        "commodity rather than one number applied to all of them.",
    ),
    (
        "Currencies",
        "The same price and volatility treatment for the currencies, which "
        "are tracked as instruments in their own right rather than only as "
        "the other leg of a pair.",
    ),
    (
        "Correlation",
        "Correlation, beta and R-squared between each commodity and its "
        "linked currencies, plus a five-year rolling view. The rolling "
        "chart is the point: a single snapshot cannot show a relationship "
        "strengthening or decaying.",
    ),
    (
        "Trade Flows",
        "Ranked exporters and importers per commodity from UN Comtrade. "
        "Clicking a country switches to its own bilateral partners.",
    ),
    (
        "Country Exposure",
        "A world map of net trade position as a share of GDP, and the "
        "modelled effect of a 10% price shock on each country's trade "
        "balance.",
    ),
    (
        "Risk & News",
        "A composite country-risk score, and classified commodity "
        "headlines with a direction and a sentiment score per asset.",
    ),
    (
        "Opportunities",
        "The synthesis, and the strictest view here. A commodity is "
        "flagged only when three independent signals agree: elevated "
        "volatility, its linked currency moving the way the relationship "
        "predicts, and news sentiment pointing the same way. Any one alone "
        "is not a signal.",
    ),
]

SOURCES: list[tuple[str, str, str]] = [
    (
        "TradingView",
        "Prices",
        "Daily bars back to January 2010 for every commodity and currency "
        "here, plus a live one-minute feed. Futures arrive on a measured 10 "
        "to 11 minute delay under this data entitlement; spot FX is real "
        "time.",
    ),
    (
        "UN Comtrade",
        "Trade flows",
        "Monthly exports and imports by country and commodity since 2010, "
        "which is what decides which currency is paired with which "
        "commodity rather than correlation alone.",
    ),
    (
        "Reuters, Bloomberg, Investing.com",
        "News",
        "Headlines collected continuously from published RSS feeds.",
    ),
    (
        "OpenAI",
        "News classification",
        "Each headline is read into the assets it affects, a direction and "
        "a sentiment score. This is what makes a story like an attack on "
        "shipping legible as a crude signal, which keyword matching cannot "
        "do.",
    ),
    (
        "World Bank",
        "Country indicators",
        "GDP and governance indicators behind the exposure and risk views.",
    ),
]


def _section(title: str, body) -> dbc.Card:
    return dbc.Card(
        dbc.CardBody([
            html.H6(title, className="section-title mb-3"),
            body,
        ]),
        style={"background": UI_PANEL, "border": f"1px solid {UI_BORDER}"},
        className="mb-3",
    )


def _definition_rows(rows: list[tuple[str, str]]) -> html.Div:
    return html.Div([
        dbc.Row(
            [
                dbc.Col(
                    html.Div(name, style={"color": UI_TEXT, "fontWeight": 600}),
                    md=3,
                ),
                dbc.Col(
                    html.Div(text, style={"color": UI_MUTED}),
                    md=9,
                ),
            ],
            className="mb-2 pb-2",
            style={"borderBottom": f"1px solid {UI_BORDER}"},
        )
        for name, text in rows
    ], style={"fontSize": "0.85rem"})


def layout() -> html.Div:
    commodity_count = len(COMMODITIES)
    currency_count = len(CURRENCY_PAIRS)
    pair_count = sum(len(v) for v in COMMODITY_FX_GROUPS.values())

    thesis = html.Div([
        html.P(
            "Commodity exporters and importers are exposed to the prices of "
            "what they trade. When a commodity moves, the currency of a "
            "country that depends on it has reason to move too, in opposite "
            "directions for a seller and a buyer. This monitor tracks that "
            "relationship across "
            f"{commodity_count} commodities and {currency_count} currencies, "
            f"in {pair_count} pairs built from trade data rather than from "
            "correlation alone.",
            style={"color": UI_MUTED},
        ),
        html.P(
            "Pairings come from measured net exports: a currency appears "
            "against a commodity because its country genuinely leads in "
            "trading it, not because the two happened to correlate. Daily "
            "history runs from January 2010.",
            style={"color": UI_MUTED, "marginBottom": 0},
        ),
    ], style={"fontSize": "0.88rem"})

    limits = html.Div([
        html.P(
            "Correlation here is measurement, not a claim of causation, and "
            "none of it is a trade recommendation. Several tracked "
            "currencies are thinly traded, so their daily closes repeat "
            "often and their statistics are weaker than the liquid ones. "
            "News classification began mid-2026, so it covers recent "
            "activity only and cannot be compared against the price history.",
            style={"color": UI_MUTED, "marginBottom": 0},
        ),
    ], style={"fontSize": "0.85rem"})

    return html.Div([
        _section("What this monitor does", thesis),
        _section("The tabs", _definition_rows(TAB_GUIDE)),
        _section(
            "Where the data comes from",
            _definition_rows([
                (name, f"{role}. {detail}") for name, role, detail in SOURCES
            ]),
        ),
        _section("What it does not claim", limits),
    ], className="mt-2")
