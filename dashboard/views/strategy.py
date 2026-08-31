"""Step 6 strategy monitor, as tabs inside the research dashboard.

This lived as a separate Streamlit app on its own port, which meant two
links, two designs and two processes for one project. The content is the
same as the spec's Step 6 sections; only the presentation moves, into the
same flap-board language the research tabs already use.

Everything here reads the read-only pipeline API and never the database
directly, which is the architectural rule the original handoff set for the
dashboard. The API already returns everything needed, including the venues
that drive session deadlines.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# pm2 runs this app as `python3 dashboard/app.py`, so sys.path[0] is the
# dashboard directory and the repository root is not importable. That is why
# `from config import ...` works here while `import paper_trading...` does
# not. Adding the root explicitly keeps the API client reachable without
# depending on PYTHONPATH being set by whatever launched the process.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from dash import dcc, html

from config import (
    UI_ACCENT,
    UI_BLUE,
    UI_BORDER,
    UI_GREEN,
    UI_MUTED,
    UI_PANEL,
    UI_RED,
    UI_TEXT,
)
from paper_trading.dashboard.api_client import (
    ApiClient,
    ApiClientError,
)


# Required on every strategy screen by the specification.
PAPER_BANNER = html.Div(
    "PAPER TRADING / SIMULATED CAPITAL",
    style={
        "background": UI_ACCENT,
        "color": "#12141a",
        "fontWeight": "600",
        "fontSize": "0.78rem",
        "letterSpacing": "0.06em",
        "padding": "0.35rem 0.75rem",
        "borderRadius": "3px",
        "textAlign": "center",
        "marginBottom": "0.9rem",
    },
)


def _client() -> ApiClient:
    return ApiClient()


def _label(relationship_id: str | None) -> str:
    """Turn a relationship id into something readable.

    Ids are built for uniqueness, not for reading: the commodity, the
    pricing currency and the venue-qualified FX symbol joined by double
    underscores. "Cotton__USD__FX:EURUSD" becomes "Cotton / EURUSD", which
    is what a reader needs: the driver and the pair actually traded.
    """
    if not relationship_id:
        return "n/a"
    parts = str(relationship_id).split("__")
    commodity = parts[0]
    pair = parts[-1].split(":")[-1] if len(parts) > 1 else ""
    return f"{commodity} / {pair}" if pair else commodity


def _panel(title: str, body: Any, subtitle: str | None = None) -> html.Div:
    children: list[Any] = [
        html.Div(title, className="board-title", style={"fontSize": "0.95rem"})
    ]
    if subtitle:
        children.append(
            html.Div(subtitle, className="board-subtitle",
                     style={"marginBottom": "0.5rem"})
        )
    children.append(body)
    return html.Div(
        children,
        style={
            "background": UI_PANEL,
            "border": f"1px solid {UI_BORDER}",
            "borderRadius": "4px",
            "padding": "0.9rem 1rem",
            "marginBottom": "1rem",
        },
    )


def _empty(message: str) -> html.Div:
    """An explicit empty state.

    Rendered rather than left blank so an empty panel is distinguishable
    from a broken one.
    """
    return html.Div(
        message,
        style={"color": UI_MUTED, "fontSize": "0.85rem", "padding": "0.6rem 0"},
    )


def _table(columns: list[str], rows: list[list[Any]]) -> Any:
    if not rows:
        return _empty("No rows.")
    header = html.Thead(
        html.Tr([
            html.Th(c, style={"color": UI_MUTED, "fontWeight": "500",
                              "fontSize": "0.72rem", "letterSpacing": "0.04em",
                              "textTransform": "uppercase",
                              "borderBottom": f"1px solid {UI_BORDER}"})
            for c in columns
        ])
    )
    body = html.Tbody([
        html.Tr([
            html.Td(cell, style={"color": UI_TEXT, "fontSize": "0.85rem",
                                 "borderBottom": f"1px solid {UI_BORDER}",
                                 "padding": "0.4rem 0.5rem"})
            for cell in row
        ])
        for row in rows
    ])
    return dbc.Table([header, body], borderless=True, hover=True,
                     responsive=True, className="mb-0")


def _coloured(value: float | None, suffix: str = "") -> html.Span:
    if value is None:
        return html.Span("-", style={"color": UI_MUTED})
    colour = UI_GREEN if value > 0 else (UI_RED if value < 0 else UI_MUTED)
    return html.Span(f"{value:+,.2f}{suffix}", style={"color": colour})


def _age(timestamp: str | None) -> str:
    if not timestamp:
        return "never"
    try:
        parsed = datetime.fromisoformat(
            str(timestamp).replace("Z", "+00:00")
        )
    except ValueError:
        return "?"
    seconds = (datetime.now(timezone.utc) - parsed).total_seconds()
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    return f"{seconds / 3600:.1f}h ago"


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

def positions_layout() -> Any:
    try:
        client = _client()
        positions = client.positions().get("items", [])
        relationships = client.relationships(active_only=True).get("items", [])
        summary = client.summary()
    except ApiClientError as error:
        return html.Div([PAPER_BANNER, _panel(
            "Pipeline API unreachable",
            _empty(f"{error}"),
        )])

    equity = (summary.get("latest_equity") or {})
    run = (summary.get("run") or {})

    tiles = dbc.Row([
        dbc.Col(html.Div([
            html.Div("Equity", className="board-subtitle"),
            html.Div(f"${equity.get('total_equity_usd', 0):,.2f}",
                     style={"color": UI_TEXT, "fontSize": "1.3rem"}),
        ], className="flip-tile"), xs=6, md=3, className="mb-2"),
        dbc.Col(html.Div([
            html.Div("Realised P&L", className="board-subtitle"),
            html.Div(_coloured(equity.get("realized_pnl_usd")),
                     style={"fontSize": "1.3rem"}),
        ], className="flip-tile"), xs=6, md=3, className="mb-2"),
        dbc.Col(html.Div([
            html.Div("Open positions", className="board-subtitle"),
            html.Div(str(equity.get("open_positions", 0)),
                     style={"color": UI_TEXT, "fontSize": "1.3rem"}),
        ], className="flip-tile"), xs=6, md=3, className="mb-2"),
        dbc.Col(html.Div([
            html.Div("Run", className="board-subtitle"),
            html.Div(str(run.get("run_id", "")).split("-")[-1] or "n/a",
                     style={"color": UI_BLUE, "fontSize": "1.3rem"}),
        ], className="flip-tile"), xs=6, md=3, className="mb-2"),
    ], className="mb-2")

    open_rows = [p for p in positions
                 if str(p.get("status", "")).lower() == "open"]
    closed_rows = [p for p in positions
                   if str(p.get("status", "")).lower() == "closed"]

    venues = {
        r.get("relationship_id"): (r.get("commodity_venue"), r.get("fx_venue"))
        for r in relationships
    }

    # Overnight positions are prohibited, so time remaining is a live
    # constraint on every open position.
    deadline_rows: list[list[Any]] = []
    try:
        from paper_trading.sessions.session_calendar import (
            SessionCalendar,
            UnknownVenueError,
        )

        calendar = SessionCalendar()
        now = datetime.now(timezone.utc)
        for position in open_rows:
            commodity_venue, fx_venue = venues.get(
                position.get("relationship_id"), (None, None)
            )
            opened = position.get("opened_at_utc")
            if not commodity_venue or not fx_venue or not opened:
                continue
            try:
                deadline = calendar.flat_deadline(
                    commodity_venue=commodity_venue,
                    fx_venue=fx_venue,
                    as_of=datetime.fromisoformat(
                        str(opened).replace("Z", "+00:00")
                    ),
                )
            except UnknownVenueError:
                continue
            remaining = (deadline.deadline_utc - now).total_seconds() / 60
            colour = UI_RED if remaining <= 30 else UI_TEXT
            deadline_rows.append([
                _label(position.get("relationship_id")),
                deadline.deadline_utc.strftime("%H:%M UTC"),
                html.Span(f"{remaining:.0f}m", style={"color": colour}),
                deadline.binding_leg.replace("_", " "),
            ])
    except Exception:  # noqa: BLE001
        deadline_rows = []

    return html.Div([
        PAPER_BANNER,
        tiles,
        _panel(
            "Open positions",
            _table(
                ["Relationship", "Dir", "Size USD", "Entry", "Opened"],
                [[
                    _label(p.get("relationship_id")),
                    "long" if int(p.get("direction", 0)) > 0 else "short",
                    f"{p.get('position_size_usd', 0):,.0f}",
                    f"{p.get('entry_price', 0):.6f}",
                    str(p.get("opened_at_utc", ""))[11:19],
                ] for p in open_rows],
            ) if open_rows else _empty("No open positions."),
        ),
        _panel(
            "Session deadlines",
            _table(
                ["Relationship", "Flat by", "Remaining", "Binding leg"],
                deadline_rows,
            ) if deadline_rows else _empty("No open positions."),
            subtitle="Positions close before their venue's session ends.",
        ),
        _panel(
            "Closed trades",
            _table(
                ["Relationship", "Exit reason", "Held", "Net P&L"],
                [[
                    _label(p.get("relationship_id")),
                    p.get("exit_reason") or "-",
                    str(p.get("closed_at_utc", ""))[11:19],
                    _coloured(p.get("net_pnl_usd"), " USD"),
                ] for p in closed_rows[:25]],
            ) if closed_rows else _empty("No closed trades."),
        ),
    ])


# ---------------------------------------------------------------------------
# Signals & features
# ---------------------------------------------------------------------------

def signals_layout() -> Any:
    try:
        client = _client()
        signals = client.signals_latest(limit=40).get("items", [])
        features = client.features_latest(limit=40).get("items", [])
    except ApiClientError as error:
        return html.Div([PAPER_BANNER,
                         _panel("Pipeline API unreachable", _empty(str(error)))])

    return html.Div([
        PAPER_BANNER,
        _panel(
            "Latest signal decisions",
            _table(
                ["Relationship", "Type", "Mode", "Strength", "Approved", "Reason"],
                [[
                    _label(s.get("relationship_id")),
                    s.get("decision_type"),
                    s.get("signal_mode"),
                    f"{s.get('signal_strength'):.3f}"
                    if s.get("signal_strength") is not None else "-",
                    html.Span("yes", style={"color": UI_GREEN})
                    if s.get("approved") else html.Span("no", style={"color": UI_MUTED}),
                    s.get("reason_code") or "-",
                ] for s in signals[:25]],
            ) if signals else _empty("No decisions this run."),
        ),
        _panel(
            "Latest feature snapshots",
            _table(
                ["Relationship", "Impulse", "Expected FX", "Observed FX",
                 "Divergence", "Coverage", "Complete"],
                [[
                    _label(f.get("relationship_id")),
                    f"{f.get('commodity_impulse'):.3f}"
                    if f.get("commodity_impulse") is not None else "-",
                    f"{f.get('expected_fx_impulse'):.3f}"
                    if f.get("expected_fx_impulse") is not None else "-",
                    f"{f.get('observed_fx_impulse'):.3f}"
                    if f.get("observed_fx_impulse") is not None else "-",
                    f"{f.get('divergence_score'):.3f}"
                    if f.get("divergence_score") is not None else "-",
                    f"{f.get('market_window_coverage_pct', 0):.1f}%",
                    html.Span("yes", style={"color": UI_GREEN})
                    if f.get("market_data_complete") else
                    html.Span("no", style={"color": UI_MUTED}),
                ] for f in features[:25]],
            ) if features else _empty("No feature snapshots."),
            subtitle="Relationships without a measured beta produce no signal.",
        ),
    ])


# ---------------------------------------------------------------------------
# Performance & exposure
# ---------------------------------------------------------------------------

def performance_layout() -> Any:
    try:
        client = _client()
        equity = client.equity(limit=500).get("items", [])
        summary = client.summary()
    except ApiClientError as error:
        return html.Div([PAPER_BANNER,
                         _panel("Pipeline API unreachable", _empty(str(error)))])

    if not equity:
        return html.Div([
            PAPER_BANNER,
            _panel("Equity", _empty("No equity snapshots.")),
        ])

    ordered = sorted(equity, key=lambda e: str(e.get("snapshot_timestamp_utc")))
    figure = go.Figure()
    figure.add_trace(go.Scatter(
        x=[e.get("snapshot_timestamp_utc") for e in ordered],
        y=[e.get("total_equity_usd") for e in ordered],
        mode="lines",
        line={"color": UI_ACCENT, "width": 2},
        name="Equity",
    ))
    figure.update_layout(
        paper_bgcolor=UI_PANEL, plot_bgcolor=UI_PANEL,
        font={"color": UI_TEXT, "size": 11},
        margin={"l": 40, "r": 20, "t": 10, "b": 30},
        height=280, showlegend=False,
        xaxis={"gridcolor": UI_BORDER, "showgrid": True},
        yaxis={"gridcolor": UI_BORDER, "showgrid": True, "title": "USD"},
    )

    latest = (summary.get("latest_equity") or {})
    exposure_rows = [
        ["Gross exposure", f"${latest.get('gross_exposure_usd', 0):,.2f}"],
        ["Net exposure", f"${latest.get('net_exposure_usd', 0):,.2f}"],
        ["Unrealised P&L", _coloured(latest.get("unrealized_pnl_usd"), " USD")],
        ["Realised P&L", _coloured(latest.get("realized_pnl_usd"), " USD")],
        ["Transaction cost", f"${latest.get('transaction_cost_usd', 0):,.2f}"],
        ["Drawdown", f"{latest.get('drawdown_pct', 0):.2f}%"],
    ]

    return html.Div([
        PAPER_BANNER,
        _panel("Equity", dcc.Graph(figure=figure, config={"displayModeBar": False})),
        _panel("Exposure", _table(["Measure", "Value"], exposure_rows)),
    ])


# ---------------------------------------------------------------------------
# Engine health
# ---------------------------------------------------------------------------

def engine_layout() -> Any:
    try:
        health = _client().health()
    except ApiClientError as error:
        return html.Div([PAPER_BANNER,
                         _panel("Pipeline API unreachable", _empty(str(error)))])

    level = {"healthy": UI_GREEN, "degraded": UI_ACCENT,
             "failed": UI_RED, "offline": UI_RED}

    service_rows = [[
        s.get("service_name"),
        html.Span(s.get("status"),
                  style={"color": level.get(str(s.get("status")), UI_MUTED)}),
        _age(s.get("last_heartbeat_utc")),
    ] for s in health.get("services", [])]

    orchestrator = next(
        (s for s in health.get("services", [])
         if s.get("service_name") == "strategy_orchestrator"),
        None,
    )
    stage_rows: list[list[Any]] = []
    if orchestrator:
        details = orchestrator.get("details_json") or {}
        if isinstance(details, str):
            import json
            try:
                details = json.loads(details)
            except ValueError:
                details = {}
        for name, stage in sorted((details.get("stages") or {}).items()):
            stage_rows.append([
                name,
                str(stage.get("total_runs", 0)),
                str(stage.get("total_failures", 0)),
                html.Span(str(stage.get("consecutive_failures", 0)),
                          style={"color": UI_RED
                                 if stage.get("consecutive_failures")
                                 else UI_MUTED}),
                (stage.get("last_error") or "-")[:70],
            ])

    return html.Div([
        PAPER_BANNER,
        _panel(
            "Services",
            _table(["Service", "Status", "Last heartbeat"], service_rows),
            subtitle="Heartbeat age since each service last reported.",
        ),
        _panel(
            "Strategy loop",
            _table(
                ["Stage", "Runs", "Failures", "Consecutive", "Last error"],
                stage_rows,
            ) if stage_rows else _empty("Strategy loop not running."),
        ),
    ])
