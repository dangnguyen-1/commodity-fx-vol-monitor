"""
Trade Flows tab — ranked exporter/importer bars, click any country for its
own real bilateral partners.

Default view: two ranked lists, top exporters (blue) and top importers
(red), each a "fat bar" sized by trade value with the country code printed
large on it — no flow lines, no Sankey, just the two rankings side by
side. Clicking any bar re-centers the view on that one country: its real
import sources (blue bars, left) and export destinations (red bars,
right), pulled from UN Comtrade's bilateral breakdown. A country that
only reports a world total for a given direction — no named partners at
all — shows "N/A" for that side rather than a guess. Clicking the
selected country's own card again returns to the two-list overview.
"""

from __future__ import annotations

import pandas as pd
from dash import dcc, html
import dash_bootstrap_components as dbc

from config import NAMES, UI_BLUE, UI_MUTED, UI_PANEL, UI_RED, UI_TEXT


def _rgba(hex_color: str, alpha: float) -> str:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def _fmt_usd(value: float) -> str:
    if value < 0.01e9:
        return f"${value / 1e6:.1f}M"
    return f"${value / 1e9:.2f}B"


def layout() -> html.Div:
    return html.Div([
        html.P(
            "Click a country to see its bilateral partners: who it "
            "sends to and who it buys from. \"N/A\" means that side only "
            "reports a world total to UN Comtrade, not named countries.",
            className="text-muted small mb-2",
        ),
        dcc.Store(id="flow-selected-country", data=None),
        dbc.Row([
            dbc.Col([
                html.Label("Commodity", className="text-muted small mb-1"),
                dcc.Dropdown(
                    id="flow-commodity",
                    options=[{"label": n, "value": n} for n in NAMES],
                    value=NAMES[0],
                    clearable=False,
                    style={"background": UI_PANEL},
                ),
            ], md=3),
            dbc.Col([
                html.Label("Show top N countries", className="text-muted small mb-1"),
                dcc.Slider(
                    id="flow-topn",
                    min=5, max=20, step=5,
                    value=10,
                    marks={5: "5", 10: "10", 15: "15", 20: "20"},
                ),
            ], md=4),
            dbc.Col(
                html.Small(id="flow-trade-source", className="text-muted"),
                className="d-flex align-items-end pb-1",
                md=3,
            ),
        ], className="mb-3 align-items-end"),
        dcc.Loading(html.Div(id="flow-content"), color=UI_BLUE),
    ])


def _bar_row(
    iso3: str,
    value: float,
    max_value: float,
    color: str,
    has_data: bool,
    selected: bool = False,
    clickable: bool = True,
) -> html.Div:
    """One fat, value-proportional bar with the country code printed large
    on top of it. Clickable rows are wrapped in a real html.Button (the
    only way to get a reliable click target — Plotly Sankey bars, tried
    earlier, don't fire click events at all)."""
    pct = max(2.0, min(100.0, (value / max_value) * 100)) if max_value else 2.0
    label = iso3 if has_data else f"{iso3} (N/A)"
    classes = "flow-bar-row"
    if selected:
        classes += " flow-bar-active"
    if not has_data:
        classes += " flow-bar-na"

    # scaleX (transform) rather than width — width changes trigger layout
    # reflow on every render; transform is compositor-only.
    content = html.Div([
        html.Div(className="flow-bar-fill",
                 style={"transform": f"scaleX({pct / 100})", "background": _rgba(color, 0.4)}),
        html.Div([
            html.Span(label, className="flow-bar-label"),
            html.Span(_fmt_usd(value), className="flow-bar-value"),
        ], className="flow-bar-content"),
    ])

    if not clickable:
        return html.Div(content, className=classes)
    return html.Button(
        content,
        id={"type": "flow-bar", "index": iso3},
        n_clicks=0,
        className=classes,
    )


def _back_button(iso3: str) -> html.Button:
    """Reuses the same click id the bars use — clicking it re-fires the
    "same country clicked twice" toggle-off in the callback, landing back
    on the overview. A dedicated, always-visible control rather than
    relying on someone noticing the center card itself is clickable."""
    return html.Button(
        "← Back to overview",
        id={"type": "flow-bar", "index": iso3},
        n_clicks=0,
        className="flow-back-btn",
    )


