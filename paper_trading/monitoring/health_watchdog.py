"""Notices when the pipeline breaks, and says so.

Every serious failure this project has had was silent. The Comtrade loader
reported success while committing nothing for six weeks. news-sync, the daily
bars and the FX inverses each went stale because nothing was scheduled to run
them. The market-bar sync was frozen since July. In every case the system
looked fine — nothing crashed, nothing went red — and the gap was found by
someone happening to look.

This is the thing that looks. It checks what should be true right now and
raises a fuss when it is not:

  * every service heartbeat is recent enough for that service's cadence;
  * FX market data is still arriving, which is the check that catches an
    expired TradingView session — the single most likely silent failure,
    since those credentials are browser cookies;
  * no unresolved critical alerts have piled up;
  * the disk the database and its backups share is not filling.

Delivery is a webhook (``ALERT_WEBHOOK_URL`` in .env), which Slack and
Discord both accept. Without one configured everything still runs and prints;
the checks are useful on their own, and a missing webhook should never stop
them.

Repeat notifications are suppressed for ``ALERT_COOLDOWN_MINUTES`` (default
60) so an ongoing outage does not post every five minutes, and a recovery is
announced once when a previously-failing check comes back.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = (
    PROJECT_ROOT / "paper_trading" / "data" / "paper_trading.db"
)
DEFAULT_STATE_PATH = (
    PROJECT_ROOT / "paper_trading" / "data" / "watchdog_state.json"
)

# How stale a heartbeat may get before it counts as a problem, per service.
# Each is comfortably more than the service's own cadence — a single late
# cycle is normal and should not page anyone.
HEARTBEAT_LIMITS_MINUTES: dict[str, int] = {
    "market_data_adapter": 10,      # 1-minute cron
    "strategy_orchestrator": 10,    # 1-minute loop
    "paper_execution_engine": 10,   # 1-minute loop
    "feature_engine": 20,           # 5-minute cadence
    "signal_engine": 20,            # 5-minute cadence
    "news_data_adapter": 45,        # 5-minute cron, bursty by nature
}

# FX trades continuously Sunday 21:00 UTC to Friday 21:00 UTC. Outside that
# window there is nothing to collect and silence is correct.
FX_STALE_LIMIT_MINUTES = 20

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
    New York time, so the window is a little conservative either side of the
    close. Being slightly wrong about a Friday evening is much better than
    paging every weekend, and a genuinely dead feed will still be caught the
    moment the week reopens.
    """
    weekday = now.weekday()  # Monday = 0
    if weekday == 5:  # Saturday
        return False
    if weekday == 4 and now.hour >= 21:  # Friday after the rollover
        return False
    if weekday == 6 and now.hour < 22:  # Sunday before the reopen
        return False
    return True


def check_heartbeats(connection: sqlite3.Connection) -> list[Finding]:
    findings: list[Finding] = []
    now = utc_now()
    rows = connection.execute(
        "SELECT service_name, status, last_heartbeat_utc FROM service_heartbeats"
    ).fetchall()
    seen = {str(row[0]) for row in rows}

    for service, limit in HEARTBEAT_LIMITS_MINUTES.items():
        if service not in seen:
            findings.append(
                Finding(
                    key=f"heartbeat_missing:{service}",
                    severity="critical",
                    message=f"{service} has never reported a heartbeat.",
                )
            )

    for service, status, last in rows:
        service = str(service)
        limit = HEARTBEAT_LIMITS_MINUTES.get(service)
        if limit is None:
            continue
        age = (now - parse_iso(str(last))).total_seconds() / 60.0
        if age > limit:
            findings.append(
                Finding(
                    key=f"heartbeat_stale:{service}",
                    severity="critical",
                    message=(
                        f"{service} last reported {age:.0f} minutes ago "
                        f"(limit {limit})."
                    ),
                )
            )
        elif str(status) == "unhealthy":
            findings.append(
                Finding(
                    key=f"heartbeat_unhealthy:{service}",
                    severity="critical",
                    message=f"{service} reports itself unhealthy.",
                )
            )
    return findings


