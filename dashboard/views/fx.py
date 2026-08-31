"""
Currencies tab — the 14 tracked FX pairs get the same board treatment as the
9 commodities: their own price tiles and their own volatility chart, not
just a correlation input buried in another tab.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import dash_bootstrap_components as dbc
from dash import dcc, html

from config import UI_MUTED, UI_PANEL
from data.fx import FX_NAMES


def layout(names: list[str] = FX_NAMES) -> html.Div:
    return html.Div([
        # dbc.Row, not html.Div: the children are dbc.Col, and a Bootstrap
        # column needs a .row flex parent to lay out horizontally. Inside a
        # plain div each column stays a block element at its declared width
        # and they stack in a single vertical strip.
        dbc.Row(id="fx-summary-cards", className="mb-3"),
        dbc.Row(
            [
                dbc.Col(
                    dcc.Dropdown(
                        id="fx-currency-select",
                        options=[{"label": n, "value": n} for n in names],
                        value=names[:3],
                        multi=True,
                        placeholder="Select currencies…",
                        style={"background": UI_PANEL},
                    ),
                    md=7,
                ),
                dbc.Col(
                    dbc.Checklist(
                        id="fx-window-select",
                        options=[{"label": f"HV{w}", "value": w} for w in (30, 60, 90)],
                        value=[30],
                        inline=True,
                        className="mt-2",
                    ),
                    md=5,
                ),
            ],
            className="mb-3",
        ),
        dcc.Graph(id="fx-chart", config={"displayModeBar": False}),
        dcc.Store(id="store-fx-hv"),
    ])


def _color_return(val: float, green: str, red: str, muted: str) -> str:
    if val > 0:
        return green
    if val < 0:
        return red
    return muted


def build_summary_cards(
    fx_prices: pd.DataFrame,
    fx_hv30: pd.Series,
    icon_triangle,
    ui_green: str,
    ui_red: str,
    ui_muted: str,
) -> list:
    """Board tiles for the 14 currency pairs — price, 1D move, HV30. No alert
    threshold exists for FX yet, so tiles stay in the plain (non-amber) state;
    the confluence screener on the Opportunities tab is where FX volatility
    actually gets judged against something."""
    last_price = fx_prices.iloc[-1]
    ret_1d = (fx_prices.iloc[-1] / fx_prices.iloc[-2] - 1) * 100 if len(fx_prices) >= 2 else pd.Series()

    cards = []
    for name in FX_NAMES:
        if name not in fx_prices.columns:
            continue
        px_val = last_price.get(name, np.nan)
        r1d = ret_1d.get(name, np.nan)
        hv = fx_hv30.get(name, np.nan)
        color = _color_return(r1d, ui_green, ui_red, ui_muted)

        cards.append(
            dbc.Col(
                html.Div([
                    html.Div(name, className="tile-label"),
                    html.Div(
                        f"{px_val:.4f}" if not np.isnan(px_val) else "-",
                        className="tile-price",
                    ),
                    html.Div(
                        [
                            icon_triangle(
                                "up" if r1d > 0 else ("down" if r1d < 0 else "flat"), color,
                            ),
                            html.Span(
                                f" {'+' if r1d > 0 else ''}{r1d:.2f}%" if not np.isnan(r1d) else " -",
                                style={"color": color},
                            ),
                        ],
                        className="tile-return",
                    ),
                    html.Div(
                        f"HV30 {hv:.1f}%" if not np.isnan(hv) else "",
                        className="tile-hv",
                        style={"color": UI_MUTED},
                    ),
                ], className="flip-tile"),
                xs=6, sm=4, md=3, className="mb-2",
            )
        )
    return cards
