"""
Country Exposure tab — world choropleth + country detail panel.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from dash import dcc, html
import dash_bootstrap_components as dbc

from config import (
    COMMODITIES, NAMES, UI_ACCENT, UI_BG, UI_BLUE, UI_BORDER, UI_GREEN,
    UI_MUTED, UI_OCEAN, UI_PANEL, UI_RED, UI_TEXT,
)
from data.fx import CURRENCY_PAIRS


# ---------------------------------------------------------------------------
# Map tab layout
# ---------------------------------------------------------------------------

def layout(names: list[str] = NAMES) -> html.Div:
    return html.Div([
        # Controls
        dbc.Row([
            dbc.Col([
                html.Label("Commodity", className="text-muted small mb-1"),
                dcc.Dropdown(
                    id="exp-commodity",
                    options=[{"label": n, "value": n} for n in names],
                    value=names[0],
                    clearable=False,
                    style={"background": UI_PANEL},
                ),
            ], md=3),
            dbc.Col([
                html.Label("Metric", className="text-muted small mb-1"),
                dcc.Dropdown(
                    id="exp-metric",
                    options=[
                        {"label": "Net trade position (% GDP)",  "value": "net_pct_gdp"},
                        {"label": "Export value (USD)",           "value": "export_usd"},
                        {"label": "Import value (USD)",           "value": "import_usd"},
                        {"label": "Price shock impact (% GDP, +10%)", "value": "shock_pct"},
                        {"label": "FX correlation",              "value": "fx_corr"},
                    ],
                    value="net_pct_gdp",
                    clearable=False,
                    style={"background": UI_PANEL},
                ),
            ], md=4),
            dbc.Col(
                html.Small(id="exp-trade-source", className="text-muted"),
                className="d-flex align-items-end pb-1",
                md=3,
            ),
        ], className="mb-3 align-items-end"),

        # Map
        dcc.Loading(
            dcc.Graph(id="exp-map", config={"displayModeBar": False}, style={"height": "450px"}),
            color=UI_BLUE,
        ),

        # Detail panel — appears on country click
        html.Div(id="exp-detail", className="mt-3"),
    ])


# ---------------------------------------------------------------------------
# Map figure builder
# ---------------------------------------------------------------------------

def build_map(
    commodity: str,
    metric: str,
    net_df: pd.DataFrame,
    shock_df: pd.DataFrame,
    fx_corr_df: pd.DataFrame,
    wb_df: pd.DataFrame,
) -> go.Figure:
    """Build a choropleth based on the selected metric."""

    if metric == "net_pct_gdp":
        # Already computed in worldbank: energy_net_pct / agri_net_pct / metals_net_pct
        group = COMMODITIES[commodity]["group"].lower()
        col_map = {"energy": "energy_net_pct", "agriculture": "agri_net_pct",
                   "metals": "metals_net_pct"}
        col = col_map.get(group)
        if col and col in wb_df.columns:
            plot_df = wb_df[[col, "country_name"]].dropna().reset_index()
            plot_df.columns = ["iso3", "value", "country_name"]
        else:
            plot_df = _net_df_to_plot(net_df)
        label = "Net trade (% GDP)"
        colorscale = [[0, UI_RED], [0.5, "#232733"], [1, UI_GREEN]]
        zmid = 0

    elif metric == "export_usd":
        if net_df.empty:
            return _empty_map("No trade data available")
        plot_df = net_df[["reporter_iso3", "Export", "reporter_name"]].dropna()
        plot_df = plot_df.rename(columns={"reporter_iso3": "iso3", "Export": "value",
                                          "reporter_name": "country_name"})
        label = "Export value (USD)"
        colorscale = [[0, "#1a1d26"], [1, UI_BLUE]]
        zmid = None

    elif metric == "import_usd":
        if net_df.empty:
            return _empty_map("No trade data available")
        plot_df = net_df[["reporter_iso3", "Import", "reporter_name"]].dropna()
        plot_df = plot_df.rename(columns={"reporter_iso3": "iso3", "Import": "value",
                                          "reporter_name": "country_name"})
        label = "Import value (USD)"
        colorscale = [[0, "#1a1d26"], [1, UI_ACCENT]]
        zmid = None

    elif metric == "shock_pct":
        if shock_df.empty or commodity not in shock_df.columns:
            return _empty_map("No shock data available")
        s = shock_df[commodity].dropna().reset_index()
        s.columns = ["iso3", "value"]
        name_map = wb_df["country_name"].to_dict() if "country_name" in wb_df.columns else {}
        s["country_name"] = s["iso3"].map(name_map).fillna(s["iso3"])
        plot_df = s
        label = "Trade balance impact (% GDP, +10% price)"
        colorscale = [[0, UI_RED], [0.5, "#232733"], [1, UI_GREEN]]
        zmid = 0

    elif metric == "fx_corr":
        if fx_corr_df.empty or commodity not in fx_corr_df.columns:
            return _empty_map("No FX correlation data available")
        s = fx_corr_df[commodity].dropna().reset_index()
        s.columns = ["iso3", "value"]
        name_map = {k: v["name"] for k, v in CURRENCY_PAIRS.items()}
        s["country_name"] = s["iso3"].map(name_map).fillna(s["iso3"])
        plot_df = s
        label = f"FX correlation with {commodity}"
        colorscale = [[0, UI_RED], [0.5, "#232733"], [1, UI_GREEN]]
        zmid = 0

    else:
        return _empty_map("Unknown metric")

    fig = px.choropleth(
        plot_df,
        locations="iso3",
        color="value",
        hover_name="country_name",
        hover_data={"value": ":.2f", "iso3": False},
        color_continuous_scale=colorscale,
        color_continuous_midpoint=zmid,
        labels={"value": label},
        title=f"{commodity}: {label}",
    )
    fig.update_layout(
        paper_bgcolor=UI_BG,
        plot_bgcolor=UI_BG,
        font={"color": UI_TEXT, "family": "Public Sans, sans-serif"},
        title_font={"family": "Big Shoulders Display, sans-serif", "color": UI_TEXT, "size": 18},
        geo=dict(
            bgcolor=UI_BG,
            showframe=False,
            showcoastlines=True,
            coastlinecolor=UI_BORDER,
            landcolor=UI_BORDER,
            showocean=True,
            oceancolor=UI_OCEAN,
            showlakes=False,
            projection_type="natural earth",
        ),
        margin={"l": 0, "r": 0, "t": 40, "b": 0},
        coloraxis_colorbar=dict(
            bgcolor=UI_BG,
            tickfont={"color": UI_TEXT},
            title={"font": {"color": UI_TEXT}},
        ),
    )
    return fig


def build_country_detail(
    iso3: str,
    country_name: str,
    commodity: str,
    net_df: pd.DataFrame,
    shock_df: pd.DataFrame,
    fx_corr_df: pd.DataFrame,
    wb_df: pd.DataFrame,
) -> html.Div:
    """Detail panel shown when a country is clicked on the map."""

    sections = []

    # --- Trade position for each commodity group ---
    if iso3 in wb_df.index:
        row = wb_df.loc[iso3]
        group_items = []
        for label, col in [
            ("Energy", "energy_net_pct"),
            ("Agriculture", "agri_net_pct"),
            ("Metals", "metals_net_pct"),
        ]:
            val = row.get(col, float("nan"))
            if pd.isna(val):
                continue
            color = UI_GREEN if val > 0 else UI_RED
            direction = "Net exporter" if val > 0 else "Net importer"
            group_items.append(
                html.Div([
                    html.Span(label, className="text-muted me-2", style={"width": "100px", "display": "inline-block"}),
                    html.Span(f"{direction}  ", style={"color": color}),
                    html.Span(f"({val:+.2f}% GDP)", style={"color": UI_MUTED, "fontSize": "0.8rem"}),
                ], className="mb-1")
            )
        if group_items:
            sections.append(html.Div([
                html.H6("Commodity-group trade position", className="text-muted mb-2"),
                *group_items,
            ], className="mb-3"))

    # --- Selected commodity trade detail ---
    if not net_df.empty and "reporter_iso3" in net_df.columns:
        row = net_df[net_df["reporter_iso3"] == iso3]
        if not row.empty:
            r = row.iloc[0]
            exp_val = r.get("Export", 0)
            imp_val = r.get("Import", 0)
            net_val = r.get("net_usd", 0)
            color = UI_GREEN if net_val >= 0 else UI_RED
            sections.append(html.Div([
                html.H6(f"{commodity} trade flows (2022)", className="text-muted mb-2"),
                _kv("Exports", f"${exp_val/1e9:.2f}B"),
                _kv("Imports", f"${imp_val/1e9:.2f}B"),
                _kv("Net position", f"${net_val/1e9:+.2f}B", color=color),
            ], className="mb-3"))

    # --- Price shock impact ---
    if not shock_df.empty and iso3 in shock_df.index:
        shock_row = shock_df.loc[iso3]
        shock_items = []
        for c in shock_row.index:
            v = shock_row[c]
            if pd.isna(v):
                continue
            col = UI_GREEN if v > 0 else UI_RED
            shock_items.append(
                html.Div([
                    html.Span(c, className="text-muted me-2",
                              style={"width": "130px", "display": "inline-block"}),
                    html.Span(f"{v:+.3f}% GDP", style={"color": col}),
                ], className="mb-1")
            )
        if shock_items:
            sections.append(html.Div([
                html.H6("Trade balance impact, +10% price shock", className="text-muted mb-2"),
                *shock_items,
            ], className="mb-3"))

    # --- FX correlation ---
    if not fx_corr_df.empty and iso3 in fx_corr_df.index:
        fx_row = fx_corr_df.loc[iso3]
        currency_name = CURRENCY_PAIRS.get(iso3, {}).get("name", iso3)
        corr_items = []
        for c in fx_row.index:
            v = fx_row[c]
            if pd.isna(v):
                continue
            bar_color = UI_GREEN if v > 0 else UI_RED
            corr_items.append(
                html.Div([
                    html.Span(c, className="text-muted me-2",
                              style={"width": "130px", "display": "inline-block"}),
                    html.Span(f"{v:+.2f}", style={"color": bar_color}),
                    html.Div(
                        style={
                            "display": "inline-block",
                            "width": f"{abs(v) * 80}px",
                            "height": "8px",
                            "background": bar_color,
                            "marginLeft": "8px",
                            "verticalAlign": "middle",
                        }
                    ),
                ], className="mb-1")
            )
        if corr_items:
            sections.append(html.Div([
                html.H6(f"{currency_name}: FX correlations with commodities (1Y)", className="text-muted mb-2"),
                *corr_items,
            ], className="mb-3"))

    if not sections:
        return html.Div(
            "No detailed data available for this country.",
            className="text-muted small",
        )

    return dbc.Card(
        dbc.CardBody([
            html.H5(country_name, className="mb-3"),
            dbc.Row([dbc.Col(s, md=6) for s in sections]),
        ]),
        style={"background": UI_PANEL, "border": f"1px solid {UI_BORDER}"},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _kv(label: str, value: str, color: str = UI_TEXT) -> html.Div:
    return html.Div([
        html.Span(label + ": ", className="text-muted"),
        html.Span(value, style={"color": color}),
    ], className="mb-1")


def _net_df_to_plot(net_df: pd.DataFrame) -> pd.DataFrame:
    if net_df.empty:
        return pd.DataFrame(columns=["iso3", "value", "country_name"])
    df = net_df[["reporter_iso3", "net_usd", "reporter_name"]].dropna()
    df = df.rename(columns={"reporter_iso3": "iso3", "net_usd": "value",
                             "reporter_name": "country_name"})
    return df


def _empty_map(message: str) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text=message, xref="paper", yref="paper",
                       x=0.5, y=0.5, showarrow=False,
                       font={"color": UI_MUTED, "size": 14})
    fig.update_layout(
        paper_bgcolor=UI_BG,
        plot_bgcolor=UI_BG,
        geo=dict(bgcolor=UI_BG, showframe=False, landcolor=UI_BORDER,
                 showocean=True, oceancolor=UI_OCEAN),
        margin={"l": 0, "r": 0, "t": 0, "b": 0},
    )
    return fig
