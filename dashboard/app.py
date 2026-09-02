"""
Commodity Price Volatility Tracker
------------------------------------
Run: python3 app.py
Then open http://localhost:8050 in your browser.

Set DATA_SOURCE in config.py:
  "yahoo"     — free Yahoo Finance data, no API key needed (default)
  "bloomberg" — Bloomberg Terminal via blpapi (Terminal must be running)
  "mock"      — synthetic random-walk data for offline testing

-------------------------------------------------------------------------
DIRECTION CONTRACT (impeccable seed a29f278f, direction:
vernacular-ephemera-boarding-pass-and-gate-board)

THESIS: Track commodities the way an airport tracks flights — a board
that visibly reranks and holds attention on what changed, refusing the
literal Bloomberg-terminal skin the category always ships.

OWN-WORLD: Near-black board (#12141a) + off-white flap text (#EAE7DC).
Big Shoulders Display condensed caps for signage, Martian Mono for every
figure, one amber accent (#F2A93B) reserved for board chrome/alerts.
Gains/losses keep green/red; informational data keeps blue.

STORY: A hiring/technical viewer watches prices flip into place, sees an
alert tile ignite when a threshold breaks, and reads country/trade/risk
context as gate-board-style panels — leaving convinced this is a real
cross-domain instrument, not a chart-library demo.

FIRST VIEWPORT: Board header (title, live dot, mono "updated" readout,
refresh) -> one flip tile per commodity (name/price/return/HV, alert
→ gate-strip tabs → tab content.

FINISH: unreviewed and undocumented is unfinished; this build ends with
the finish review, the verdict, and DESIGN.md.
-------------------------------------------------------------------------
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date

import dash
import dash_bootstrap_components as dbc
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from dash import ALL, Input, Output, State, callback, ctx, dcc, html, no_update

from config import (
    ALERT_THRESHOLDS,
    BBG_TICKERS,
    BBG_TO_NAME,
    COMMODITIES,
    DATA_SOURCE,
    NAMES,
    UI_ACCENT,
    UI_AMBER,
    UI_BG,
    UI_BLUE,
    UI_BORDER,
    UI_GREEN,
    UI_MUTED,
    UI_PANEL,
    UI_RED,
    UI_TEXT,
    YAHOO_TICKERS,
    YAHOO_TO_NAME,
    VOL_WINDOWS,
)
from data.processor import (
    check_alerts,
    correlation_matrix,
    current_vol_summary,
    historical_volatility,
    moving_averages,
    price_returns,
    trend_signal,
)
from data.worldbank import fetch_country_indicators, commodity_exposure_pct
from data.trade_data import net_positions, top_traders, data_source_label
from data.comtrade import bilateral_export_partners_batch, bilateral_import_partners_batch
from data.fx import (
    commodity_fx_relationship,
    price_shock_impact,
    fetch_fx_prices,
    rolling_relationship_series,
    CURRENCY_PAIRS,
)
from data.market_data import fetch_prices as fetch_market_prices
from data.political_risk import country_risk_scores, commodity_geopolitical_risk
from data.news import commodity_news, news_sentiment_score
import views.country_exposure as exp_view
import views.trade_flows as flow_view
import views.risk_news as risk_view
import views.fx as fx_view
import views.opportunities as opp_view

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App init
# ---------------------------------------------------------------------------

app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.DARKLY],
    title="Commodity FX Volatility Tracker",
    suppress_callback_exceptions=True,
)

# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

# Categorical palette for multi-commodity line charts — distinct from the
# semantic tokens (accent/green/red/blue mean alert/gain/loss/info
# elsewhere) so a commodity's line color never implies a direction it
# doesn't have.
CATEGORY_PALETTE = [
    "#E8C468", "#6FA8DC", "#C9784F", "#8FBF8F", "#A78BFA",
    "#4FBDBA", "#E093A8", "#B5A642", "#8593B0",
]


def _color_return(val: float) -> str:
    if val > 0:
        return UI_GREEN
    if val < 0:
        return UI_RED
    return UI_MUTED


def icon_triangle(direction: str, color: str) -> html.Span:
    """Authored up/down/flat indicator, drawn in CSS — no unicode glyphs."""
    if direction == "up":
        return html.Span(className="icon-tri icon-tri--up", style={"color": color})
    if direction == "down":
        return html.Span(className="icon-tri icon-tri--down", style={"color": color})
    return html.Span(className="icon-dot", style={"background": color})


def _build_summary_cards(prices: pd.DataFrame, hv30: pd.Series) -> list:
    """Board tiles, in the roster order defined by config.COMMODITIES.

    These used to rerank on every refresh, breached thresholds first and
    then descending HV30, so the board showed what had changed. That is a
    reasonable idea for a monitoring board and the wrong one here: it
    reshuffled the tiles on every update, so gold might sit beside cotton
    on one refresh and beside wheat on the next, and a reader comparing
    two related contracts could never rely on where either one was.

    The roster is now ordered so neighbours share a driver (gold beside
    silver, the two PGMs together, crude before the thermal fuels), and
    that only helps if the order actually holds. A breached threshold is
    still obvious from the tile's own styling, which is where that signal
    belongs, rather than from its position moving.
    """
    last_price = prices.iloc[-1]
    ret_1d = (prices.iloc[-1] / prices.iloc[-2] - 1) * 100 if len(prices) >= 2 else pd.Series()

    cards = []
    for name in NAMES:
        px_val = last_price.get(name, np.nan)
        r1d = ret_1d.get(name, np.nan)
        hv = hv30.get(name, np.nan)
        threshold = ALERT_THRESHOLDS.get(name, 999)
        alert = not np.isnan(hv) and hv > threshold

        tile_class = "flip-tile flip-tile--alert" if alert else "flip-tile"
        # Keying on the live price re-mounts the tile (rather than patching
        # it in place) whenever a refresh changes the value, so the flip
        # animation replays on every board update, not just first paint.
        tile_key = f"{name}-{px_val}-{hv}"

        card = dbc.Col(
            html.Div([
                html.Div(name, className="tile-label"),
                html.Div(
                    f"{px_val:,.2f}" if not np.isnan(px_val) else "-",
                    className="tile-price",
                ),
                html.Div(
                    [
                        icon_triangle(
                            "up" if r1d > 0 else ("down" if r1d < 0 else "flat"),
                            _color_return(r1d),
                        ),
                        html.Span(
                            f" {'+' if r1d > 0 else ''}{r1d:.2f}%" if not np.isnan(r1d) else " -",
                            style={"color": _color_return(r1d)},
                        ),
                    ],
                    className="tile-return",
                ),
                html.Div(
                    f"HV30 {hv:.1f}%" if not np.isnan(hv) else "",
                    className="tile-hv",
                    style={"color": UI_ACCENT if alert else UI_MUTED},
                ),
            ], className=tile_class),
            xs=6, sm=6, md=4, className="mb-2", key=tile_key,
        )
        cards.append(card)
    return cards


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

# The mode switch and its Paper Trading tabs were removed with the
# paper-trading engine (see docs/PAPER_TRADING_HISTORY.md). One mode means
# the tab list is static, so it is built once in the layout rather than by a
# callback watching a switch that no longer exists.
# Ordered as an argument rather than by when each tab was written. It runs
# observation, then the relationship, then the mechanism behind it, then
# current context, and finishes on the synthesis:
#
#   Volatility, Returns, Alerts   what the commodities are doing
#   Currencies, Correlation       how the currencies move with them
#   Trade Flows, Country Exposure why that link exists, and on whom
#   Risk & News                   what is happening right now
#   Opportunities                 where all three signals agree
#
# Opportunities previously sat fourth, ahead of the trade data that explains
# it and the news sentiment it consumes, so the conclusion was presented
# before any of its inputs.
TABS = [
    ("Volatility", "vol"),
    ("Returns & Trends", "returns"),
    ("Alerts", "alerts-tab"),
    ("Currencies", "fx"),
    ("Correlation", "corr"),
    ("Trade Flows", "trade-flows"),
    ("Country Exposure", "country-exp"),
    ("Risk & News", "risk-news"),
    ("Opportunities", "opportunities"),
]


app.layout = dbc.Container(
    [
        # ── Header ──────────────────────────────────────────────────────
        dbc.Row(
            [
                dbc.Col(
                    [
                        html.Span(className="board-status-dot"),
                        html.Span("Commodity FX Volatility Tracker", className="board-title"),
                    ],
                    width="auto",
                    className="d-flex align-items-center",
                ),
                dbc.Col(
                    html.Small(id="last-updated", className="board-subtitle"),
                    className="d-flex align-items-center",
                ),
                dbc.Col(
                    dbc.Button(
                        "Refresh",
                        id="refresh-btn",
                        className="board-refresh-btn float-end",
                    ),
                    width="auto",
                    className="ms-auto",
                ),
            ],
            className="py-2 board-header align-items-center mb-3",
        ),

        # ── Summary cards ───────────────────────────────────────────────
        dbc.Row(id="summary-cards", className="mb-3"),

        # ── Alert banner ────────────────────────────────────────────────
        html.Div(id="alert-banner", className="mb-3"),

        # ── Tabs ────────────────────────────────────────────────────────
        dbc.Tabs(
            [dbc.Tab(label=label, tab_id=tab_id) for label, tab_id in TABS],
            id="tabs",
            active_tab=TABS[0][1],
            className="mb-3",
        ),
        html.Div(id="tab-content"),

        # ── Hidden stores ───────────────────────────────────────────────
        dcc.Store(id="store-prices"),       # commodity price DataFrame
        dcc.Store(id="store-wb"),           # World Bank indicators (lazy)
        dcc.Store(id="store-fx-prices"),    # FX pairs' own price DataFrame (lazy)
        dcc.Store(id="store-fx-corr"),      # commodity x FX correlation matrix (lazy)
        dcc.Store(id="store-risk"),         # country risk scores (lazy)
        dcc.Store(id="store-sentiment"),    # per-commodity news sentiment (lazy)
        dcc.Interval(
            id="auto-refresh",
            interval=5 * 60 * 1000,
            n_intervals=0,
        ),
    ],
    fluid=True,
    className="bg-dark min-vh-100 px-4 py-2 board-frame",
)

# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

@app.callback(
    Output("store-prices", "data"),
    Output("last-updated", "children"),
    Input("refresh-btn", "n_clicks"),
    Input("auto-refresh", "n_intervals"),
)
def load_data(_clicks, _interval):
    if DATA_SOURCE == "bloomberg":
        from data.bloomberg import fetch_prices as _fetch
        prices_raw = _fetch(BBG_TICKERS, lookback_days=365)
        prices_raw.rename(columns=BBG_TO_NAME, inplace=True)
    elif DATA_SOURCE == "yahoo":
        from data.yahoo import fetch_prices as _fetch
        prices_raw = _fetch(YAHOO_TICKERS, lookback_days=365)
        prices_raw.rename(columns=YAHOO_TO_NAME, inplace=True)
    elif DATA_SOURCE == "pipeline":
        # TradingView (via the pipeline's API) first, Yahoo
        # Finance fills in only whatever the pipeline doesn't track —
        # see data/market_data.py.
        from data.market_data import fetch_prices as _fetch
        names_to_tv = {name: spec.get("tradingview") for name, spec in COMMODITIES.items()}
        names_to_yahoo = {name: spec["yahoo"] for name, spec in COMMODITIES.items()}
        prices_raw = _fetch(names_to_tv, names_to_yahoo, lookback_days=365)
    else:  # "mock"
        from data.bloomberg import fetch_mock_prices
        prices_raw = fetch_mock_prices(BBG_TICKERS, lookback_days=365)
        prices_raw.rename(columns=BBG_TO_NAME, inplace=True)

    prices = prices_raw[[c for c in NAMES if c in prices_raw.columns]]
    timestamp = f"Updated {date.today().strftime('%d %b %Y %H:%M')}"
    return prices.to_json(date_format="iso", orient="split"), timestamp


@app.callback(
    Output("summary-cards", "children"),
    Output("alert-banner", "children"),
    Input("store-prices", "data"),
)
def update_summary(json_data):
    if json_data is None:
        return [], []
    prices = pd.read_json(json_data, orient="split")
    hv_dict = historical_volatility(prices, VOL_WINDOWS)
    hv30_series = hv_dict[30].iloc[-1]

    cards = _build_summary_cards(prices, hv30_series)

    # Alert banner
    alerts = check_alerts(hv_dict, ALERT_THRESHOLDS, window=30)
    if alerts:
        items = [
            html.Span(
                f"{a['name']}: {a['current_vol']}% (threshold {a['threshold']}%)",
                className="me-3",
            )
            for a in alerts
        ]
        banner = dbc.Alert(
            [html.Span("VOLATILITY ALERT", className="board-alert-label"), *items],
            color="warning",
            className="board-alert py-2 mb-0",
        )
    else:
        banner = []

    return cards, banner




@app.callback(
    Output("tab-content", "children"),
    Input("tabs", "active_tab"),
    Input("store-prices", "data"),
)
def render_tab(active_tab, json_data):
    if json_data is None:
        return dbc.Spinner(color="light")

    prices = pd.read_json(json_data, orient="split")

    if active_tab == "corr":
        return _render_correlation(prices)
    if active_tab == "fx":
        return fx_view.layout()
    if active_tab == "opportunities":
        return opp_view.layout()
    if active_tab == "country-exp":
        return exp_view.layout()
    if active_tab == "trade-flows":
        return flow_view.layout()
    if active_tab == "risk-news":
        return risk_view.layout()

    hv_dict = historical_volatility(prices, VOL_WINDOWS)
    if active_tab == "vol":
        return _render_volatility(prices, hv_dict)
    if active_tab == "returns":
        return _render_returns(prices, hv_dict)
    if active_tab == "alerts-tab":
        return _render_alerts(hv_dict)
    return html.Div()


# ---------------------------------------------------------------------------
# Tab renderers
# ---------------------------------------------------------------------------

def _render_volatility(prices: pd.DataFrame, hv_dict: dict) -> html.Div:
    """Line chart of rolling HV for selected commodities and windows."""

    # Controls row
    controls = dbc.Row(
        [
            dbc.Col(
                dcc.Dropdown(
                    id="vol-commodity-select",
                    options=[{"label": n, "value": n} for n in NAMES],
                    value=NAMES[:3],
                    multi=True,
                    placeholder="Select commodities…",
                    style={"background": UI_PANEL},
                ),
                md=7,
            ),
            dbc.Col(
                dbc.Checklist(
                    id="vol-window-select",
                    options=[{"label": f"HV{w}", "value": w} for w in VOL_WINDOWS],
                    value=[30],
                    inline=True,
                    switch=False,
                    className="mt-2",
                ),
                md=5,
            ),
        ],
        className="mb-3",
    )

    return html.Div(
        [
            controls,
            dcc.Graph(id="vol-chart", config={"displayModeBar": False}),
            # Hidden store for passing hv_dict data to the chart callback
            dcc.Store(
                id="store-hv",
                data={
                    str(w): hv.to_json(date_format="iso", orient="split")
                    for w, hv in hv_dict.items()
                },
            ),
        ]
    )


@app.callback(
    Output("vol-chart", "figure"),
    Input("vol-commodity-select", "value"),
    Input("vol-window-select", "value"),
    State("store-hv", "data"),
)
def update_vol_chart(selected_names, selected_windows, hv_store):
    if not selected_names or not selected_windows or not hv_store:
        return go.Figure()

    fig = go.Figure()
    palette = CATEGORY_PALETTE

    for i, name in enumerate(selected_names):
        color = palette[i % len(palette)]
        for j, w in enumerate(sorted(selected_windows)):
            hv = pd.read_json(hv_store[str(w)], orient="split")
            if name not in hv.columns:
                continue
            series = hv[name].dropna()
            dash_style = "solid" if j == 0 else ("dash" if j == 1 else "dot")
            fig.add_trace(
                go.Scatter(
                    x=series.index,
                    y=series.values,
                    name=f"{name} HV{w}",
                    line={"color": color, "dash": dash_style},
                    hovertemplate="%{y:.1f}%<extra>" + f"{name} HV{w}</extra>",
                )
            )

    fig.update_layout(
        **_dark_layout(),
        yaxis_title="Annualised Volatility (%)",
        legend={"orientation": "h", "y": -0.15},
        hovermode="x unified",
    )
    return fig


def _render_returns(prices: pd.DataFrame, hv_dict: dict) -> html.Div:
    """Table of returns, moving averages, HV30, and trend signal."""
    rets = price_returns(prices)          # index = [1d, 5d, 30d], cols = names
    ma = moving_averages(prices, [20, 50])
    hv30 = hv_dict[30].iloc[-1]
    trend = trend_signal(prices)

    TREND_DIRECTION = {1: "up", -1: "down", 0: "flat"}
    TREND_LABEL = {1: "Up", -1: "Down", 0: "Flat"}
    TREND_COLOR = {1: UI_GREEN, -1: UI_RED, 0: UI_MUTED}

    rows = []
    for name in NAMES:
        r1d = rets.at["1d", name] if "1d" in rets.index else np.nan
        r5d = rets.at["5d", name] if "5d" in rets.index else np.nan
        r30d = rets.at["30d", name] if "30d" in rets.index else np.nan
        ma20_val = ma[20].get(name, np.nan)
        ma50_val = ma[50].get(name, np.nan)
        hv_val = hv30.get(name, np.nan)
        tr = trend.get(name, 0)

        rows.append(
            html.Tr(
                [
                    html.Td(name),
                    html.Td(
                        f"{'+' if r1d > 0 else ''}{r1d:.2f}%" if not np.isnan(r1d) else "-",
                        style={"color": _color_return(r1d), "textAlign": "right"},
                    ),
                    html.Td(
                        f"{'+' if r5d > 0 else ''}{r5d:.2f}%" if not np.isnan(r5d) else "-",
                        style={"color": _color_return(r5d), "textAlign": "right"},
                    ),
                    html.Td(
                        f"{'+' if r30d > 0 else ''}{r30d:.2f}%" if not np.isnan(r30d) else "-",
                        style={"color": _color_return(r30d), "textAlign": "right"},
                    ),
                    html.Td(
                        f"{ma20_val:,.2f}" if not np.isnan(ma20_val) else "-",
                        style={"textAlign": "right"},
                    ),
                    html.Td(
                        f"{ma50_val:,.2f}" if not np.isnan(ma50_val) else "-",
                        style={"textAlign": "right"},
                    ),
                    html.Td(
                        f"{hv_val:.1f}%" if not np.isnan(hv_val) else "-",
                        style={"textAlign": "right"},
                    ),
                    html.Td(
                        [
                            icon_triangle(TREND_DIRECTION[tr], TREND_COLOR[tr]),
                            html.Span(f" {TREND_LABEL[tr]}", style={"color": TREND_COLOR[tr]}),
                        ],
                        style={"textAlign": "center"},
                    ),
                ]
            )
        )

    # Each header carries its column's alignment. They used to be bare
    # html.Th, which Bootstrap left-aligns, while every value cell below is
    # right-aligned (numbers) or centred (trend). The result was a header row
    # that did not sit above the figures it named.
    COLUMNS = [
        ("Commodity", "left"),
        ("1D %", "right"),
        ("5D %", "right"),
        ("30D %", "right"),
        ("MA20", "right"),
        ("MA50", "right"),
        ("HV30", "right"),
        ("Trend", "center"),
    ]

    header = html.Thead(
        html.Tr(
            [
                html.Th(label, style={"textAlign": align})
                for label, align in COLUMNS
            ]
        )
    )

    table = dbc.Table(
        [header, html.Tbody(rows)],
        hover=True,
        responsive=True,
        striped=False,
        size="sm",
        style={"fontSize": "0.88rem"},
    )

    return html.Div(table, className="mt-2")


def _corr_heatmap(z, x, y, height=520, colorscale=None, zmid=0, zmin=-1, zmax=1) -> go.Figure:
    fig = go.Figure(
        go.Heatmap(
            z=z, x=x, y=y,
            colorscale=colorscale or [[0, UI_RED], [0.5, "#232733"], [1, UI_GREEN]],
            zmid=zmid, zmin=zmin, zmax=zmax,
            text=np.round(z, 2),
            texttemplate="%{text}",
            textfont={"family": "Martian Mono, monospace"},
            hovertemplate="%{y} / %{x}: %{z:.2f}<extra></extra>",
        )
    )
    layout = _dark_layout()
    layout["xaxis"] = {**layout["xaxis"], "tickangle": -30}
    fig.update_layout(**layout, height=height)
    return fig


# Metric definitions for the commodity x currency relationship heatmap —
# each a real, independently-meaningful statistic derived from the same
# 52-week aligned return series (see data.fx.commodity_fx_relationship),
# not three views of the same number.
RELATIONSHIP_METRICS = {
    "correlation": {
        "label": "Correlation",
        "colorscale": [[0, UI_RED], [0.5, "#232733"], [1, UI_GREEN]],
        "zmid": 0, "zmin": -1, "zmax": 1,
    },
    "beta": {
        "label": "Beta (currency move per 1% commodity move)",
        "colorscale": [[0, UI_RED], [0.5, "#232733"], [1, UI_GREEN]],
        "zmid": 0, "zmin": None, "zmax": None,  # symmetric range computed from data
    },
    "r2": {
        "label": "R² (share of currency variance the commodity explains)",
        "colorscale": [[0, "#1a1d26"], [1, UI_ACCENT]],
        "zmid": None, "zmin": 0, "zmax": 1,
    },
}

# Rolling window choices, in trading days. A shorter window reacts fast and
# is noisy (3M beta can swing ±0.15 inside a single year); 52W/2Y are the
# smooth, stable read. Neither is "more correct" — they're different lenses
# on the same relationship, so this is user-selectable rather than one
# fixed window.
RELATIONSHIP_WINDOWS = {
    "1m":  {"label": "1 Month",  "days": 21},
    "3m":  {"label": "3 Months", "days": 63},
    "6m":  {"label": "6 Months", "days": 126},
    "9m":  {"label": "9 Months", "days": 189},
    "52w": {"label": "52 Weeks", "days": 252},
    "2y":  {"label": "2 Years",  "days": 504},
}


def _render_correlation(prices: pd.DataFrame) -> html.Div:
    """Commodity-vs-commodity heatmap, plus an interactive commodity x
    currency relationship section (Correlation / Beta / R², all 52-week
    rolling) — the real relationship this whole board exists to track,
    not just a coloring option buried in a map dropdown."""
    corr = correlation_matrix(prices, window=60)
    commodity_heatmap = dcc.Graph(
        figure=_corr_heatmap(corr.values, corr.columns.tolist(), corr.index.tolist()),
        config={"displayModeBar": False},
    )

    all_window_options = [{"label": v["label"], "value": k} for k, v in RELATIONSHIP_WINDOWS.items()]
    # The snapshot heatmap shares the app-wide 1-year commodity price cache
    # (store-prices), so it can't honor a window longer than that without a
    # dedicated longer fetch — offer only what it can actually deliver.
    # The per-pair History chart below does its own 5-year fetch and can
    # support the full range, "2 Years" included.
    snapshot_window_options = [o for o in all_window_options if RELATIONSHIP_WINDOWS[o["value"]]["days"] <= 252]

    return html.Div([
        html.H6("Commodity Correlation (60d)", className="text-muted mb-2"),
        commodity_heatmap,

        html.H6("Commodity × Currency Relationship (Rolling)", className="text-muted mb-2 mt-4"),
        dbc.Row([
            dbc.Col(
                dcc.Dropdown(
                    id="fx-relationship-metric",
                    options=[{"label": v["label"], "value": k} for k, v in RELATIONSHIP_METRICS.items()],
                    value="correlation",
                    clearable=False,
                    style={"background": UI_PANEL},
                ),
                md=7,
            ),
            dbc.Col(
                dcc.Dropdown(
                    id="fx-relationship-window",
                    options=snapshot_window_options,
                    value="52w",
                    clearable=False,
                    style={"background": UI_PANEL},
                ),
                md=3,
            ),
        ], className="mb-3"),
        dcc.Loading(html.Div(id="fx-relationship-heatmap")),

        html.H6("Relationship History (Rolling, Last 5 Years)", className="text-muted mb-2 mt-4"),
        html.P(
            "How this specific pair's beta, correlation, and R² have moved over "
            "the last five years. A single snapshot cannot show a "
            "relationship strengthening or decaying; a rolling view can. A "
            "shorter window reacts quickly and is noisy, a longer one is "
            "smoother and slower to turn, and neither is more correct than "
            "the other.",
            className="text-muted small mb-2",
        ),
        dbc.Row([
            dbc.Col(
                dcc.Dropdown(
                    id="rel-history-commodity",
                    options=[{"label": n, "value": n} for n in NAMES],
                    value="WTI Crude",
                    clearable=False,
                    style={"background": UI_PANEL},
                ),
                md=4,
            ),
            dbc.Col(
                dcc.Dropdown(
                    id="rel-history-currency",
                    options=[{"label": v["name"], "value": iso3} for iso3, v in CURRENCY_PAIRS.items()],
                    value="CAN",
                    clearable=False,
                    style={"background": UI_PANEL},
                ),
                md=4,
            ),
            dbc.Col(
                dcc.Dropdown(
                    id="rel-history-window",
                    options=all_window_options,
                    value="52w",
                    clearable=False,
                    style={"background": UI_PANEL},
                ),
                md=2,
            ),
        ], className="mb-3"),
        dcc.Loading(html.Div(id="rel-history-chart")),
        # Holds the last-fetched (commodity, currency) pair's 5-year raw
        # prices, keyed so a window-only change (which needs no new data,
        # just a different rolling-window recompute over the same prices)
        # doesn't refetch — see update_relationship_history.
        dcc.Store(id="store-rel-history-raw", data=None),
    ])


@app.callback(
    Output("rel-history-chart", "children"),
    Output("store-rel-history-raw", "data"),
    Input("rel-history-commodity", "value"),
    Input("rel-history-currency", "value"),
    Input("rel-history-window", "value"),
    State("store-rel-history-raw", "data"),
)
def update_relationship_history(commodity, iso3, window_key, cached_raw):
    if not commodity or not iso3:
        return html.Div(), no_update

    window_spec = RELATIONSHIP_WINDOWS.get(window_key, RELATIONSHIP_WINDOWS["52w"])
    window_days, window_label = window_spec["days"], window_spec["label"]

    commodity_ticker = COMMODITIES[commodity]["yahoo"]
    currency_ticker = CURRENCY_PAIRS[iso3]["ticker"]
    currency_name = CURRENCY_PAIRS[iso3]["name"]
    pair_key = f"{commodity_ticker}|{currency_ticker}"

    if cached_raw and cached_raw.get("key") == pair_key:
        raw = pd.read_json(cached_raw["prices"], orient="split")
        store_update = no_update
    else:
        try:
            # TradingView first (via the pipeline), Yahoo fills in whichever
            # side it doesn't cover — same hybrid used everywhere else, not
            # a Yahoo-only fetch, even though this chart's 5-year lookback
            # historically only had Yahoo behind it.
            names_to_tv = {
                commodity_ticker: COMMODITIES[commodity].get("tradingview"),
                currency_ticker: CURRENCY_PAIRS[iso3].get("tradingview"),
            }
            names_to_yahoo = {commodity_ticker: commodity_ticker, currency_ticker: currency_ticker}
            raw = fetch_market_prices(names_to_tv, names_to_yahoo, lookback_days=5 * 365)
        except Exception as exc:
            logger.warning("Relationship history fetch failed: %s", exc)
            return (html.Div("Price history unavailable for this pair right now.", className="text-muted small"),
                    no_update)
        store_update = {"key": pair_key, "prices": raw.to_json(date_format="iso", orient="split")}

    if commodity_ticker not in raw.columns or currency_ticker not in raw.columns:
        return html.Div("Not enough overlapping history for this pair.", className="text-muted small"), store_update

    series = rolling_relationship_series(raw[commodity_ticker], raw[currency_ticker], window=window_days)
    if series.empty:
        return (html.Div(
            f"Not enough history yet to compute a {window_label.lower()} rolling window for this pair.",
            className="text-muted small",
        ), store_update)

    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.04,
        subplot_titles=("Beta", "Correlation", "R²"),
    )
    fig.add_trace(go.Scatter(x=series.index, y=series["beta"], line={"color": UI_ACCENT},
                              hovertemplate="%{y:.3f}<extra>Beta</extra>"), row=1, col=1)
    fig.add_trace(go.Scatter(x=series.index, y=series["correlation"], line={"color": UI_BLUE},
                              hovertemplate="%{y:.3f}<extra>Correlation</extra>"), row=2, col=1)
    fig.add_trace(go.Scatter(x=series.index, y=series["r2"], line={"color": UI_GREEN},
                              hovertemplate="%{y:.3f}<extra>R²</extra>"), row=3, col=1)
    fig.add_hline(y=0, line={"color": UI_BORDER, "width": 1}, row=1, col=1)
    fig.add_hline(y=0, line={"color": UI_BORDER, "width": 1}, row=2, col=1)

    layout = _dark_layout()
    layout.pop("yaxis", None)
    layout.pop("margin", None)
    fig.update_layout(
        **layout,
        height=560,
        showlegend=False,
        margin={"l": 50, "r": 20, "t": 40, "b": 30},
        title={
            "text": f"{commodity} vs {currency_name}: {window_label.lower()} rolling, last 5 years",
            "font": {"family": "Big Shoulders Display, sans-serif", "size": 16},
        },
    )
    fig.update_xaxes(gridcolor=UI_BORDER, zeroline=False)
    fig.update_yaxes(gridcolor=UI_BORDER, zeroline=False)
    for ann in fig["layout"]["annotations"]:
        ann["font"] = {"family": "Big Shoulders Display, sans-serif", "size": 12, "color": UI_MUTED}

    return dcc.Graph(figure=fig, config={"displayModeBar": False}), store_update


@app.callback(
    Output("fx-relationship-heatmap", "children"),
    Input("fx-relationship-metric", "value"),
    Input("fx-relationship-window", "value"),
    Input("store-prices", "data"),
    State("store-fx-prices", "data"),
)
def update_fx_relationship_heatmap(metric, window_key, prices_json, fx_prices_json):
    """Recomputed fresh whenever the window changes, since correlation/
    beta/R² all depend on that choice — the cached store-fx-corr/
    store-fx-beta stay fixed at 52 weeks for the Opportunities screener
    and the Country Exposure map, which don't need this flexibility.
    "Recomputed", not "refetched": if store-fx-prices already has the 15
    FX tickers' own price history (populated by visiting the Currencies
    tab), every window this offers (all ≤52 weeks) is reused from that
    instead of hitting Yahoo Finance again for the same tickers."""
    if not prices_json:
        return html.Div("Waiting on commodity prices…", className="text-muted small")

    window_spec = RELATIONSHIP_WINDOWS.get(window_key, RELATIONSHIP_WINDOWS["52w"])
    prices = pd.read_json(prices_json, orient="split")
    fx_prices = pd.read_json(fx_prices_json, orient="split") if fx_prices_json else None

    try:
        relationship = commodity_fx_relationship(prices, window=window_spec["days"], fx_prices=fx_prices)
    except Exception as exc:
        logger.warning("FX relationship computation failed: %s", exc)
        return html.Div("Relationship data unavailable right now.", className="text-muted small")

    fx_corr = relationship["correlation"]
    if fx_corr.empty:
        return html.Div("Not enough overlapping history for this window.", className="text-muted small")

    spec = RELATIONSHIP_METRICS[metric]
    if metric == "beta":
        z_df = relationship["beta"]
        if z_df.empty:
            return html.Div("No beta data available.", className="text-muted small")
        limit = float(np.nanmax(np.abs(z_df.values))) or 1.0
        zmin, zmax = -limit, limit
    elif metric == "r2":
        z_df = relationship["r2"]
        zmin, zmax = spec["zmin"], spec["zmax"]
    else:
        z_df = fx_corr
        zmin, zmax = spec["zmin"], spec["zmax"]

    name_map = {k: v["name"] for k, v in CURRENCY_PAIRS.items()}
    y_labels = [name_map.get(i, i) for i in z_df.index]

    return dcc.Graph(
        figure=_corr_heatmap(
            z_df.values, z_df.columns.tolist(), y_labels,
            height=420, colorscale=spec["colorscale"],
            zmid=spec["zmid"], zmin=zmin, zmax=zmax,
        ),
        config={"displayModeBar": False},
    )


def _render_alerts(hv_dict: dict) -> html.Div:
    """Full alerts table with all commodities and their HV vs threshold."""
    hv_summary = current_vol_summary(hv_dict)
    thresholds = ALERT_THRESHOLDS

    rows = []
    for name in NAMES:
        group = COMMODITIES[name]["group"]
        threshold = thresholds[name]
        hv30 = hv_summary.at[name, "HV30"] if "HV30" in hv_summary.columns else np.nan
        hv60 = hv_summary.at[name, "HV60"] if "HV60" in hv_summary.columns else np.nan
        hv90 = hv_summary.at[name, "HV90"] if "HV90" in hv_summary.columns else np.nan
        triggered = not np.isnan(hv30) and hv30 > threshold
        status = (
            dbc.Badge("ALERT", color="warning", className="me-1")
            if triggered
            else dbc.Badge("OK", color="success", className="me-1")
        )
        rows.append(
            html.Tr(
                [
                    html.Td(name),
                    html.Td(group, className="text-muted"),
                    html.Td(f"{threshold:.1f}%", style={"textAlign": "right"}),
                    html.Td(
                        f"{hv30:.1f}%" if not np.isnan(hv30) else "-",
                        style={"textAlign": "right", "color": UI_ACCENT if triggered else "inherit"},
                    ),
                    html.Td(
                        f"{hv60:.1f}%" if not np.isnan(hv60) else "-",
                        style={"textAlign": "right"},
                    ),
                    html.Td(
                        f"{hv90:.1f}%" if not np.isnan(hv90) else "-",
                        style={"textAlign": "right"},
                    ),
                    html.Td(status, style={"textAlign": "center"}),
                ],
                style={"background": "var(--accent-dim)"} if triggered else {},
                className="row-flash" if triggered else "",
            )
        )

    # Same fix as the returns table: bare html.Th left-aligns, while the
    # numeric cells below are right-aligned and Status is centred.
    header = html.Thead(
        html.Tr(
            [
                html.Th(label, style={"textAlign": align})
                for label, align in [
                    ("Commodity", "left"),
                    ("Group", "left"),
                    ("Threshold", "right"),
                    ("HV30", "right"),
                    ("HV60", "right"),
                    ("HV90", "right"),
                    ("Status", "center"),
                ]
            ]
        )
    )

    table = dbc.Table(
        [header, html.Tbody(rows)],
        hover=True,
        responsive=True,
        striped=False,
        size="sm",
        style={"fontSize": "0.88rem"},
    )

    triggered_count = sum(
        1 for name in NAMES
        if (hv_summary.at[name, "HV30"] if "HV30" in hv_summary.columns else np.nan) > thresholds[name]
    )
    summary_badge = (
        dbc.Alert(
            f"{triggered_count} commodit{'y' if triggered_count == 1 else 'ies'} "
            "currently exceeding volatility threshold.",
            color="warning",
            className="py-2 mb-3",
        )
        if triggered_count > 0
        else dbc.Alert("All commodities within normal volatility ranges.", color="success", className="py-2 mb-3")
    )

    return html.Div([summary_badge, table])


# ---------------------------------------------------------------------------
# Country Exposure callbacks
# ---------------------------------------------------------------------------

@app.callback(
    Output("store-wb", "data"),
    Output("store-fx-corr", "data"),
    Output("store-risk", "data"),
    Input("tabs", "active_tab"),
    Input("store-prices", "data"),
    State("store-wb", "data"),
    State("store-fx-corr", "data"),
    State("store-risk", "data"),
    State("store-fx-prices", "data"),
)
def load_geo_data(active_tab, prices_json, wb_cached, fx_cached, risk_cached, fx_prices_json):
    """Lazily fetch World Bank + FX correlation (fixed 52-week, for the
    Opportunities screener and the Country Exposure map's FX metric — the
    Correlation tab's own relationship section recomputes on demand at
    whatever window the user picks, see update_fx_relationship_heatmap)
    + risk data when the geo/risk/correlation/opportunity tabs first open."""
    if active_tab not in ("country-exp", "trade-flows", "risk-news", "corr", "opportunities"):
        return no_update, no_update, no_update
    if wb_cached and fx_cached and risk_cached:
        return no_update, no_update, no_update

    wb_json = wb_cached
    fx_json = fx_cached

    if wb_json is None:
        try:
            wb_raw = fetch_country_indicators()
            wb_exposure = commodity_exposure_pct(wb_raw)
            combined = wb_raw[["country_name", "gdp_usd",
                                "merch_exports_usd", "merch_imports_usd"]].join(
                wb_exposure.drop(columns=["country_name"], errors="ignore")
            )
            wb_json = combined.to_json(orient="index")
        except Exception as exc:
            logger.warning("World Bank fetch failed: %s", exc)
            wb_json = "{}"

    if fx_json is None and prices_json is not None:
        try:
            prices = pd.read_json(prices_json, orient="split")
            # Reuse store-fx-prices if the Currencies tab already fetched
            # it this session, instead of hitting Yahoo Finance again for
            # the same 15 tickers.
            fx_prices = pd.read_json(fx_prices_json, orient="split") if fx_prices_json else None
            corr = commodity_fx_relationship(prices, fx_prices=fx_prices)["correlation"]
            fx_json = corr.to_json(orient="index") if not corr.empty else "{}"
        except Exception as exc:
            logger.warning("FX relationship computation failed: %s", exc)
            fx_json = "{}"

    risk_json = risk_cached
    if risk_json is None and wb_json not in (None, "{}"):
        try:
            wb_df = pd.read_json(wb_json, orient="index")
            risks = country_risk_scores(wb_df)
            risk_json = risks.to_json(orient="index")
        except Exception as exc:
            logger.warning("Risk scoring failed: %s", exc)
            risk_json = "{}"

    return wb_json, fx_json, risk_json


# ---------------------------------------------------------------------------
# Currencies + Opportunities callbacks
# ---------------------------------------------------------------------------

@app.callback(
    Output("store-fx-prices", "data"),
    Input("tabs", "active_tab"),
    State("store-fx-prices", "data"),
)
def load_fx_prices(active_tab, fx_prices_cached):
    """Lazily fetch the FX pairs' own price history — a separate callback
    from sentiment below so this fast Yahoo fetch isn't stuck behind the
    slow per-commodity sentiment loop; Dash runs independent callbacks
    concurrently, a single callback with two Outputs would not return
    either until both were done."""
    if active_tab not in ("fx", "opportunities") or fx_prices_cached:
        return no_update
    try:
        fx_prices = fetch_fx_prices(lookback_days=365)
        return fx_prices.to_json(date_format="iso", orient="split") if not fx_prices.empty else "{}"
    except Exception as exc:
        logger.warning("FX price fetch failed: %s", exc)
        return "{}"


@app.callback(
    Output("store-sentiment", "data"),
    Input("tabs", "active_tab"),
    State("store-sentiment", "data"),
)
def load_sentiment_data(active_tab, sentiment_cached):
    """Lazily fetch per-commodity news sentiment for the confluence screener."""
    if active_tab not in ("fx", "opportunities") or sentiment_cached:
        return no_update
    sentiment = {}
    for name in NAMES:
        try:
            sentiment[name] = news_sentiment_score(name)
        except Exception as exc:
            logger.warning("Sentiment fetch failed for %s: %s", name, exc)
            sentiment[name] = 0.0
    return json.dumps(sentiment)


@app.callback(
    Output("fx-summary-cards", "children"),
    Input("store-fx-prices", "data"),
)
def update_fx_summary(fx_json):
    if not fx_json or fx_json == "{}":
        return []
    fx_prices = pd.read_json(fx_json, orient="split")
    hv_dict = historical_volatility(fx_prices, VOL_WINDOWS)
    hv30 = hv_dict[30].iloc[-1]
    return fx_view.build_summary_cards(fx_prices, hv30, icon_triangle, UI_GREEN, UI_RED, UI_MUTED)


@app.callback(
    Output("fx-chart", "figure"),
    Output("store-fx-hv", "data"),
    Input("fx-currency-select", "value"),
    Input("fx-window-select", "value"),
    Input("store-fx-prices", "data"),
)
def update_fx_chart(selected_names, selected_windows, fx_json):
    if not fx_json or fx_json == "{}":
        return go.Figure(), no_update
    fx_prices = pd.read_json(fx_json, orient="split")
    hv_dict = historical_volatility(fx_prices, VOL_WINDOWS)
    hv_store = {str(w): hv.to_json(date_format="iso", orient="split") for w, hv in hv_dict.items()}

    if not selected_names or not selected_windows:
        return go.Figure(), hv_store

    fig = go.Figure()
    palette = CATEGORY_PALETTE
    for i, name in enumerate(selected_names):
        color = palette[i % len(palette)]
        for j, w in enumerate(sorted(selected_windows)):
            hv = hv_dict[w]
            if name not in hv.columns:
                continue
            series = hv[name].dropna()
            dash_style = "solid" if j == 0 else ("dash" if j == 1 else "dot")
            fig.add_trace(
                go.Scatter(
                    x=series.index, y=series.values, name=f"{name} HV{w}",
                    line={"color": color, "dash": dash_style},
                    hovertemplate="%{y:.1f}%<extra>" + f"{name} HV{w}</extra>",
                )
            )
    fig.update_layout(
        **_dark_layout(),
        yaxis_title="Annualised Volatility (%)",
        legend={"orientation": "h", "y": -0.15},
        hovermode="x unified",
    )
    return fig, hv_store


@app.callback(
    Output("opportunity-board", "children"),
    Output("opportunity-poll", "disabled"),
    Input("opportunity-poll", "n_intervals"),
    Input("store-prices", "data"),
    Input("store-fx-prices", "data"),
    Input("store-fx-corr", "data"),
    State("store-sentiment", "data"),
)
def update_opportunity_board(_n, prices_json, fx_json, fx_corr_json, sentiment_json):
    """Renders as soon as the fast inputs (prices, FX prices, correlation)
    are ready. Sentiment is the slowest input (one classified-news lookup per commodity)
    and is never a hard blocker: a commodity with no sentiment score yet
    reads as neutral (0.0) rather than holding the whole board hostage —
    the poll keeps ticking and the board quietly fills in sentiment once
    it lands, without the fast 90% of the data waiting on the slow 10%."""
    if not prices_json:
        return dbc.Spinner(color="light"), False
    if not fx_json or fx_json == "{}" or not fx_corr_json or fx_corr_json == "{}":
        return html.Div(
            "Loading price, FX, and correlation data for the confluence screen…",
            className="text-muted small",
        ), False

    prices = pd.read_json(prices_json, orient="split")
    fx_prices = pd.read_json(fx_json, orient="split")
    fx_corr = pd.read_json(fx_corr_json, orient="index")
    sentiment = json.loads(sentiment_json) if sentiment_json else {}

    board = opp_view.build_opportunity_board(prices, fx_prices, fx_corr, sentiment)
    fully_loaded = bool(sentiment_json) and len(sentiment) >= len(NAMES)
    return board, fully_loaded  # stop polling only once sentiment is complete too


# ---------------------------------------------------------------------------
# Risk & News callbacks
# ---------------------------------------------------------------------------

@app.callback(
    Output("risk-overview", "children"),
    Input("risk-commodity", "value"),
    Input("store-risk", "data"),
    State("store-prices", "data"),
)
def update_risk_overview(commodity, risk_json, prices_json):
    if not commodity:
        return html.Div()

    # Geo risk
    geo_risk = 5.0
    country_risks = pd.DataFrame()
    if risk_json and risk_json != "{}":
        try:
            country_risks = pd.read_json(risk_json, orient="index")
            geo_risk = commodity_geopolitical_risk(country_risks, commodity)
        except Exception:
            pass

    # News sentiment
    try:
        sentiment = news_sentiment_score(commodity)
    except Exception:
        sentiment = 0.0

    # HV30
    hv30 = None
    if prices_json:
        try:
            prices = pd.read_json(prices_json, orient="split")
            hv_dict = historical_volatility(prices, [30])
            val = hv_dict[30][commodity].dropna()
            if not val.empty:
                hv30 = float(val.iloc[-1])
        except Exception:
            pass

    return risk_view.build_risk_overview(commodity, geo_risk, sentiment, hv30)


@app.callback(
    Output("risk-news-feed", "children"),
    Input("risk-commodity", "value"),
)
def update_news_feed(commodity):
    if not commodity:
        return html.Div()
    try:
        # max_records=20 matches news_sentiment_score's internal call
        # (used by update_risk_overview, fired by the same commodity
        # dropdown) — same cache key, so whichever callback runs first
        # is the only one that actually fetches news; this one just slices
        # the top 8 for display instead of issuing its own query.
        articles = commodity_news(commodity, max_records=20)[:8]
    except Exception:
        articles = []
    return risk_view.build_news_feed(articles)


@app.callback(
    Output("risk-country-table", "children"),
    Input("risk-commodity", "value"),
    Input("store-risk", "data"),
)
def update_country_risk_table(commodity, risk_json):
    if not commodity:
        return html.Div()
    country_risks = pd.DataFrame()
    if risk_json and risk_json != "{}":
        try:
            country_risks = pd.read_json(risk_json, orient="index")
        except Exception:
            pass
    return risk_view.build_country_risk_table(commodity, country_risks)


@app.callback(
    Output("exp-map", "figure"),
    Input("exp-commodity", "value"),
    Input("exp-metric", "value"),
    Input("store-wb", "data"),
    Input("store-fx-corr", "data"),
)
def update_exposure_map(commodity, metric, wb_json, fx_json):
    wb_df = pd.read_json(wb_json or "{}", orient="index") if wb_json else pd.DataFrame()
    fx_df = pd.read_json(fx_json or "{}", orient="index") if fx_json else pd.DataFrame()

    net_df = net_positions(commodity)

    shock_df = pd.DataFrame()
    if metric == "shock_pct" and not wb_df.empty:
        try:
            all_nets = {n: net_positions(n) for n in NAMES}
            shock_df = price_shock_impact(all_nets, wb_df, shock_pct=0.10)
        except Exception as exc:
            logger.warning("Shock calc failed: %s", exc)

    return exp_view.build_map(commodity, metric, net_df, shock_df, fx_df, wb_df)


@app.callback(
    Output("exp-trade-source", "children"),
    Input("exp-commodity", "value"),
)
def update_exp_trade_source(commodity):
    return data_source_label(commodity)


@app.callback(
    Output("exp-detail", "children"),
    Input("exp-map", "clickData"),
    State("exp-commodity", "value"),
    State("store-wb", "data"),
    State("store-fx-corr", "data"),
)
def update_country_detail(click_data, commodity, wb_json, fx_json):
    if not click_data:
        return html.Div(
            "Click any country on the map to see its detailed exposure.",
            className="text-muted small mt-2",
        )
    iso3 = click_data["points"][0].get("location", "")
    country_name = click_data["points"][0].get("hovertext", iso3)

    wb_df = pd.read_json(wb_json or "{}", orient="index") if wb_json else pd.DataFrame()
    fx_df = pd.read_json(fx_json or "{}", orient="index") if fx_json else pd.DataFrame()

    net_df = net_positions(commodity)

    shock_df = pd.DataFrame()
    if not wb_df.empty:
        try:
            all_nets = {n: net_positions(n) for n in NAMES}
            shock_df = price_shock_impact(all_nets, wb_df, shock_pct=0.10)
        except Exception:
            pass

    return exp_view.build_country_detail(
        iso3, country_name, commodity, net_df, shock_df, fx_df, wb_df
    )


# ---------------------------------------------------------------------------
# Trade Flows callbacks
# ---------------------------------------------------------------------------

_BILATERAL_PARTNERS_SHOWN = 8


@app.callback(
    Output("flow-content", "children"),
    Output("flow-trade-source", "children"),
    Output("flow-selected-country", "data"),
    Input("flow-commodity", "value"),
    Input("flow-topn", "value"),
    Input({"type": "flow-bar", "index": ALL}, "n_clicks"),
    State("flow-selected-country", "data"),
)
def update_trade_flows(commodity, top_n, _bar_clicks, selected_country):
    try:
        traders = top_traders(commodity, top_n=top_n)
    except Exception as exc:
        logger.warning("Comtrade fetch failed: %s", exc)
        traders = {"exporters": pd.DataFrame(), "importers": pd.DataFrame(), "period": None}

    exporters_df, importers_df, period = traders["exporters"], traders["importers"], traders.get("period")

    # Bilateral (country-to-country) breakdown only exists on top of a real
    # live period — the static fallback has no such concept. Fetched once
    # up front for the whole exporter roster and the whole importer roster
    # (two batched calls, ~12 TTM requests each, not per-country), so every
    # bar in the overview already knows whether it has real partners before
    # anyone clicks anything.
    exp_bilateral, imp_bilateral = {}, {}
    if period and not exporters_df.empty:
        try:
            exp_bilateral = bilateral_export_partners_batch(
                commodity, list(exporters_df["reporter_iso3"]), period,
                top_n_partners=_BILATERAL_PARTNERS_SHOWN,
            )
        except Exception as exc:
            logger.warning("Export bilateral fetch failed for %s: %s", commodity, exc)
    if period and not importers_df.empty:
        try:
            imp_bilateral = bilateral_import_partners_batch(
                commodity, list(importers_df["reporter_iso3"]), period,
                top_n_partners=_BILATERAL_PARTNERS_SHOWN,
            )
        except Exception as exc:
            logger.warning("Import bilateral fetch failed for %s: %s", commodity, exc)

    # A bar click toggles that country's selection; changing commodity or
    # top-N invalidates whatever was selected (a different country roster
    # entirely). n_clicks are only meaningful when a bar itself fired this
    # callback — ctx.triggered_id is a dict for those, a plain string
    # ("flow-commodity"/"flow-topn") otherwise.
    triggered = ctx.triggered_id
    if isinstance(triggered, dict) and triggered.get("type") == "flow-bar":
        clicked_iso3 = triggered["index"]
        selected_country = None if clicked_iso3 == selected_country else clicked_iso3
    else:
        selected_country = None

    if selected_country is None:
        content = flow_view.build_overview(exporters_df, importers_df, exp_bilateral, imp_bilateral, top_n)
        return content, data_source_label(commodity), None

    exp_row = exporters_df[exporters_df["reporter_iso3"] == selected_country]
    imp_row = importers_df[importers_df["reporter_iso3"] == selected_country]
    export_total = float(exp_row["trade_usd"].iloc[0]) if not exp_row.empty else None
    import_total = float(imp_row["trade_usd"].iloc[0]) if not imp_row.empty else None

    # Both directions come straight from the upfront batch fetch — no
    # on-demand per-country fetch on click. A country outside the
    # pre-fetched roster for a given direction (e.g. Saudi Arabia isn't a
    # top-10 importer) shows "no available data" for that side instantly
    # rather than kicking off a fresh ~12-request TTM fetch that Comtrade's
    # rate limiting could stretch to several minutes.
    destinations = exp_bilateral.get(selected_country, pd.DataFrame())
    sources = imp_bilateral.get(selected_country, pd.DataFrame())

    content = flow_view.build_zoom(selected_country, export_total, import_total, sources, destinations, top_n)
    return content, data_source_label(commodity), selected_country


# ---------------------------------------------------------------------------
# Shared Plotly layout defaults (dark theme)
# ---------------------------------------------------------------------------

def _dark_layout() -> dict:
    return {
        "paper_bgcolor": UI_BG,
        "plot_bgcolor": UI_BG,
        "font": {"color": UI_TEXT, "size": 12, "family": "Public Sans, sans-serif"},
        "xaxis": {"gridcolor": UI_BORDER, "zeroline": False},
        "yaxis": {"gridcolor": UI_BORDER, "zeroline": False},
        "margin": {"l": 50, "r": 20, "t": 30, "b": 40},
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Dash's debug mode exposes the Werkzeug interactive debugger on
    # unhandled exceptions — a remote-code-execution risk if that ever
    # reaches a public cloud URL. Defaults off; opt in locally with
    # DASH_DEBUG=true.
    debug_mode = os.environ.get("DASH_DEBUG", "false").lower() == "true"
    # Most cloud platforms (Render, Railway, Heroku, ...) assign the port
    # at runtime via $PORT and route to whatever that is — a hardcoded
    # port would mean the platform's traffic never reaches the app. Falls
    # back to 8050 (this project's usual local URL) when unset.
    port = int(os.environ.get("PORT", 8050))
    app.run(debug=debug_mode, host="0.0.0.0", port=port, threaded=True)
