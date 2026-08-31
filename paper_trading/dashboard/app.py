from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from paper_trading.dashboard.api_client import (
    ApiClient,
    ApiClientError,
    ApiNotFoundError,
    DEFAULT_API_BASE_URL,
)


# Validated default palette (see dataviz skill references/palette.md).
COLOR_GOOD = "#0ca30c"
COLOR_WARNING = "#fab219"
COLOR_CRITICAL = "#d03b3b"
COLOR_SEQUENTIAL_BLUE = "#256abf"
# Cool-toned to match the app's navy background (rgb(14,17,23)) -- a warm
# gray here would clash; see redesign-existing-projects' "don't mix warm
# and cool grays" rule.
COLOR_GRIDLINE = "rgba(148, 163, 184, 0.20)"
CATEGORICAL_SLOTS = [
    "#2a78d6",  # blue
    "#eb6834",  # orange
    "#1baf7a",  # aqua
    "#eda100",  # yellow
    "#e87ba4",  # magenta
    "#008300",  # green
    "#4a3aa7",  # violet
    "#e34948",  # red
]

SPEC_REFRESH_SECONDS = 5  # dashboard.refresh_interval_seconds in the YAML spec
TICK_SECONDS = 1  # how often the fragment checks whether a refresh is due

SERVICE_STATUS_LEVEL = {
    "healthy": "good",
    "degraded": "warning",
    "offline": "critical",
    "failed": "critical",
}
BADGE_COLOR = {"good": "green", "warning": "orange", "critical": "red", "muted": "gray"}
BADGE_ICON = {
    "good": ":material/check_circle:",
    "warning": ":material/warning:",
    "critical": ":material/cancel:",
    "muted": ":material/help:",
}

st.set_page_config(
    page_title="Commodity-FX Paper Trading",
    page_icon="📈",
    layout="wide",
)

