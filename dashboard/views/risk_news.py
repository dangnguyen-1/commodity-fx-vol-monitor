"""
Risk & News tab — geopolitical risk scores + live news feed.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from dash import dcc, html
import dash_bootstrap_components as dbc

from config import NAMES, UI_ACCENT, UI_BG, UI_BLUE, UI_BORDER, UI_GREEN, UI_MUTED, UI_PANEL, UI_RED, UI_TEXT
from data.news import news_service_unreachable
from data.political_risk import _risk_label, risk_color, COMMODITY_COUNTRY_WEIGHTS


def _tone_icon(direction: str, color: str) -> html.Span:
    """Authored up/down/flat indicator for news tone, drawn in CSS — no unicode glyphs."""
    if direction == "up":
        return html.Span(className="icon-tri icon-tri--up", style={"color": color})
    if direction == "down":
        return html.Span(className="icon-tri icon-tri--down", style={"color": color})
    return html.Span(className="icon-dot", style={"background": color})


def layout() -> html.Div:
    return html.Div([
        dbc.Row([
            dbc.Col([
                html.Label("Commodity", className="text-muted small mb-1"),
                dcc.Dropdown(
                    id="risk-commodity",
                    options=[{"label": n, "value": n} for n in NAMES],
                    value=NAMES[0],
                    clearable=False,
                    style={"background": UI_PANEL},
                ),
            ], md=3),
        ], className="mb-3"),

        # Risk gauge row
        dcc.Loading(
            html.Div(id="risk-overview"),
            color=UI_ACCENT,
        ),

        html.Hr(style={"borderColor": UI_BORDER}),

        # News feed
        dbc.Row([
            dbc.Col([
                html.H6("Latest News", className="text-muted mb-2"),
                dcc.Loading(html.Div(id="risk-news-feed"), color=UI_BLUE),
            ], md=7),
            dbc.Col([
                html.H6("Key Country Risks", className="text-muted mb-2"),
                html.Div(id="risk-country-table"),
            ], md=5),
        ]),
    ])


def build_risk_overview(
    commodity: str,
    geo_risk: float,
    news_sentiment: float,
    hv30: float | None,
) -> html.Div:
    """Top row: composite risk gauge + component breakdown."""
    # Composite score: blend geo risk, news sentiment adjustment, volatility signal
    sentiment_adj = max(-1.5, min(1.5, -news_sentiment / 5))  # negative tone → higher risk
    vol_adj = 0.0
    if hv30 is not None:
        vol_adj = max(0, (hv30 - 30) / 40)  # adds up to ~1.75 for extreme vol

    composite = min(10.0, max(1.0, round(geo_risk + sentiment_adj + vol_adj, 1)))

    gauge = _risk_gauge(composite, commodity)

    components = dbc.Card(
        dbc.CardBody([
            html.H6("Risk components", className="text-muted mb-3"),
            _component_bar("Geopolitical", geo_risk, 10),
            _component_bar("News sentiment", max(1, min(10, 5.5 - news_sentiment / 2)), 10,
                           note=f"{news_sentiment:+.1f} tone"),
            _component_bar("Volatility", min(10, max(1, round(3 + vol_adj * 4, 1))), 10,
                           note=f"HV30: {hv30:.0f}%" if hv30 else "-"),
            html.Div(
                "Geopolitical blends a live World Bank baseline with a static, "
                "dated conflict and sanctions list, not a live feed.",
                className="text-muted",
                style={"fontSize": "0.7rem", "marginTop": "10px"},
            ),
        ]),
        style={"background": UI_PANEL, "border": f"1px solid {UI_BORDER}"},
        className="h-100",
    )

    return dbc.Row([
        dbc.Col(dcc.Graph(figure=gauge, config={"displayModeBar": False},
                          style={"height": "220px"}), md=5),
        dbc.Col(components, md=7),
    ], className="mb-3")


def build_news_feed(articles: list[dict]) -> html.Div:
    if not articles:
        if news_service_unreachable():
            return html.Div(
                "News service (GDELT) is unreachable. Retrying "
                "automatically in the background. This is a live third-party "
                "dependency, not a broken tab.",
                className="text-muted small",
            )
        return html.Div("No recent news found for this commodity.", className="text-muted small")
    items = []
    for a in articles:
        tone = a.get("tone", 0)
        tone_color = UI_RED if tone < -2 else (UI_GREEN if tone > 2 else UI_MUTED)
        tone_direction = "down" if tone < -1 else ("up" if tone > 1 else "flat")
        items.append(
            dbc.Card(
                dbc.CardBody([
                    dbc.Row([
                        dbc.Col(
                            html.A(
                                a["title"],
                                href=a["url"],
                                target="_blank",
                                style={"color": UI_TEXT, "textDecoration": "none",
                                       "fontSize": "0.88rem"},
                            ),
                            width=10,
                        ),
                        dbc.Col(
                            _tone_icon(tone_direction, tone_color),
                            width=2, className="text-end",
                        ),
                    ]),
                    html.Div(
                        f"{a.get('source', '')}  ·  {_fmt_date(a.get('date', ''))}",
                        style={"color": UI_MUTED, "fontSize": "0.75rem", "marginTop": "2px"},
                    ),
                ], className="py-2 px-3"),
                className="news-card",
                style={"marginBottom": "6px"},
            ),
        )
    return html.Div(items)


def build_country_risk_table(commodity: str, country_risks: pd.DataFrame) -> html.Div:
    """Table of key supply-chain countries for this commodity with risk scores."""
    weights = COMMODITY_COUNTRY_WEIGHTS.get(commodity, {})
    if not weights or country_risks.empty:
        return html.Div("Loading risk data…", className="text-muted small")

    rows = []
    for iso3, weight in sorted(weights.items(), key=lambda x: -x[1]):
        if iso3 not in country_risks.index:
            continue
        r = country_risks.loc[iso3]
        score = r["risk_score"]
        color = risk_color(score)
        note = r.get("conflict_note", "")
        rows.append(html.Tr([
            html.Td(iso3, style={"color": UI_MUTED, "fontSize": "0.75rem", "width": "40px"}),
            html.Td(f"{int(weight * 100)}%",
                    style={"color": UI_MUTED, "fontSize": "0.75rem", "width": "38px"}),
            html.Td(
                dbc.Progress(
                    value=score * 10,
                    color=_progress_color(score),
                    style={"height": "8px"},
                    className="mt-1",
                ),
                style={"width": "80px"},
            ),
            html.Td(
                html.Span(f"{score:.0f}", style={"color": color, "fontWeight": "bold",
                                                  "fontSize": "0.88rem"}),
                style={"width": "28px", "textAlign": "right"},
            ),
            html.Td(
                html.Small(note[:40] + "…" if len(note) > 40 else note,
                           style={"color": UI_MUTED}),
            ),
        ]))

    return dbc.Table(
        [html.Tbody(rows)],
        size="sm", style={"fontSize": "0.8rem"},
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _risk_gauge(score: float, commodity: str) -> go.Figure:
    color = risk_color(score)
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=score,
        number={"font": {"size": 42, "color": color, "family": "Martian Mono, monospace"}},
        title={"text": f"{commodity}<br><span style='font-size:0.8em;color:{UI_MUTED}'>"
                       f"Composite Risk Score</span>",
               "font": {"color": UI_TEXT, "family": "Big Shoulders Display, sans-serif"}},
        gauge={
            "axis": {"range": [1, 10], "tickwidth": 1, "tickcolor": UI_BORDER,
                     "tickvals": [1, 2.5, 4, 5.5, 7, 8.5, 10],
                     "ticktext": ["1", "", "Low", "Mod", "High", "", "10"]},
            "bar": {"color": color, "thickness": 0.3},
            "bgcolor": UI_PANEL,
            "bordercolor": UI_BORDER,
            "steps": [
                {"range": [1, 2.5],  "color": "#132420"},
                {"range": [2.5, 4],  "color": "#1c2a1c"},
                {"range": [4, 5.5],  "color": "#2b2313"},
                {"range": [5.5, 7],  "color": "#332210"},
                {"range": [7, 8.5],  "color": "#33161b"},
                {"range": [8.5, 10], "color": "#2e0f13"},
            ],
            "threshold": {"line": {"color": color, "width": 3}, "value": score},
        },
    ))
    fig.update_layout(
        paper_bgcolor=UI_BG, plot_bgcolor=UI_BG,
        font={"color": UI_TEXT},
        margin={"l": 20, "r": 20, "t": 50, "b": 10},
        height=220,
    )
    return fig


def _component_bar(label: str, value: float, max_val: float, note: str = "") -> html.Div:
    color = risk_color(value)
    pct = value / max_val * 100
    return html.Div([
        dbc.Row([
            dbc.Col(html.Small(label, style={"color": UI_MUTED}), width=5),
            dbc.Col(
                dbc.Progress(value=pct, color=_progress_color(value),
                             style={"height": "10px", "marginTop": "2px"}),
                width=5,
            ),
            dbc.Col(
                html.Small(
                    f"{value:.1f}" + (f"  {note}" if note else ""),
                    style={"color": color, "fontWeight": "bold"},
                ),
                width=2, className="text-end",
            ),
        ], className="align-items-center"),
    ], className="mb-2")


def _progress_color(score: float) -> str:
    if score <= 3:   return "success"
    if score <= 5.5: return "warning"
    return "danger"


def _fmt_date(raw: str) -> str:
    if len(raw) >= 8:
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw
