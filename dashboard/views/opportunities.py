"""
Opportunities tab, the confluence screener this whole board exists for.

Real commodity-currency exposure is many-to-many, not one pick per
commodity: gold links to the Australian Dollar, the South African Rand,
the Canadian Dollar, and the Swiss Franc all at once, because all four are
real gold-exporting or gold-linked economies; the Australian Dollar in
turn links to gold, silver, copper, natural gas, AND wheat. So this screens
every (commodity, currency) pair drawn from COMMODITY_FX_GROUPS, the
curated real economic linkages, not a single "most correlated" match,
which tends to surface statistical noise (Copper pairing with the
Ukrainian Hryvnia over the Chilean Peso) over the actual exporter
relationship.

For each pair, flag a "confluence" only when three independent signals
all agree: the commodity's own volatility is elevated (the move is real,
not chop), the currency is actually moving the way the correlation
predicts (the relationship is holding, not breaking), and news sentiment
points the same direction as the price move (the narrative backs it up).
All three have to line up, any one alone is not a signal.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import dash_bootstrap_components as dbc
from dash import dcc, html

from config import ALERT_THRESHOLDS, NAMES, UI_GREEN, UI_MUTED, UI_RED, UI_TEXT
from data.fx import CURRENCY_PAIRS, COMMODITY_FX_GROUPS
from data.processor import historical_volatility, price_returns

VOL_RATIO_THRESHOLD = 0.85   # HV30 / alert threshold
CORR_STRENGTH_THRESHOLD = 0.30  # |correlation| needed to call the link "real"
MOVE_EPSILON = 0.1  # % move below which a direction doesn't count as directional

# Currencies whose only data source (Yahoo Finance, no TradingView symbol
# exists for these) is thin enough that a real chunk of daily closes are
# just the prior day's value repeated, not a fresh live quote. Checked
# directly: over a trailing 30-day sample, the Congolese Franc repeated
# ~53% of days and the Ghanaian Cedi ~13%, both look more like an
# occasionally-refreshed fixing rate than a live market feed. That flattens
# their measured volatility/correlation below what the real relationship
# probably is, so they're marked in the table rather than presented at
# face value.
THIN_DATA_CURRENCIES = {"COD", "GHA"}


def layout() -> html.Div:
    return html.Div([
        html.P(
            "A commodity is flagged only when three independent signals agree: "
            "its own volatility is elevated, its historically-linked currency is "
            "moving the way that relationship predicts, and news sentiment points "
            "the same direction as the price move. Any one alone is not a signal.",
            # text-muted is dropped rather than overridden: it sets colour
            # with !important, which beats an inline style.
            className="small mb-3",
            style={"color": UI_TEXT},
        ),
        html.Div(id="opportunity-board"),
        # The board's own Inputs (price/FX/correlation/sentiment stores) are
        # all populated by callbacks that ran before this tab was ever
        # opened, so nothing here is guaranteed to "change" the moment this
        # div mounts. This interval is a fresh, self-contained trigger that
        # fires right after mount (and periodically while data is still
        # arriving), so the board reliably renders instead of staying blank
        # until some unrelated store happens to update again later.
        dcc.Interval(id="opportunity-poll", interval=2000, n_intervals=0),
    ])


def _sign(x: float, epsilon: float = MOVE_EPSILON) -> int:
    if pd.isna(x) or abs(x) < epsilon:
        return 0
    return 1 if x > 0 else -1


def build_opportunity_board(
    prices: pd.DataFrame,
    fx_prices: pd.DataFrame,
    fx_corr: pd.DataFrame,
    sentiment: dict,
) -> html.Div:
    hv30 = historical_volatility(prices, [30])[30].iloc[-1]
    commodity_returns = price_returns(prices)
    fx_returns = price_returns(fx_prices) if not fx_prices.empty else pd.DataFrame()
    iso3_to_name = {k: v["name"] for k, v in CURRENCY_PAIRS.items()}

    rows = []
    flagged_count = 0
    thin_data_used = False

    for name in NAMES:
        if name not in fx_corr.columns or fx_corr[name].dropna().empty:
            continue

        corr_col = fx_corr[name].dropna()
        # Every currency this commodity is actually economically linked to
        # (its real exporter/producer economies), a commodity can have
        # several at once (gold: AUD, ZAR, CAD, CHF), and the same currency
        # can show up under several commodities (AUD also under silver,
        # copper, natural gas, wheat). Falls back to the single strongest
        # statistical match only when no curated group exists for a name.
        candidates = [iso3 for iso3 in COMMODITY_FX_GROUPS.get(name, []) if iso3 in corr_col.index]
        if not candidates:
            candidates = corr_col.abs().sort_values(ascending=False).index[:1].tolist()

        for iso3 in candidates:
            corr_value = corr_col.loc[iso3]
            currency_name = iso3_to_name.get(iso3, iso3)
            if iso3 in THIN_DATA_CURRENCIES:
                thin_data_used = True
                currency_name += " *"

            commodity_5d = commodity_returns.at["5d", name] if "5d" in commodity_returns.index else np.nan
            fx_5d = (
                fx_returns.at["5d", currency_name]
                if not fx_returns.empty and currency_name in fx_returns.columns and "5d" in fx_returns.index
                else np.nan
            )

            commodity_dir = _sign(commodity_5d)
            fx_dir = _sign(fx_5d)
            expected_fx_dir = int(np.sign(corr_value)) * commodity_dir if commodity_dir != 0 else 0
            relationship_holds = (
                abs(corr_value) >= CORR_STRENGTH_THRESHOLD
                and commodity_dir != 0
                and fx_dir != 0
                and fx_dir == expected_fx_dir
            )

            threshold = ALERT_THRESHOLDS.get(name, 999)
            hv = hv30.get(name, np.nan)
            vol_ratio = hv / threshold if threshold else np.nan
            vol_elevated = not np.isnan(vol_ratio) and vol_ratio >= VOL_RATIO_THRESHOLD

            sentiment_score = sentiment.get(name, 0.0)
            sentiment_dir = _sign(sentiment_score, epsilon=0.5)
            sentiment_aligned = commodity_dir != 0 and sentiment_dir == commodity_dir

            confluence = vol_elevated and relationship_holds and sentiment_aligned
            if confluence:
                flagged_count += 1

            call = "Bullish" if commodity_dir > 0 else ("Bearish" if commodity_dir < 0 else "-")
            call_color = UI_GREEN if commodity_dir > 0 else (UI_RED if commodity_dir < 0 else UI_MUTED)

            rows.append(html.Tr([
                html.Td(name),
                html.Td(currency_name, className="text-muted"),
                html.Td(
                    f"{'+' if commodity_5d > 0 else ''}{commodity_5d:.2f}%" if not np.isnan(commodity_5d) else "-",
                    style={"textAlign": "right"},
                ),
                html.Td(
                    f"{vol_ratio*100:.0f}%" if not np.isnan(vol_ratio) else "-",
                    style={"textAlign": "right", "color": call_color if vol_elevated else "inherit"},
                ),
                html.Td(f"{corr_value:+.2f}", style={"textAlign": "right"}),
                html.Td(
                    "Holding" if relationship_holds else "-",
                    style={"textAlign": "center", "color": UI_GREEN if relationship_holds else UI_MUTED},
                ),
                html.Td(
                    f"{sentiment_score:+.1f}",
                    style={"textAlign": "right", "color": UI_GREEN if sentiment_aligned else UI_MUTED},
                ),
                html.Td(call, style={"textAlign": "center", "color": call_color}),
                html.Td(
                    dbc.Badge("CONFLUENCE", color="warning") if confluence
                    else dbc.Badge("-", color="secondary"),
                    style={"textAlign": "center"},
                ),
            ], className="row-flash" if confluence else "", style={"background": "var(--accent-dim)"} if confluence else {}))

    # Headers carry their column's alignment. Bare html.Th left-aligns,
    # which left every heading here sitting off to the side of the figures
    # it named.
    header = html.Thead(html.Tr([
        html.Th(label, style={"textAlign": align})
        for label, align in [
            ("Commodity", "left"),
            ("Currency", "left"),
            ("5D Move", "right"),
            ("Vol vs Threshold", "right"),
            ("Correlation", "right"),
            ("Relationship", "center"),
            ("Sentiment", "right"),
            ("Call", "center"),
            ("Signal", "center"),
        ]
    ]))

    table = dbc.Table([header, html.Tbody(rows)], hover=True, responsive=True, size="sm",
                       style={"fontSize": "0.85rem"})

    summary = (
        dbc.Alert(
            f"{flagged_count} confluence signal{'s' if flagged_count != 1 else ''}: "
            "volatility, correlation, and sentiment all pointing the same way.",
            color="warning", className="py-2 mb-3",
        )
        if flagged_count > 0
        else dbc.Alert("No confluence. Nothing has all three signals aligned.",
                        color="success", className="py-2 mb-3")
    )

    pending_note = html.Div()
    scored = len(sentiment)
    if scored < len(NAMES):
        pending_note = html.Div(
            f"Sentiment scored for {scored} of {len(NAMES)} commodities. "
            "The rest read as neutral (0.0) until their news fetch "
            "completes.",
            className="text-muted small mb-2",
        )

    thin_data_note = html.Div()
    if thin_data_used:
        thin_data_note = html.Div(
            "* No live TradingView feed exists for this currency, and its Yahoo "
            "Finance data is thin. A large share of daily closes repeat the "
            "prior day's value rather than reflecting a fresh live quote. Treat "
            "its correlation/volatility numbers here as directionally suggestive, "
            "not precise.",
            className="text-muted small mt-2",
        )

    return html.Div([pending_note, summary, table, thin_data_note])