# Scoped visual polish: one accent color, flat surfaces, hairline borders
# instead of shadows, no gradients or decoration. Card elevation is hooked
# off an invisible marker (see card()) rather than Streamlit's internal
# class names, so it survives Streamlit version upgrades. No motion here
# by design -- this is a task surface, not a marketing page, and it
# reruns every 5 seconds on auto-refresh, so any entrance animation would
# restart on a loop instead of running once.
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Manrope:wght@400;500;600;700&display=swap');

    :root {
        --font-display: 'Space Grotesk', system-ui, sans-serif;
        --font-body: 'Manrope', system-ui, sans-serif;
    }

    .stApp { font-family: var(--font-body); }

    /* Display font for anything that names or quantifies something. */
    .stApp h1, .stApp h2, .stApp h3,
    [data-testid="stMetricValue"],
    [data-testid="stMetricLabel"],
    [data-testid="stTab"] p,
    .stMarkdownBadge,
    button[data-testid^="stBaseButton"] p {
        font-family: var(--font-display) !important;
    }

    /* Hide the invisible card-marker's own element wrapper so it takes no space. */
    div[data-testid="stElementContainer"]:has(.dash-card-marker) {
        display: none;
    }

    /* Card structure: flat surface, one hairline edge, no shadow, no glow.
       Elevation is declared once (border only) rather than stacked with a
       shadow -- see impeccable's craft-floor "ghost card" rule. */
    div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] .dash-card-marker) {
        border: 1px solid rgba(255, 255, 255, 0.08) !important;
        border-radius: 10px !important;
        box-shadow: none !important;
        background: rgba(255, 255, 255, 0.02);
    }

    /* Hero KPI (Total Equity): a bigger figure earns the emphasis on its
       own -- no color wash, same flat surface as every other card. */
    div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] .dash-hero-marker) {
        height: 100%;
        display: flex;
        flex-direction: column;
        justify-content: center;
    }
    div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] .dash-hero-marker) [data-testid="stMetricValue"] {
        font-size: 2.5rem !important;
        line-height: 1.1;
    }

    /* Headings: size-specific tracking (tighter as text gets larger). */
    .stApp h1 { letter-spacing: -0.02em; font-weight: 700; }
    .stApp h2, .stApp h3 { letter-spacing: -0.01em; font-weight: 600; }

    /* Numerals: fixed-width digits so figures in a column line up. */
    [data-testid="stMetricValue"],
    [data-testid="stMetricDelta"],
    [data-testid="stDataFrame"] {
        font-variant-numeric: tabular-nums;
    }

    /* Themed text selection instead of the browser default. */
    ::selection {
        background: rgba(57, 135, 229, 0.35);
    }

    @media (prefers-reduced-motion: reduce) {
        * { transition: none !important; animation: none !important; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def age_seconds(timestamp: str | None) -> float | None:
    parsed = parse_utc(timestamp)
    if parsed is None:
        return None
    return (utc_now() - parsed).total_seconds()


def format_age(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)}s ago"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f}m ago"
    return f"{minutes / 60:.1f}h ago"


def currency_color_map(currencies: list[str]) -> dict[str, str]:
    ordered = sorted(set(currencies))
    return {c: CATEGORICAL_SLOTS[i % len(CATEGORICAL_SLOTS)] for i, c in enumerate(ordered)}


def status_badge(label: str, level: str) -> None:
    st.badge(label, icon=BADGE_ICON.get(level), color=BADGE_COLOR.get(level, "gray"))


@contextmanager
def card() -> Iterator[None]:
    """A bordered container marked so CSS can style it as an elevated card.

    Uses an invisible marker element (rather than sniffing for a metric or
    badge inside) so the styling hook doesn't depend on Streamlit's internal
    markup for any particular widget.
    """
    with st.container(border=True):
        st.markdown('<span class="dash-card-marker"></span>', unsafe_allow_html=True)
        yield


@st.cache_resource(show_spinner=False)
def get_client(base_url: str) -> ApiClient:
    return ApiClient(base_url=base_url)


# ----------------------------------------------------------------------
# Data loading: fetch everything once per refresh interval, cache in
# session_state, and render from the cache every tick so the UI never
# flickers empty between refreshes.
# ----------------------------------------------------------------------


def fetch_all(client: ApiClient, run_id_override: str | None) -> dict[str, Any]:
    data: dict[str, Any] = {"fetched_at": utc_now(), "errors": []}

    try:
        data["health"] = client.health()
    except ApiClientError as exc:
        data["errors"].append(f"health: {exc}")
        data["health"] = None

    try:
        data["run_payload"] = client.current_run(run_id=run_id_override)
    except ApiNotFoundError:
        data["run_payload"] = None
    except ApiClientError as exc:
        data["errors"].append(f"current_run: {exc}")
        data["run_payload"] = None

    run = (data["run_payload"] or {}).get("run") or {}
    run_id = run.get("run_id")
    data["run_id"] = run_id

    try:
        data["relationships"] = client.relationships(active_only=True).get("items", [])
    except ApiClientError as exc:
        data["errors"].append(f"relationships: {exc}")
        data["relationships"] = []

    try:
        data["news"] = client.news_latest(limit=150).get("items", [])
    except ApiClientError as exc:
        data["errors"].append(f"news: {exc}")
        data["news"] = []

    if run_id:
        try:
            spec_payload = client.strategy(run_id=run_id)
            data["specification"] = spec_payload.get("specification") or {}
        except ApiClientError:
            data["specification"] = {}

        try:
            data["features"] = client.features_latest(run_id=run_id, limit=200)
        except ApiClientError as exc:
            data["errors"].append(f"features: {exc}")
            data["features"] = {}

        try:
            data["signals"] = client.signals_latest(run_id=run_id, limit=200)
        except ApiClientError as exc:
            data["errors"].append(f"signals: {exc}")
            data["signals"] = {}

        try:
            data["open_positions"] = client.positions(run_id=run_id, status="open", limit=500).get(
                "items", []
            )
        except ApiClientError as exc:
            data["errors"].append(f"positions(open): {exc}")
            data["open_positions"] = []

        try:
            data["closed_positions"] = client.positions(
                run_id=run_id, status="closed", limit=500
            ).get("items", [])
        except ApiClientError as exc:
            data["errors"].append(f"positions(closed): {exc}")
            data["closed_positions"] = []

        try:
            data["orders"] = client.orders(run_id=run_id, status="all", limit=500).get("items", [])
        except ApiClientError as exc:
            data["errors"].append(f"orders: {exc}")
            data["orders"] = []

        try:
            data["fills"] = client.fills(run_id=run_id, limit=500).get("items", [])
        except ApiClientError as exc:
            data["errors"].append(f"fills: {exc}")
            data["fills"] = []

        try:
            equity_payload = client.equity(run_id=run_id, limit=2000)
            data["equity_items"] = equity_payload.get("items", [])
            data["equity_latest"] = equity_payload.get("latest")
        except ApiClientError as exc:
            data["errors"].append(f"equity: {exc}")
            data["equity_items"] = []
            data["equity_latest"] = None
    else:
        data["specification"] = {}
        data["features"] = {}
        data["signals"] = {}
        data["open_positions"] = []
        data["closed_positions"] = []
        data["orders"] = []
        data["fills"] = []
        data["equity_items"] = []
        data["equity_latest"] = None

    return data


def ensure_fresh_data(
    client: ApiClient, run_id_override: str | None, refresh_interval: float
) -> dict[str, Any]:
    now_ts = time.time()
    last_ts = st.session_state.get("last_fetch_ts", 0.0)
    cache = st.session_state.get("data_cache")
    force = st.session_state.pop("force_refresh", False)
    if cache is None or force or (now_ts - last_ts) >= refresh_interval:
        st.session_state["data_cache"] = fetch_all(client, run_id_override)
        st.session_state["last_fetch_ts"] = now_ts
    return st.session_state["data_cache"]


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------


@st.cache_data(ttl=30, show_spinner=False)
def fetch_known_currencies(base_url: str) -> list[str]:
    try:
        client = ApiClient(base_url=base_url)
        items = client.relationships(active_only=True).get("items", [])
    except ApiClientError:
        return []
    return sorted({item.get("currency") for item in items if item.get("currency")})


def render_sidebar() -> tuple[str, str | None, list[str], int, float]:
    st.sidebar.title("Commodity–FX")
    st.sidebar.caption("Paper trading · read-only dashboard")

    with st.sidebar.expander("Connection", expanded=False):
        base_url = st.text_input(
            "API base URL",
            value=st.session_state.get("api_base_url", DEFAULT_API_BASE_URL),
            help="Read-only FastAPI backend. The dashboard never queries SQLite directly.",
        )
        st.session_state["api_base_url"] = base_url
        run_id_input = st.text_input(
            "Run ID override (optional)",
            value=st.session_state.get("run_id_override", ""),
        )
        st.session_state["run_id_override"] = run_id_input

    st.sidebar.subheader("Filters")
    known_currencies = fetch_known_currencies(base_url)
    currency_filter = st.sidebar.multiselect("Currencies", options=known_currencies, default=[])
    news_limit = st.sidebar.slider("News articles to show", 10, 150, 50, step=10)

    st.sidebar.subheader("Refresh")
    refresh_interval = st.sidebar.slider(
        "Refresh interval (sec)", 5, 120, SPEC_REFRESH_SECONDS, step=5
    )
    if st.sidebar.button("Refresh now", width="stretch"):
        st.session_state["force_refresh"] = True
        st.rerun()

    return base_url, (run_id_input.strip() or None), currency_filter, news_limit, float(
        refresh_interval
    )


def render_header_and_kpis(data: dict[str, Any]) -> None:
    st.title("Commodity–FX Intraday Paper Trading")
    st.error(
        "**PAPER TRADING ONLY** — no real capital, no brokerage connection. "
        "Live capital is not approved for this candidate.",
        icon=":material/gpp_maybe:",
    )

    health = data.get("health")
    run = (data.get("run_payload") or {}).get("run") or {}
    equity_latest = data.get("equity_latest")
    open_positions = data.get("open_positions") or []

    today_start = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    equity_items = data.get("equity_items") or []
    day_start_equity = None
    for item in equity_items:
        ts = parse_utc(item.get("snapshot_timestamp_utc"))
        if ts and ts < today_start:
            day_start_equity = item["total_equity_usd"]
    if day_start_equity is None and run:
        day_start_equity = run.get("initial_equity_usd")

    # Total Equity gets real visual weight (hero card); the other four are
    # secondary context in a 2x2 grid beside it -- five equal-sized tiles
    # would flatten the hierarchy and bury the one number that matters most.
    hero_col, secondary_col = st.columns([1.2, 2], gap="medium")

    with hero_col, card():
        st.markdown('<span class="dash-hero-marker"></span>', unsafe_allow_html=True)
        equity_value = equity_latest["total_equity_usd"] if equity_latest else run.get(
            "initial_equity_usd"
        )
        delta = None
        if equity_value is not None and day_start_equity is not None:
            delta = equity_value - day_start_equity
        st.metric(
            "Total Equity",
            f"${equity_value:,.2f}" if equity_value is not None else "—",
            delta=f"${delta:,.2f} today" if delta is not None else None,
            delta_color="off" if not delta else "normal",
        )

    with secondary_col:
        row1 = st.columns(2)
        with row1[0], card():
            realized = equity_latest["realized_pnl_usd"] if equity_latest else 0.0
            st.metric("Realized P&L", f"${realized:,.2f}" if equity_latest else "—")
        with row1[1], card():
            unrealized = equity_latest["unrealized_pnl_usd"] if equity_latest else 0.0
            st.metric("Unrealized P&L", f"${unrealized:,.2f}" if equity_latest else "—")
        row2 = st.columns(2)
        with row2[0], card():
            st.metric("Open Positions", len(open_positions))
        with row2[1], card():
            if health is None:
                status_badge("unreachable", "critical")
            else:
                overall = health.get("status", "unknown")
                status_badge(overall, "good" if overall == "healthy" else "warning")
            st.caption("System status")


def render_system_status_expander(data: dict[str, Any]) -> None:
    health = data.get("health")
    if health is None:
        st.caption("API unreachable — cannot show service heartbeats.")
        return
    services = health.get("services", [])
    unresolved = health.get("unresolved_alerts", {}) or {}
    run = (data.get("run_payload") or {}).get("run") or {}
    counts = (data.get("run_payload") or {}).get("counts") or {}

    with st.expander(
        f"System details — run `{run.get('run_id', 'none')}` · "
        f"{unresolved.get('total', 0)} unresolved alert(s)",
        expanded=False,
    ):
        if run:
            st.caption(
                f"Mode `{run.get('run_mode')}` · status `{run.get('status')}` · "
                f"{counts.get('features', 0)} feature rows · "
                f"{counts.get('decisions', 0)} decisions · "
                f"{counts.get('open_positions', 0)} open / {counts.get('closed_positions', 0)} closed"
            )
        else:
            st.info(
                "No paper-trading run exists yet. Panels below show empty states "
                "until the strategy cycle has run at least once."
            )
        if services:
            cols = st.columns(min(len(services), 4) or 1)
            for i, svc in enumerate(services):
                with cols[i % len(cols)]:
                    level = SERVICE_STATUS_LEVEL.get(svc.get("status"), "muted")
                    status_badge(f"{svc.get('service_name')}: {svc.get('status')}", level)
                    st.caption(f"heartbeat {format_age(age_seconds(svc.get('last_heartbeat_utc')))}")
        st.divider()
        render_orchestrator_panel(data)
        for err in data.get("errors", []):
            st.caption(f"⚠ {err}")


def render_session_panel(data: dict[str, Any]) -> None:
    """When each open position must be flat, and why.

    The spec forbids overnight positions, and since v0.2.0 that is actually
    enforced -- so "how long has this position got" is now a live constraint
    rather than a footnote. Venues come from /relationships, which already
    returns them, so this stays inside the rule that the dashboard reads the
    API and never the database.
    """
    positions = [
        p
        for p in (data.get("positions") or [])
        if str(p.get("status", "")).lower() == "open"
    ]

    try:
        from paper_trading.sessions.session_calendar import (
            SessionCalendar,
            UnknownVenueError,
        )

        calendar = SessionCalendar()
    except Exception as exc:  # noqa: BLE001
        st.caption(f"Session calendar unavailable: {exc}")
        return

    venues = {
        r.get("relationship_id"): (
            r.get("commodity_venue"),
            r.get("fx_venue"),
        )
        for r in (data.get("relationships") or [])
    }

    st.subheader(":material/schedule: Session Deadlines")
    if not positions:
        st.caption(
            "No open positions. Zero positions is a valid state — "
            "deadlines appear here once something is open."
        )
        return

    now = utc_now()
    rows = []
    for position in positions:
        relationship_id = position.get("relationship_id")
        commodity_venue, fx_venue = venues.get(relationship_id, (None, None))
        if not commodity_venue or not fx_venue:
            continue
        opened_at = parse_utc(position.get("opened_at_utc"))
        if opened_at is None:
            continue
        try:
            deadline = calendar.flat_deadline(
                commodity_venue=commodity_venue,
                fx_venue=fx_venue,
                as_of=opened_at,
            )
        except UnknownVenueError:
            continue
        remaining = (deadline.deadline_utc - now).total_seconds() / 60.0
        rows.append(
            {
                "relationship": relationship_id,
                "must be flat by (UTC)": deadline.deadline_utc.strftime(
                    "%Y-%m-%d %H:%M"
                ),
                "minutes left": round(remaining, 1),
                "binding leg": deadline.binding_leg,
                "commodity venue": commodity_venue,
            }
        )

    if not rows:
        st.caption("Open positions have no resolvable venue metadata.")
        return

    overdue = [r for r in rows if r["minutes left"] <= 0]
    soon = [r for r in rows if 0 < r["minutes left"] <= 30]
    if overdue:
        st.error(
            f"{len(overdue)} position(s) past the flat deadline. Execution "
            "closes these on its next cycle; if that persists, check the "
            "strategy-orchestrator process."
        )
    elif soon:
        st.warning(f"{len(soon)} position(s) within 30 minutes of the deadline.")

    st.dataframe(
        pd.DataFrame(rows).sort_values("minutes left"),
        width="stretch",
        hide_index=True,
    )


def render_orchestrator_panel(data: dict[str, Any]) -> None:
    """Per-stage health of the strategy loop.

    The heartbeat carries which stages ran, how many times each has failed
    and the last error. Without this the dashboard can only say the
    orchestrator is alive, not whether it is doing anything -- which is the
    distinction that mattered when the whole engine sat idle for six weeks.
    """
    health = data.get("health") or {}
    orchestrator = next(
        (
            s
            for s in health.get("services", [])
            if s.get("service_name") == "strategy_orchestrator"
        ),
        None,
    )
    if orchestrator is None:
        st.caption(
            "No strategy_orchestrator heartbeat. The decision loop is not "
            "running — data collection alone does not evaluate the strategy."
        )
        return

    details = orchestrator.get("details_json") or {}
    if isinstance(details, str):
        import json

        try:
            details = json.loads(details)
        except ValueError:
            details = {}

    stages = details.get("stages") or {}
    st.caption(
        f"cycle {details.get('cycles', '?')} · run `{details.get('run_id', '?')}` · "
        f"signals every {details.get('signal_interval_minutes', '?')}m · "
        f"execution every {details.get('execution_interval_minutes', '?')}m"
    )
    if not stages:
        return

    rows = [
        {
            "stage": name,
            "last success (UTC)": (stage.get("last_success_utc") or "never")[:19],
            "runs": stage.get("total_runs", 0),
            "failures": stage.get("total_failures", 0),
            "consecutive failures": stage.get("consecutive_failures", 0),
            "last error": stage.get("last_error") or "",
        }
        for name, stage in sorted(stages.items())
    ]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def filter_by_currency(rows: list[dict], currency_filter: list[str]) -> list[dict]:
    if not currency_filter:
        return rows
    return [r for r in rows if r.get("currency") in currency_filter]


def render_positions_tab(data: dict[str, Any], currency_filter: list[str]) -> None:
    open_positions = filter_by_currency(data.get("open_positions") or [], currency_filter)
    st.caption(f"{len(open_positions)} open position(s)")

    max_holding_minutes = (
        (data.get("specification") or {})
        .get("exits", {})
        .get("maximum_holding_time", {})
        .get("minutes")
    )

    if not open_positions:
        st.info("No open positions. Zero positions is a valid state.")
    else:
        rows = []
        for item in open_positions:
            age_min = None
            opened = age_seconds(item.get("opened_at_utc"))
            if opened is not None:
                age_min = opened / 60.0
            exit_risk = "on schedule"
            if max_holding_minutes and age_min is not None:
                fraction = age_min / max_holding_minutes
                if fraction >= 1.0:
                    exit_risk = "past max holding"
                elif fraction >= 0.8:
                    exit_risk = "near max holding"
            rows.append(
                {
                    "commodity": item.get("commodity"),
                    "currency": item.get("currency"),
                    "direction": "long" if item.get("direction") == 1 else "short",
                    "entry_price": item.get("entry_price"),
                    "mark_price": item.get("latest_mark_price"),
                    "notional_usd": item.get("position_size_usd"),
                    "unrealized_pnl_usd": item.get("latest_unrealized_pnl_usd"),
                    "age_minutes": round(age_min, 1) if age_min is not None else None,
                    "exit_risk": exit_risk,
                }
            )
        df = pd.DataFrame(rows)
        st.dataframe(
            df,
            width="stretch",
            hide_index=True,
            column_config={
                "unrealized_pnl_usd": st.column_config.NumberColumn(
                    "Unrealized P&L (USD)", format="%.2f"
                ),
                "notional_usd": st.column_config.NumberColumn("Notional (USD)", format="%.2f"),
            },
        )

    closed_positions = filter_by_currency(data.get("closed_positions") or [], currency_filter)
    with st.expander(f"Closed positions & audit trail ({len(closed_positions)})", expanded=False):
        if closed_positions:
            closed_df = pd.DataFrame(closed_positions)
            cols = [
                c
                for c in [
                    "commodity",
                    "currency",
                    "direction",
                    "opened_at_utc",
                    "entry_price",
                    "closed_at_utc",
                    "exit_price",
                    "net_pnl_usd",
                    "exit_reason",
                ]
                if c in closed_df.columns
            ]
            st.dataframe(closed_df[cols], width="stretch", hide_index=True)
        else:
            st.caption("No closed positions yet.")

        orders = data.get("orders") or []
        fills = data.get("fills") or []
        st.markdown("**Orders**")
        if orders:
            orders_df = pd.DataFrame(orders)
            cols = [
                c
                for c in [
                    "order_id",
                    "decision_id",
                    "commodity",
                    "currency",
                    "side",
                    "order_action",
                    "status",
                    "notional_usd",
                    "signal_price",
                    "decision_type",
                    "signal_mode",
                    "submitted_at_utc",
                ]
                if c in orders_df.columns
            ]
            st.dataframe(orders_df[cols], width="stretch", hide_index=True)
        else:
            st.caption("No orders yet — expected while all decisions are no_action.")

        st.markdown("**Fills**")
        if fills:
            fills_df = pd.DataFrame(fills)
            cols = [
                c
                for c in [
                    "fill_id",
                    "order_id",
                    "commodity",
                    "currency",
                    "side",
                    "fill_price",
                    "filled_notional_usd",
                    "total_transaction_cost_usd",
                    "fill_timestamp_utc",
                ]
                if c in fills_df.columns
            ]
            st.dataframe(fills_df[cols], width="stretch", hide_index=True)
        else:
            st.caption("No fills yet.")


def render_signals_tab(data: dict[str, Any], currency_filter: list[str]) -> None:
    signals_payload = data.get("signals") or {}
    signal_items = filter_by_currency(signals_payload.get("items", []), currency_filter)
    st.subheader(":material/bolt: Latest Signal Decisions")
    st.caption(
        f"Decision timestamp: {signals_payload.get('decision_timestamp_utc') or 'none yet'} — "
        f"{len(signal_items)} relationship(s)"
    )
    if signal_items:
        df = pd.DataFrame(signal_items)
        cols = [
            c
            for c in [
                "commodity",
                "currency",
                "decision_type",
                "signal_mode",
                "signal_strength",
                "approved",
                "reason_code",
                "divergence_score",
                "market_data_complete",
            ]
            if c in df.columns
        ]
        st.dataframe(df[cols], width="stretch", hide_index=True)
    else:
        st.info(
            "No signal decisions at the latest timestamp — expected when market data "
            "is incomplete or entry thresholds aren't met."
        )

    st.subheader(":material/dataset: Latest Feature Snapshots")
    features_payload = data.get("features") or {}
    feature_items = filter_by_currency(features_payload.get("items", []), currency_filter)
    st.caption(
        f"Feature timestamp: {features_payload.get('feature_timestamp_utc') or 'none yet'} — "
        f"{len(feature_items)} relationship(s)"
    )
    if feature_items:
        df = pd.DataFrame(feature_items)
        cols = [
            c
            for c in [
                "commodity",
                "currency",
                "market_data_complete",
                "market_window_coverage_pct",
                "divergence_score",
                "expected_fx_impulse",
                "observed_fx_impulse",
                "relevant_news_count",
            ]
            if c in df.columns
        ]
        st.dataframe(df[cols], width="stretch", hide_index=True)
    else:
        st.info("No feature snapshots yet.")


def render_news_tab(
    data: dict[str, Any], currency_filter: list[str], news_limit: int
) -> None:
    news_items = (data.get("news") or [])[:news_limit]
    relationships = data.get("relationships") or []
    commodity_to_currencies: dict[str, set[str]] = {}
    for rel in relationships:
        commodity_to_currencies.setdefault(rel.get("commodity"), set()).add(rel.get("currency"))

    if currency_filter:
        filtered = []
        for row in news_items:
            asset, asset_type = row.get("asset"), row.get("asset_type")
            if asset_type == "currency" and asset in currency_filter:
                filtered.append(row)
            elif asset_type == "commodity" and commodity_to_currencies.get(asset, set()) & set(
                currency_filter
            ):
                filtered.append(row)
        news_items = filtered

    st.caption(f"{len(news_items)} recent asset impact(s)")
    if not news_items:
        st.info(
            "No classified news impacts yet. This is expected before the news "
            "collector/classifier/adapter has run."
        )
        return

    for row in sorted(
        news_items, key=lambda r: r.get("publication_timestamp_utc") or "", reverse=True
    ):
        with card():
            top = st.columns([5, 1])
            with top[0]:
                st.markdown(f"**{row.get('headline', '(no headline)')}**")
            with top[1]:
                direction = row.get("direction", "neutral")
                level = {"bullish": "good", "bearish": "critical"}.get(direction, "muted")
                status_badge(direction, level)
            source = row.get("source_name")
            quality = (
                " · headline-only (Reuters/Google News RSS)" if source == "Reuters" else ""
            )
            st.caption(
                f"{row.get('publication_timestamp_utc', '')} · {source}{quality} · "
                f"asset: {row.get('asset')} ({row.get('asset_type')}) · "
                f"sentiment {row.get('sentiment', 0):.2f} · confidence {row.get('confidence', 0):.2f}"
            )


def make_equity_figure(equity_items: list[dict]) -> go.Figure:
    df = pd.DataFrame(equity_items)
    df["snapshot_timestamp_utc"] = pd.to_datetime(df["snapshot_timestamp_utc"], utc=True)

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.7, 0.3],
        vertical_spacing=0.06,
        subplot_titles=("Total Equity (USD)", "Drawdown (%)"),
    )
    fig.add_trace(
        go.Scatter(
            x=df["snapshot_timestamp_utc"],
            y=df["total_equity_usd"],
            mode="lines",
            line=dict(color=COLOR_SEQUENTIAL_BLUE, width=2),
            name="Equity",
            hovertemplate="%{x|%Y-%m-%d %H:%M UTC}<br>$%{y:,.2f}<extra></extra>",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=df["snapshot_timestamp_utc"],
            y=df["drawdown_pct"],
            mode="lines",
            fill="tozeroy",
            line=dict(color=COLOR_CRITICAL, width=2),
            fillcolor="rgba(208, 59, 59, 0.20)",
            name="Drawdown",
            hovertemplate="%{x|%Y-%m-%d %H:%M UTC}<br>%{y:.2f}%<extra></extra>",
        ),
        row=2,
        col=1,
    )
    fig.update_layout(
        height=420,
        margin=dict(l=10, r=10, t=40, b=10),
        showlegend=False,
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        hovermode="x unified",
    )
    fig.update_xaxes(gridcolor=COLOR_GRIDLINE, showgrid=True)
    fig.update_yaxes(gridcolor=COLOR_GRIDLINE, showgrid=True)
    return fig