def _na_placeholder(iso3: str, text: str = "No available data") -> html.Div:
    return html.Div([
        html.Div(text, className="flow-bar-empty"),
        _back_button(iso3),
    ])


def build_overview(
    exporters: pd.DataFrame,
    importers: pd.DataFrame,
    exp_bilateral: dict[str, pd.DataFrame],
    imp_bilateral: dict[str, pd.DataFrame],
    top_n: int,
    selected: str | None = None,
) -> html.Div:
    """The default two-list ranking — no flow lines, just fat bars."""
    if exporters.empty and importers.empty:
        return html.Div(
            "No trade flow data available for this commodity. Data loads "
            "from UN Comtrade. Try again or check your connection.",
            className="text-muted",
        )

    exp_rows = exporters.head(top_n)
    imp_rows = importers.head(top_n)
    exp_max = float(exp_rows["trade_usd"].max()) if not exp_rows.empty else 1.0
    imp_max = float(imp_rows["trade_usd"].max()) if not imp_rows.empty else 1.0

    exp_bars = [
        _bar_row(
            row.reporter_iso3, row.trade_usd, exp_max, UI_BLUE,
            has_data=bool(exp_bilateral.get(row.reporter_iso3) is not None
                          and not exp_bilateral[row.reporter_iso3].empty),
            selected=row.reporter_iso3 == selected,
        )
        for row in exp_rows.itertuples()
    ]
    imp_bars = [
        _bar_row(
            row.reporter_iso3, row.trade_usd, imp_max, UI_RED,
            has_data=bool(imp_bilateral.get(row.reporter_iso3) is not None
                          and not imp_bilateral[row.reporter_iso3].empty),
            selected=row.reporter_iso3 == selected,
        )
        for row in imp_rows.itertuples()
    ]

    return dbc.Row([
        dbc.Col([html.H6("Top Exporters", style={"color": UI_BLUE}, className="mb-2"), *exp_bars], md=6),
        dbc.Col([html.H6("Top Importers", style={"color": UI_RED}, className="mb-2"), *imp_bars], md=6),
    ])


def build_zoom(
    iso3: str,
    export_total: float | None,
    import_total: float | None,
    sources: pd.DataFrame,
    destinations: pd.DataFrame,
    top_n: int,
) -> html.Div:
    """Centered on one country: who sends to it (left, blue) and who it
    sends to (right, red). Flanking bars are informational, not clickable
    — only the original top-exporter/top-importer rankings drill down, to
    keep the click surface bounded to countries this app already has
    totals for."""
    src_rows = sources.head(top_n) if sources is not None else pd.DataFrame()
    dst_rows = destinations.head(top_n) if destinations is not None else pd.DataFrame()
    src_max = float(src_rows["trade_usd"].max()) if not src_rows.empty else 1.0
    dst_max = float(dst_rows["trade_usd"].max()) if not dst_rows.empty else 1.0

    src_bars = (
        [_bar_row(r.partner_iso3, r.trade_usd, src_max, UI_BLUE, has_data=True, clickable=False)
         for r in src_rows.itertuples()]
        if not src_rows.empty else [_na_placeholder(iso3)]
    )
    dst_bars = (
        [_bar_row(r.partner_iso3, r.trade_usd, dst_max, UI_RED, has_data=True, clickable=False)
         for r in dst_rows.itertuples()]
        if not dst_rows.empty else [_na_placeholder(iso3)]
    )

    totals = []
    if export_total is not None:
        totals.append(html.Div(f"Exports: {_fmt_usd(export_total)}", className="flow-center-total"))
    if import_total is not None:
        totals.append(html.Div(f"Imports: {_fmt_usd(import_total)}", className="flow-center-total"))

    center = html.Button([
        html.Div(iso3, className="flow-center-label"),
        *totals,
        html.Div("click to go back", className="flow-center-hint"),
    ], id={"type": "flow-bar", "index": iso3}, n_clicks=0, className="flow-center-card")

    return dbc.Row([
        dbc.Col([html.H6("Imports From", style={"color": UI_BLUE}, className="mb-2"), *src_bars], md=4),
        dbc.Col(center, md=4, className="d-flex align-items-center justify-content-center"),
        dbc.Col([html.H6("Exports To", style={"color": UI_RED}, className="mb-2"), *dst_bars], md=4),
    ])
