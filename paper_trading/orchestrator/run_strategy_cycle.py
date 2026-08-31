"""Runs the strategy's decision loop continuously.

Everything under pm2 until now has been the *collection* layer — market data,
news, trade flows. The strategy's own cycle
(``build_feature_snapshots`` -> ``build_signal_decisions`` ->
``run_paper_execution``) has only ever been run by hand, which is why the
API's /health showed those services frozen at a date in July. This is the
process that runs it.

Two cadences, both taken from the spec rather than hardcoded:

  * features and signals every ``signals.evaluation.interval_minutes``
    (5 minutes), aligned to the wall clock so evaluation timestamps land on
    real five-minute boundaries;
  * execution every minute, because ``paper_ledger`` asks for positions to be
    marked to market every minute — and, more importantly, because exits and
    the session-close deadline are only ever acted on when execution runs.

The ordering matters more than it looks: **execution runs even when the
feature or signal stage has failed.** A broken feature build must never stop
open positions being marked, exited, or force-closed at their session
deadline. Failing to enter a trade is a missed opportunity; failing to exit
one is an open risk, and the no-overnight rule depends on execution getting a
turn no matter what else is broken.

Each stage is called with its default timestamp (``None``) rather than one
this loop computes. The engines resolve their own: features take the latest
completed market timestamp they actually have data for, signals follow the
latest snapshot for the run, execution uses now. They know what data exists;
this loop does not. All three are idempotent — deterministic ids and upserts
throughout — so a repeated or replayed timestamp is harmless, which is what
makes restarts safe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import signal
import sqlite3
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from paper_trading.execution.run_paper_execution import (
    run_paper_execution,
)
from paper_trading.features.build_feature_snapshots import (
    build_feature_snapshots,
)
from paper_trading.signals.build_signal_decisions import (
    build_signal_decisions,
)
from strategy.config.intraday.load_intraday_spec import (
    DEFAULT_SPEC_PATH,
    load_intraday_spec,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = (
    PROJECT_ROOT / "paper_trading" / "data" / "paper_trading.db"
)
ORCHESTRATOR_SERVICE_NAME = "strategy_orchestrator"

# Consecutive failures of one stage before it is escalated to a system alert.
# One failure is usually a late bar; a run of them is a broken pipeline.
FAILURE_ALERT_THRESHOLD = 3


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


@dataclass
class StageHealth:
    """Rolling health of one stage of the cycle."""

    name: str
    last_success_utc: str | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    total_runs: int = 0
    total_failures: int = 0
    last_details: dict[str, Any] = field(default_factory=dict)

    def succeeded(self, details: dict[str, Any]) -> None:
        self.last_success_utc = utc_iso(utc_now())
        self.last_error = None
        self.consecutive_failures = 0
        self.total_runs += 1
        self.last_details = details

    def failed(self, error: BaseException) -> None:
        self.last_error = f"{type(error).__name__}: {error}"
        self.consecutive_failures += 1
        self.total_runs += 1
        self.total_failures += 1

    def summary(self) -> dict[str, Any]:
        return {
            "last_success_utc": self.last_success_utc,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "total_runs": self.total_runs,
            "total_failures": self.total_failures,
        }


class Orchestrator:
    def __init__(
        self,
        *,
        database_path: Path,
        spec_path: Path,
        run_mode: str,
        run_id: str | None,
    ) -> None:
        self.database_path = database_path
        self.spec_path = spec_path
        self.run_mode = run_mode
        self.run_id = run_id

        spec = load_intraday_spec(spec_path)
        self.signal_interval_minutes = int(
            spec["signals"]["evaluation"]["interval_minutes"]
        )
        self.mark_every_minute = bool(
            spec["paper_ledger"]["mark_positions_to_market_every_minute"]
        )
        self.execution_interval_minutes = (
            1 if self.mark_every_minute else self.signal_interval_minutes
        )

        self.stages = {
            "features": StageHealth("features"),
            "signals": StageHealth("signals"),
            "execution": StageHealth("execution"),
        }
        self.running = True
        self.cycles = 0
        self.started_at = utc_now()
        self._last_signal_boundary: datetime | None = None
        self._ran_this_cycle: set[str] = set()

    # -- lifecycle ------------------------------------------------------

    def request_stop(self, signum: int, _frame: Any) -> None:
        # pm2 sends SIGINT on restart and SIGTERM on stop. Finish the cycle in
        # flight rather than dying mid-write; sqlite transactions are short,
        # but a half-written cycle is still worth avoiding.
        print(
            f"[orchestrator] signal {signum} received; "
            "finishing this cycle then stopping.",
            flush=True,
        )
        self.running = False

    # -- helpers --------------------------------------------------------

    def run_stage(
        self,
        name: str,
        call: Callable[[], dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Run one stage, absorbing its failure.

        Returns the stage's details, or None if it raised. Exceptions are
        never allowed out: one stage failing must not take down the loop,
        because execution still has to get its turn.
        """
        stage = self.stages[name]
        try:
            details = call()
        except Exception as error:  # noqa: BLE001 - deliberately broad
            stage.failed(error)
            print(
                f"[orchestrator] {name} failed "
                f"({stage.consecutive_failures} in a row): {error}",
                flush=True,
            )
            traceback.print_exc()
            if stage.consecutive_failures == FAILURE_ALERT_THRESHOLD:
                self.record_alert(
                    severity="critical",
                    alert_type=f"{name}_stage_failing",
                    message=(
                        f"{name} stage has failed "
                        f"{stage.consecutive_failures} times in a row."
                    ),
                    details={"last_error": stage.last_error},
                )
            return None

        stage.succeeded(details)
        self._ran_this_cycle.add(name)
        return details

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def record_alert(
        self,
        *,
        severity: str,
        alert_type: str,
        message: str,
        details: dict[str, Any],
    ) -> None:
        raised_at = utc_iso(utc_now())
        # Same deterministic-id shape the execution engine uses, so a repeated
        # alert updates in place instead of piling up a row a minute while a
        # stage stays broken.
        alert_id = hashlib.sha256(
            "|".join(
                [
                    "orchestrator-alert",
                    self.resolved_run_id(),
                    alert_type,
                    raised_at[:13],  # hour bucket
                ]
            ).encode("utf-8")
        ).hexdigest()
        try:
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO system_alerts (
                        alert_id,
                        run_id,
                        alert_timestamp_utc,
                        severity,
                        service_name,
                        alert_type,
                        message,
                        details_json,
                        resolved,
                        resolved_at_utc
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
                    ON CONFLICT(alert_id)
                    DO UPDATE SET
                        severity = excluded.severity,
                        message = excluded.message,
                        details_json = excluded.details_json
                    """,
                    (
                        alert_id,
                        self.resolved_run_id(),
                        raised_at,
                        severity,
                        ORCHESTRATOR_SERVICE_NAME,
                        alert_type,
                        message,
                        json.dumps(details, sort_keys=True),
                    ),
                )
        except Exception as error:  # noqa: BLE001
            # An alert that cannot be written must not crash the loop either.
            print(
                f"[orchestrator] could not record alert: {error}",
                flush=True,
            )

    def resolved_run_id(self) -> str:
        if self.run_id:
            return self.run_id
        spec = load_intraday_spec(self.spec_path)
        return (
            f"{spec['strategy']['name']}-"
            f"{self.run_mode}-"
            f"v{spec['strategy']['specification_version']}"
        )

    def write_heartbeat(self) -> None:
        failing = [
            name
            for name, stage in self.stages.items()
            if stage.consecutive_failures > 0
        ]
        # Execution failing is what makes the loop unhealthy rather than
        # merely degraded: without it, nothing exits and nothing is forced
        # flat at the session deadline.
        if self.stages["execution"].consecutive_failures > 0:
            status = "unhealthy"
        elif failing:
            status = "degraded"
        else:
            status = "healthy"

        details = {
            "cycles": self.cycles,
            "run_id": self.resolved_run_id(),
            "run_mode": self.run_mode,
            "started_at_utc": utc_iso(self.started_at),
            "signal_interval_minutes": self.signal_interval_minutes,
            "execution_interval_minutes": self.execution_interval_minutes,
            "stages": {
                name: stage.summary()
                for name, stage in self.stages.items()
            },
        }
        try:
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO service_heartbeats (
                        service_name,
                        status,
                        last_heartbeat_utc,
                        details_json,
                        updated_at_utc
                    )
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(service_name)
                    DO UPDATE SET
                        status = excluded.status,
                        last_heartbeat_utc = excluded.last_heartbeat_utc,
                        details_json = excluded.details_json,
                        updated_at_utc = excluded.updated_at_utc
                    """,
                    (
                        ORCHESTRATOR_SERVICE_NAME,
                        status,
                        utc_iso(utc_now()),
                        json.dumps(details, sort_keys=True),
                        utc_iso(utc_now()),
                    ),
                )
        except Exception as error:  # noqa: BLE001
            print(
                f"[orchestrator] could not write heartbeat: {error}",
                flush=True,
            )

    def signal_boundary_due(self, now: datetime) -> bool:
        """Whether a new five-minute evaluation boundary has come round.

        Keyed to the wall-clock boundary rather than "five minutes since the
        last run", so a slow cycle or a restart cannot drift evaluation
        timestamps off the grid the spec expects.
        """
        interval = self.signal_interval_minutes
        boundary = now.replace(
            minute=(now.minute // interval) * interval,
            second=0,
            microsecond=0,
        )
        if self._last_signal_boundary == boundary:
            return False
        self._last_signal_boundary = boundary
        return True

    # -- the loop -------------------------------------------------------

    def cycle(self) -> None:
        now = utc_now()
        self.cycles += 1
        self._ran_this_cycle = set()

        if self.signal_boundary_due(now):
            features = self.run_stage(
                "features",
                lambda: build_feature_snapshots(
                    database_path=self.database_path,
                    spec_path=self.spec_path,
                    evaluation_timestamp=None,
                    run_id=self.run_id,
                    run_mode=self.run_mode,
                ),
            )
            # Signals read the snapshots features just wrote, so there is no
            # point evaluating them when that stage failed.
            if features is not None:
                self.run_stage(
                    "signals",
                    lambda: build_signal_decisions(
                        database_path=self.database_path,
                        spec_path=self.spec_path,
                        decision_timestamp=None,
                        run_id=self.run_id,
                        run_mode=self.run_mode,
                    ),
                )

        # Unconditional: see the module docstring. Exits and session-close
        # enforcement only happen here.
        self.run_stage(
            "execution",
            lambda: run_paper_execution(
                database_path=self.database_path,
                spec_path=self.spec_path,
                run_id=self.run_id,
                run_mode=self.run_mode,
                as_of=None,
            ),
        )

        self.write_heartbeat()

    def sleep_until_next_minute(self) -> None:
        now = utc_now()
        nxt = (now + timedelta(minutes=1)).replace(
            second=0, microsecond=0
        )
        remaining = (nxt - now).total_seconds()
        # Wake often enough that a stop signal is acted on promptly rather
        # than after a full minute.
        while remaining > 0 and self.running:
            time.sleep(min(1.0, remaining))
            remaining = (nxt - utc_now()).total_seconds()

    def run_forever(self) -> None:
        print(
            f"[orchestrator] starting: run_id={self.resolved_run_id()} "
            f"mode={self.run_mode} "
            f"signals every {self.signal_interval_minutes}m, "
            f"execution every {self.execution_interval_minutes}m",
            flush=True,
        )
        while self.running:
            started = time.monotonic()
            self.cycle()
            elapsed = time.monotonic() - started
            print(
                f"[orchestrator] cycle {self.cycles} done in {elapsed:.1f}s "
                f"({self.summary_line()})",
                flush=True,
            )
            if self.running:
                self.sleep_until_next_minute()

        self.write_heartbeat()
        print("[orchestrator] stopped.", flush=True)

    def summary_line(self) -> str:
        # Distinguish "ran and succeeded" from "was not due this cycle".
        # Four cycles in five skip the feature and signal stages by design,
        # and a log line that reports those as `ok` reads as though the whole
        # cycle ran every minute — which would hide a stage that had silently
        # stopped being scheduled.
        parts = []
        for name, stage in self.stages.items():
            if stage.consecutive_failures:
                marker = f"FAIL x{stage.consecutive_failures}"
            elif name in self._ran_this_cycle:
                marker = "ok"
            else:
                marker = "skipped"
            parts.append(f"{name}={marker}")
        return " ".join(parts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the intraday strategy's feature/signal/execution cycle "
            "continuously."
        )
    )
    parser.add_argument(
        "--database", type=Path, default=DEFAULT_DATABASE_PATH
    )
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC_PATH)
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--run-mode",
        choices=["local_replay", "shadow", "live_paper"],
        default="live_paper",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single cycle and exit. For smoke-testing.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    orchestrator = Orchestrator(
        database_path=args.database,
        spec_path=args.spec,
        run_mode=args.run_mode,
        run_id=args.run_id,
    )

    signal.signal(signal.SIGTERM, orchestrator.request_stop)
    signal.signal(signal.SIGINT, orchestrator.request_stop)

    if args.once:
        orchestrator.cycle()
        print(f"[orchestrator] single cycle: {orchestrator.summary_line()}")
        for name, stage in orchestrator.stages.items():
            if stage.last_error:
                print(f"  {name}: {stage.last_error}")
        failed = any(
            stage.consecutive_failures
            for stage in orchestrator.stages.values()
        )
        raise SystemExit(1 if failed else 0)

    orchestrator.run_forever()


if __name__ == "__main__":
    main()