def render_performance_tab(data: dict[str, Any], risk_spec: dict[str, Any]) -> None:
    equity_latest = data.get("equity_latest")
    equity_items = data.get("equity_items") or []
    closed_positions = data.get("closed_positions") or []

    if not equity_latest:
        st.info(
            "No equity snapshots yet. This is expected before the execution "
            "engine has produced at least one mark."
        )
        return

    trade_count = len(closed_positions)
    wins = sum(1 for p in closed_positions if (p.get("net_pnl_usd") or 0) > 0)
    win_rate = wins / trade_count if trade_count else None
    max_dd = max((item.get("drawdown_pct", 0) for item in equity_items), default=0.0)
    avg_trade_pnl = (
        sum(p.get("net_pnl_usd") or 0 for p in closed_positions) / trade_count
        if trade_count
        else None
    )

    cols = st.columns(4)
    with cols[0], card():
        st.metric("Max Drawdown", f"{max_dd:.2f}%")
    with cols[1], card():
        st.metric("Win Rate", f"{win_rate:.1%}" if win_rate is not None else "—")
    with cols[2], card():
        st.metric("Closed Trades", trade_count)
    with cols[3], card():
        st.metric("Avg Trade P&L", f"${avg_trade_pnl:,.2f}" if avg_trade_pnl is not None else "—")

    if len(equity_items) >= 2:
        st.plotly_chart(make_equity_figure(equity_items), use_container_width=True)
    else:
        st.info("Only one equity snapshot exists so far — the chart will appear once more accumulate.")

    if trade_count:
        st.subheader(":material/table_chart: Per-Relationship P&L")
        per_rel: dict[tuple[str, str], dict[str, float]] = {}
        for p in closed_positions:
            key = (p.get("commodity"), p.get("currency"))
            bucket = per_rel.setdefault(key, {"trades": 0, "net_pnl_usd": 0.0, "wins": 0})
            bucket["trades"] += 1
            bucket["net_pnl_usd"] += p.get("net_pnl_usd") or 0.0
            if (p.get("net_pnl_usd") or 0) > 0:
                bucket["wins"] += 1
        rows = [
            {
                "commodity": k[0],
                "currency": k[1],
                "trades": v["trades"],
                "net_pnl_usd": round(v["net_pnl_usd"], 2),
                "win_rate": round(v["wins"] / v["trades"], 3) if v["trades"] else None,
            }
            for k, v in per_rel.items()
        ]
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    st.subheader(":material/pie_chart: Exposure")
    gross_cap_pct = risk_spec.get("maximum_gross_exposure_pct")
    currency_cap_pct = risk_spec.get("maximum_currency_exposure_pct")
    equity_base = equity_latest["total_equity_usd"]

    exp_cols = st.columns(3)
    with exp_cols[0], card():
        st.metric("Gross Exposure", f"${equity_latest['gross_exposure_usd']:,.2f}")
    with exp_cols[1], card():
        st.metric("Net Exposure", f"${equity_latest['net_exposure_usd']:,.2f}")
    with exp_cols[2], card():
        if gross_cap_pct and equity_base:
            gross_pct = equity_latest["gross_exposure_usd"] / equity_base * 100
            st.metric("Gross vs Cap", f"{gross_pct:.1f}%", delta=f"cap {gross_cap_pct * 100:.0f}%", delta_color="off")
        else:
            st.metric("Gross vs Cap", "—")

    open_positions = data.get("open_positions") or []
    if open_positions:
        by_currency: dict[str, float] = {}
        for item in open_positions:
            currency = item.get("currency") or "unknown"
            by_currency[currency] = by_currency.get(currency, 0.0) + float(
                item.get("position_size_usd") or 0.0
            )
        color_map = currency_color_map(list(by_currency.keys()))
        currency_df = pd.DataFrame(
            [{"currency": k, "exposure_usd": v} for k, v in by_currency.items()]
        ).sort_values("exposure_usd", ascending=False)

        fig = go.Figure(
            go.Bar(
                x=currency_df["exposure_usd"],
                y=currency_df["currency"],
                orientation="h",
                marker_color=[color_map[c] for c in currency_df["currency"]],
                hovertemplate="%{y}: $%{x:,.2f}<extra></extra>",
            )
        )
        fig.update_layout(
            height=max(160, 40 * len(currency_df)),
            margin=dict(l=10, r=10, t=10, b=10),
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            xaxis_title="Exposure (USD)",
        )
        fig.update_xaxes(gridcolor=COLOR_GRIDLINE)
        st.plotly_chart(fig, use_container_width=True)

        if currency_cap_pct and equity_base:
            cap_usd = currency_cap_pct * equity_base
            st.caption(
                f"Per-currency exposure cap: {currency_cap_pct * 100:.0f}% of current "
                f"equity ≈ ${cap_usd:,.2f} (computed, not stored)."
            )
            breaches = currency_df[currency_df["exposure_usd"] > cap_usd]
            if not breaches.empty:
                st.warning(
                    "Currency exposure cap exceeded for: " + ", ".join(breaches["currency"])
                )
    else:
        st.caption("No open positions — exposure by currency is zero.")


