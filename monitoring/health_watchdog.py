"""Checks that the collectors are actually collecting, and alerts when not.

The failure this exists to catch is not a process crashing, which is loud.
It is a collector that keeps running and quietly stops producing rows,
which looks healthy from every angle except the data. So every check reads
freshness out of Postgres rather than asking whether a process is up.

Findings are deduplicated by key and suppressed for ALERT_COOLDOWN_MINUTES,
and a key that stops appearing is reported as RECOVERED.

Run:
    .venv/bin/python3 -m monitoring.health_watchdog
    .venv/bin/python3 -m monitoring.health_watchdog --test
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg2
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_PATH = PROJECT_ROOT / "monitoring" / "watchdog_state.json"

# FX trades continuously from Sunday evening to Friday evening. Outside that
# window silence is correct, so the check is skipped rather than fired.
FX_STALE_LIMIT_MINUTES = 20

# Futures arrive on a measured 10 to 11 minute delay on this data
# entitlement, so the threshold has to clear that before it means anything.
COMMODITY_STALE_LIMIT_MINUTES = 30

# daily-bars-refresh runs at 06:00. Two days allows a weekend plus one
# missed run before anyone is told.
DAILY_BAR_STALE_LIMIT_HOURS = 48

# Comtrade publishes monthly and lags by design. This only catches the
# refresh having stopped entirely, not a slow month at the UN.
TRADE_STALE_LIMIT_DAYS = 75

# Articles arrive continuously, so a long silence means classification has
# stopped, most often because OpenAI credit ran out. That fails silently.
CLASSIFICATION_STALE_LIMIT_MINUTES = 180
ARTICLE_STALE_LIMIT_MINUTES = 240

DISK_WARN_PERCENT = 85.0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass
class Finding:
    key: str
    severity: str
    message: str

    def line(self) -> str:
        icon = {"critical": "CRITICAL", "warning": "WARNING"}.get(
            self.severity, "INFO"
        )
        return f"[{icon}] {self.message}"


def fx_market_should_be_open(now: datetime) -> bool:
    """Whether FX should be producing bars at this instant.

    Approximate on purpose: the weekend edges are treated in UTC rather than
    New York time, so the window is conservative either side of the close.
    Being slightly wrong about a Friday evening beats paging every weekend,
    and a genuinely dead feed is still caught when the week reopens.
    """
    weekday = now.weekday()  # Monday = 0
    if weekday == 5:
        return False
    if weekday == 4 and now.hour >= 21:
        return False
    if weekday == 6 and now.hour < 22:
        return False
    return True


def scalar_minutes(cursor, query: str) -> float | None:
    cursor.execute(query)
    row = cursor.fetchone()
    return None if row is None or row[0] is None else float(row[0])


def check_market_data(cursor, now: datetime) -> list[Finding]:
    """Whether the TradingView feed is still writing bars.

    FX and commodities are reported separately because they fail
    separately and have different thresholds: FX is spot and arrives in
    real time, futures come through on a delayed entitlement.
    """
    findings: list[Finding] = []

    daily_hours = scalar_minutes(
        cursor,
        """
        SELECT EXTRACT(EPOCH FROM (now() - MAX(datetime_utc))) / 3600
        FROM market_data WHERE timeframe = '1D'
        """,
    )
    if daily_hours is not None and daily_hours > DAILY_BAR_STALE_LIMIT_HOURS:
        findings.append(
            Finding(
                key="daily_bars_stale",
                severity="critical",
                message=(
                    f"Newest daily bar is {daily_hours / 24:.1f} days old. "
                    "The dashboard's history comes from this table. Check "
                    "the daily-bars-refresh job."
                ),
            )
        )

    if not fx_market_should_be_open(now):
        return findings

    fx_minutes = scalar_minutes(
        cursor,
        """
        SELECT EXTRACT(EPOCH FROM (now() - MAX(datetime_utc))) / 60
        FROM market_data
        WHERE timeframe = '1' AND symbol LIKE 'FX%'
        """,
    )
    if fx_minutes is not None and fx_minutes > FX_STALE_LIMIT_MINUTES:
        findings.append(
            Finding(
                key="fx_stale",
                severity="critical",
                message=(
                    f"Newest FX bar is {fx_minutes:.0f} minutes old during "
                    "trading hours. Check the TradingView session first, "
                    "since those cookies expire and the collector fails "
                    "quietly."
                ),
            )
        )

    commodity_minutes = scalar_minutes(
        cursor,
        """
        SELECT EXTRACT(EPOCH FROM (now() - MAX(datetime_utc))) / 60
        FROM market_data
        WHERE timeframe = '1'
          AND symbol NOT LIKE 'FX%' AND symbol NOT LIKE 'DERIVED:%'
        """,
    )
    if (
        commodity_minutes is not None
        and commodity_minutes > COMMODITY_STALE_LIMIT_MINUTES
    ):
        findings.append(
            Finding(
                key="commodity_stale",
                severity="warning",
                message=(
                    f"Newest commodity bar is {commodity_minutes:.0f} "
                    "minutes old. Some contracts trade thin sessions, so "
                    "check whether it is one venue or all of them."
                ),
            )
        )

    return findings


def check_trade_data(cursor) -> list[Finding]:
    days = scalar_minutes(
        cursor,
        """
        SELECT EXTRACT(EPOCH FROM (now() - MAX(received_at_utc))) / 86400
        FROM fundamental_trade_data
        """,
    )
    if days is not None and days > TRADE_STALE_LIMIT_DAYS:
        return [
            Finding(
                key="trade_data_stale",
                severity="warning",
                message=(
                    f"Comtrade data has not been refreshed in {days:.0f} "
                    "days. Monthly by nature, so this means the job "
                    "stopped rather than the UN being late."
                ),
            )
        ]
    return []


def check_news(cursor) -> list[Finding]:
    """Collection and classification fail independently, so both are checked.

    An article feed that stops looks identical from the dashboard to a
    classifier that stops, but the fix is completely different.
    """
    findings: list[Finding] = []

    article_minutes = scalar_minutes(
        cursor,
        """
        SELECT EXTRACT(EPOCH FROM (now() - MAX(received_at_utc))) / 60
        FROM news_articles
        """,
    )
    if (
        article_minutes is not None
        and article_minutes > ARTICLE_STALE_LIMIT_MINUTES
    ):
        findings.append(
            Finding(
                key="articles_stale",
                severity="warning",
                message=(
                    f"No news article collected for {article_minutes:.0f} "
                    "minutes. Check the news-stream process."
                ),
            )
        )

    classified_minutes = scalar_minutes(
        cursor,
        """
        SELECT EXTRACT(EPOCH FROM (now() - MAX(created_at_utc))) / 60
        FROM news_sentiment
        """,
    )
    if (
        classified_minutes is not None
        and classified_minutes > CLASSIFICATION_STALE_LIMIT_MINUTES
    ):
        findings.append(
            Finding(
                key="classification_stale",
                severity="critical",
                message=(
                    f"No news classified for {classified_minutes:.0f} "
                    "minutes. Check OpenAI credit first, then the "
                    "news-sentiment-stream process."
                ),
            )
        )

    return findings


def check_openai(cursor) -> list[Finding]:
    """Low-credit warning, only when a starting balance is configured.

    Nothing fires without OPENAI_CREDIT_USD, because a threshold measured
    against an unknown budget would be theatre. Ordinary usage is reported
    by the weekly billing reminder instead.
    """
    credit = float(os.environ.get("OPENAI_CREDIT_USD", "0") or 0)
    if credit <= 0:
        return []

    cursor.execute(
        """
        SELECT COALESCE(SUM(estimated_cost_usd), 0),
               COALESCE(SUM(input_tokens), 0),
               COALESCE(SUM(output_tokens), 0)
        FROM openai_usage
        """
    )
    spent, input_tokens, output_tokens = cursor.fetchone()
    spent = float(spent or 0)

    alert_at = float(os.environ.get("OPENAI_ALERT_REMAINING_USD", "5") or 5)
    remaining = credit - spent
    if remaining > alert_at:
        return []

    return [
        Finding(
            key="openai_credit",
            severity="critical",
            message=(
                f"OpenAI credit down to ${remaining:.2f} of ${credit:.2f} "
                f"(spent ${spent:.2f} on {int(input_tokens):,} in / "
                f"{int(output_tokens):,} out tokens). Top up before "
                "classification stops."
            ),
        )
    ]


def check_disk(path: Path) -> list[Finding]:
    usage = shutil.disk_usage(path)
    used_percent = usage.used / usage.total * 100.0
    if used_percent >= DISK_WARN_PERCENT:
        return [
            Finding(
                key="disk",
                severity="warning",
                message=(
                    f"Disk {used_percent:.0f}% full "
                    f"({usage.free / 1e9:.1f} GB free). The database and "
                    "its backups share this volume."
                ),
            )
        ]
    return []


def load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(state, indent=2, sort_keys=True), encoding="utf-8"
        )
    except OSError as error:
        print(f"[watchdog] could not persist state: {error}")


def notify(message: str) -> bool:
    """Post to the configured webhook. Both Slack and Discord are happy.

    Slack reads `text` and ignores `content`; Discord does the reverse.
    Sending both means one setting works for either without the user having
    to say which they picked.
    """
    url = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
    if not url:
        return False

    payload = json.dumps({"text": message, "content": message}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            # Discord sits behind Cloudflare, which rejects urllib's default
            # "Python-urllib/3.x" agent with a 403 before the request ever
            # reaches the webhook. Slack does not care either way.
            "User-Agent": "commodities-health-watchdog/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError) as error:
        # A webhook that cannot be reached must not take the watchdog down:
        # the checks and their console output are still worth having.
        print(f"[watchdog] webhook delivery failed: {error}")
        return False


def collect_findings(database_url: str) -> list[Finding]:
    now = utc_now()
    try:
        connection = psycopg2.connect(database_url, connect_timeout=15)
    except Exception as error:  # noqa: BLE001
        return [
            Finding(
                key="database",
                severity="critical",
                message=f"Could not reach Postgres: {error}",
            )
        ]

    try:
        connection.set_session(readonly=True, autocommit=True)
        with connection.cursor() as cursor:
            return (
                check_market_data(cursor, now)
                + check_trade_data(cursor)
                + check_news(cursor)
                + check_openai(cursor)
            )
    except Exception as error:  # noqa: BLE001
        return [
            Finding(
                key="check_failed",
                severity="warning",
                message=f"A health check raised: {error}",
            )
        ]
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check collector health and notify when something breaks."
    )
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument(
        "--quiet-when-healthy",
        action="store_true",
        help="Print nothing when every check passes (for cron).",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help=(
            "Send a test notification and exit. Delivery is worth checking "
            "deliberately rather than discovering it never worked during "
            "the outage it was meant to catch."
        ),
    )
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")

    if args.test:
        message = (
            "*commodities pipeline*\n"
            "[TEST] Watchdog delivery check, "
            f"{utc_now().isoformat(timespec='seconds')}. "
            "Nothing is wrong; this confirms alerts reach you."
        )
        if not os.environ.get("ALERT_WEBHOOK_URL", "").strip():
            print("[watchdog] ALERT_WEBHOOK_URL is not set, nothing to test.")
            raise SystemExit(1)
        delivered = notify(message)
        print(
            "[watchdog] test notification "
            + ("delivered." if delivered else "FAILED to deliver.")
        )
        raise SystemExit(0 if delivered else 1)

    cooldown = float(os.environ.get("ALERT_COOLDOWN_MINUTES", "60"))
    findings = collect_findings(os.environ.get("DATABASE_URL", ""))

    now = utc_now()
    state = load_state(args.state)
    previous = state.get("active", {})
    active: dict[str, str] = {}
    to_send: list[str] = []

    for finding in findings:
        last_sent = previous.get(finding.key)
        active[finding.key] = last_sent or now.isoformat()
        due = last_sent is None or (
            now - parse_iso(last_sent) > timedelta(minutes=cooldown)
        )
        if due:
            to_send.append(finding.line())
            active[finding.key] = now.isoformat()

    recovered = [key for key in previous if key not in active]
    for key in recovered:
        to_send.append(f"[RECOVERED] {key}")

    if to_send:
        body = "\n".join(["*commodities pipeline*", *to_send])
        delivered = notify(body)
        print(body)
        status = (
            "sent"
            if delivered
            else "not delivered (ALERT_WEBHOOK_URL unset or unreachable)"
        )
        print(f"[watchdog] {status}")
    elif findings:
        # Still broken, just inside the cooldown. Say so rather than falling
        # through to the healthy message: a watchdog that reports "all checks
        # passed" during an ongoing outage is worse than no watchdog.
        print(
            f"[watchdog] {len(findings)} finding(s) still active, "
            f"notification suppressed for {cooldown:.0f}m:"
        )
        for finding in findings:
            print(f"  {finding.line()}")
    elif not args.quiet_when_healthy:
        print(f"[watchdog] all checks passed at {now.isoformat()}")

    save_state(args.state, {"active": active, "checked_at": now.isoformat()})

    # Exit non-zero on an active problem so the cron log and any shell caller
    # can tell the difference without parsing this output.
    raise SystemExit(1 if findings else 0)


if __name__ == "__main__":
    main()