def check_market_data_flowing(
    connection: sqlite3.Connection,
) -> list[Finding]:
    """Is FX data still arriving?

    This is the TradingView-session check. Those credentials are browser
    cookies with no expiry we control, and when they lapse the collector
    keeps running while collecting nothing — which looks identical to a quiet
    market unless something compares against the clock.
    """
    now = utc_now()
    if not fx_market_should_be_open(now):
        return []

    row = connection.execute(
        """
        SELECT MAX(bar_timestamp_utc)
        FROM market_bars_1m
        WHERE symbol LIKE 'FX%' OR symbol LIKE 'DERIVED%'
        """
    ).fetchone()

    if row is None or row[0] is None:
        return [
            Finding(
                key="fx_no_data",
                severity="critical",
                message="No FX bars in the database at all.",
            )
        ]

    age = (now - parse_iso(str(row[0]))).total_seconds() / 60.0
    if age > FX_STALE_LIMIT_MINUTES:
        return [
            Finding(
                key="fx_stale",
                severity="critical",
                message=(
                    f"Newest FX bar is {age:.0f} minutes old during trading "
                    "hours. Check the TradingView session first — those "
                    "cookies expire and the collector fails quietly."
                ),
            )
        ]
    return []


def check_unresolved_alerts(
    connection: sqlite3.Connection,
) -> list[Finding]:
    rows = connection.execute(
        """
        SELECT alert_type, message, COUNT(*)
        FROM system_alerts
        WHERE resolved = 0 AND severity = 'critical'
        GROUP BY alert_type, message
        """
    ).fetchall()
    return [
        Finding(
            key=f"alert:{row[0]}",
            severity="critical",
            message=f"Unresolved alert x{row[2]}: {row[1]}",
        )
        for row in rows
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
                    f"({usage.free / 1e9:.1f} GB free). The database and its "
                    "backups share this volume."
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
        # A webhook that cannot be reached must not take the watchdog down —
        # the checks and their console output are still worth having.
        print(f"[watchdog] webhook delivery failed: {error}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check pipeline health and notify when something breaks."
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
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
            "[TEST] Watchdog delivery check — "
            f"{utc_now().isoformat(timespec='seconds')}. "
            "Nothing is wrong; this confirms alerts reach you."
        )
        if not os.environ.get("ALERT_WEBHOOK_URL", "").strip():
            print("[watchdog] ALERT_WEBHOOK_URL is not set — nothing to test.")
            raise SystemExit(1)
        delivered = notify(message)
        print(
            "[watchdog] test notification "
            + ("delivered." if delivered else "FAILED to deliver.")
        )
        raise SystemExit(0 if delivered else 1)
    cooldown = float(os.environ.get("ALERT_COOLDOWN_MINUTES", "60"))

    connection = sqlite3.connect(args.database, timeout=30.0)
    try:
        findings = (
            check_heartbeats(connection)
            + check_market_data_flowing(connection)
            + check_unresolved_alerts(connection)
            + check_disk(args.database.parent)
        )
    finally:
        connection.close()

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
        # through to the healthy message — a watchdog that reports "all
        # checks passed" during an ongoing outage is worse than no watchdog.
        print(
            f"[watchdog] {len(findings)} finding(s) still active, "
            f"notification suppressed for {cooldown:.0f}m:"
        )
        for finding in findings:
            print(f"  {finding.line()}")
    elif not args.quiet_when_healthy:
        print(f"[watchdog] all checks passed at {now.isoformat()}")

    save_state(args.state, {"active": active, "checked_at": now.isoformat()})

    # Exit non-zero on an active problem so pm2 logs and any shell caller can
    # tell the difference without parsing this output.
    raise SystemExit(1 if findings else 0)


if __name__ == "__main__":
    main()