def render_commodity_map_tab(data: dict[str, Any]) -> None:
    relationships = data.get("relationships") or []
    st.subheader(":material/hub: Active Relationship Universe")
    if relationships:
        df = pd.DataFrame(relationships)
        cols = [
            c
            for c in [
                "commodity",
                "currency",
                "fx_symbol",
                "direction",
                "selected",
                "selection_weight",
                "trailing_trades",
                "trailing_profit_factor",
            ]
            if c in df.columns
        ]
        st.dataframe(df[cols], width="stretch", hide_index=True)
    else:
        st.info("No active relationships loaded.")

    st.subheader(":material/grid_view: Data Coverage")
    st.caption("Latest feature-snapshot market-window coverage per relationship.")
    feature_items = (data.get("features") or {}).get("items", [])
    if not feature_items:
        st.info("No feature snapshots yet — coverage heatmap will appear once the feature engine runs.")
        return

    df = pd.DataFrame(feature_items)
    if "market_window_coverage_pct" not in df.columns:
        return
    df["label"] = df["commodity"] + " / " + df["currency"]
    df = df.sort_values("market_window_coverage_pct")

    def coverage_color(pct: float) -> str:
        if pct >= 95:
            return COLOR_GOOD
        if pct >= 50:
            return COLOR_WARNING
        return COLOR_CRITICAL

    fig = go.Figure(
        go.Bar(
            x=df["market_window_coverage_pct"],
            y=df["label"],
            orientation="h",
            marker_color=[coverage_color(v) for v in df["market_window_coverage_pct"]],
            hovertemplate="%{y}: %{x:.1f}%%<extra></extra>",
        )
    )
    fig.update_layout(
        height=max(300, 22 * len(df)),
        margin=dict(l=10, r=10, t=10, b=10),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        xaxis_title="Market window coverage (%)",
        xaxis_range=[0, 100],
    )
    fig.update_xaxes(gridcolor=COLOR_GRIDLINE)
    st.plotly_chart(fig, use_container_width=True)


@st.fragment(run_every=TICK_SECONDS)
def render_dashboard(
    base_url: str,
    run_id_override: str | None,
    currency_filter: list[str],
    news_limit: int,
    refresh_interval: float,
) -> None:
    client = get_client(base_url)
    data = ensure_fresh_data(client, run_id_override, refresh_interval)

    render_header_and_kpis(data)
    render_system_status_expander(data)

    risk_spec = (data.get("specification") or {}).get("risk", {}) or {}

    tabs = st.tabs(
        [
            ":material/account_balance_wallet: Positions",
            ":material/query_stats: Signals & Features",
            ":material/newspaper: News Feed",
            ":material/monitoring: Performance",
            ":material/hub: Commodity Map",
        ]
    )
    with tabs[0]:
        render_positions_tab(data, currency_filter)
        # Deadlines belong with positions rather than in a tab of their own:
        # the spec's Step 6 asks for five sections and this is information
        # about the positions above, not a sixth subject.
        st.divider()
        render_session_panel(data)
    with tabs[1]:
        render_signals_tab(data, currency_filter)
    with tabs[2]:
        render_news_tab(data, currency_filter, news_limit)
    with tabs[3]:
        render_performance_tab(data, risk_spec)
    with tabs[4]:
        render_commodity_map_tab(data)

    st.caption(
        f"Last data refresh: {data['fetched_at'].isoformat(timespec='seconds')} "
        f"(every {int(refresh_interval)}s)"
    )


def main() -> None:
    base_url, run_id_override, currency_filter, news_limit, refresh_interval = render_sidebar()
    render_dashboard(base_url, run_id_override, currency_filter, news_limit, refresh_interval)


if __name__ == "__main__":
    main()
