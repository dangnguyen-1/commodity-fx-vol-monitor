from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

from data_collector.fundamental_data.config.commodities import COMMODITIES
from data_collector.fundamental_data.config.countries import COUNTRIES, ISO3_TO_M49

load_dotenv()

BASE_URL = "https://comtradeapi.un.org/data/v1/get/C/M/HS"
PARTNER_CODE = "0"
START_PERIOD = "201001"
END_PERIOD = os.getenv(
    "COMTRADE_END_PERIOD",
    datetime.now(timezone.utc).strftime("%Y%m"),
)

COUNTRY_BATCH_SIZE = len(COUNTRIES)   # all configured reporters per request
PERIOD_BATCH_SIZE = 12                # one year of monthly periods per request
MAX_RECORDS = 100_000
DAILY_CALL_LIMIT_PER_KEY = 500
MIN_SECONDS_BETWEEN_CALLS_PER_KEY = 1.10
MAX_RETRIES = 4
REQUEST_TIMEOUT_SECONDS = 120

OUTPUT_ROOT = Path("data_collector/fundamental_data/output")
BACKFILL_DIR = OUTPUT_ROOT / "backfill_v3"
CHECKPOINT_DIR = BACKFILL_DIR / "checkpoints"
COMMODITY_DIR = BACKFILL_DIR / "commodities"
STATE_DIR = BACKFILL_DIR / "state"
STATE_PATH = STATE_DIR / "comtrade_key_usage.json"
COMBINED_OUTPUT_PATH = OUTPUT_ROOT / "trade_data_backfill_long.csv"

CHECKPOINT_COLUMNS = [
    "country",
    "commodity",
    "period",
    "flow_code",
    "trade_value_usd",
]
FINAL_COLUMNS = [
    "country",
    "commodity",
    "period",
    "exports_usd",
    "imports_usd",
    "net_usd",
    "note",
]


class KeyUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class RequestJob:
    commodity: str
    hs_codes: tuple[str, ...]
    hs_length: int
    countries: tuple[str, ...]
    country_batch_index: int
    periods: tuple[str, ...]
    flow_code: str

    @property
    def roster_fingerprint(self) -> str:
        """Short digest of the reporters this job was fetched for.

        Part of the checkpoint name because it is part of the request.
        COUNTRY_BATCH_SIZE puts every reporter in one batch, so the batch
        index alone cannot distinguish two different rosters. Without this,
        adding a country would leave every checkpoint filename unchanged
        and the new reporter would never be requested.
        """
        joined = ",".join(sorted(self.countries)).encode()
        return hashlib.blake2s(joined, digest_size=3).hexdigest()

    @property
    def checkpoint_path(self) -> Path:
        return CHECKPOINT_DIR / (
            f"{safe_name(self.commodity)}"
            f"__r{self.country_batch_index:02d}"
            f"__c{self.roster_fingerprint}"
            f"__p{self.periods[0]}-{self.periods[-1]}"
            f"__h{self.hs_length}"
            f"__{self.flow_code}.csv"
        )


def safe_name(value: str) -> str:
    return (
        value.lower()
        .replace(" / ", "_")
        .replace(" ", "_")
        .replace("/", "_")
        .replace(":", "_")
        .replace(",", "_")
    )


def chunk_list(items: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        raise ValueError("Chunk size must be positive.")
    return [items[i:i + size] for i in range(0, len(items), size)]


def group_hs_codes_by_length(hs_codes: list[str]) -> dict[int, list[str]]:
    grouped: dict[int, list[str]] = {}
    for code in hs_codes:
        code = str(code).strip()
        if code:
            grouped.setdefault(len(code), []).append(code)
    return grouped


def make_periods(start_period: str, end_period: str) -> list[str]:
    start_year, start_month = int(start_period[:4]), int(start_period[4:])
    end_year, end_month = int(end_period[:4]), int(end_period[4:])

    if (end_year, end_month) < (start_year, start_month):
        raise ValueError("COMTRADE_END_PERIOD precedes START_PERIOD.")

    periods: list[str] = []
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            if year == start_year and month < start_month:
                continue
            if year == end_year and month > end_month:
                continue
            periods.append(f"{year}{month:02d}")
    return periods


def get_api_keys() -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []

    for index in range(1, 21):
        value = os.getenv(f"COMTRADE_API_KEY_{index}", "").strip()
        if value and not value.lower().startswith("your_key"):
            keys.append((f"key_{index}", value))

    legacy = os.getenv("COMTRADE_API_KEY", "").strip()
    if not keys and legacy and not legacy.lower().startswith("your_key"):
        keys.append(("key_1", legacy))

    if not keys:
        raise ValueError(
            "No API keys found. Add COMTRADE_API_KEY_1, "
            "COMTRADE_API_KEY_2, ... to .env."
        )

    return keys


class UsageTracker:
    def __init__(self, labels: list[str]) -> None:
        self.labels = labels
        self.lock = threading.Lock()
        self.state = self._load()

    @staticmethod
    def _blank_key() -> dict:
        return {
            "calls_used": 0,
            "successful_calls": 0,
            "failed_calls": 0,
            "exhausted": False,
        }

    def _load(self) -> dict:
        today = datetime.now(timezone.utc).date().isoformat()
        state: dict = {}

        if STATE_PATH.exists():
            try:
                state = json.loads(STATE_PATH.read_text())
            except (OSError, json.JSONDecodeError):
                state = {}

        if state.get("date_utc") != today:
            state = {"date_utc": today, "keys": {}}

        state.setdefault("keys", {})
        for label in self.labels:
            state["keys"].setdefault(label, self._blank_key())
        return state

    def _save_locked(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        temp = STATE_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps(self.state, indent=2, sort_keys=True))
        temp.replace(STATE_PATH)

    def can_call(self, label: str) -> bool:
        with self.lock:
            item = self.state["keys"][label]
            return (
                not item["exhausted"]
                and item["calls_used"] < DAILY_CALL_LIMIT_PER_KEY
            )

    def record_attempt(self, label: str) -> None:
        with self.lock:
            item = self.state["keys"][label]
            item["calls_used"] += 1
            if item["calls_used"] >= DAILY_CALL_LIMIT_PER_KEY:
                item["exhausted"] = True
            self._save_locked()

    def record_success(self, label: str) -> None:
        with self.lock:
            self.state["keys"][label]["successful_calls"] += 1
            self._save_locked()

    def record_failure(self, label: str) -> None:
        with self.lock:
            self.state["keys"][label]["failed_calls"] += 1
            self._save_locked()

    def mark_exhausted(self, label: str) -> None:
        with self.lock:
            self.state["keys"][label]["exhausted"] = True
            self._save_locked()

    def print_summary(self) -> None:
        print("\nPer-key usage:")
        with self.lock:
            for label in self.labels:
                item = self.state["keys"][label]
                print(
                    f"- {label}: calls={item['calls_used']}, "
                    f"success={item['successful_calls']}, "
                    f"failed={item['failed_calls']}, "
                    f"exhausted={item['exhausted']}"
                )


def build_jobs(periods: list[str]) -> list[RequestJob]:
    country_batches = chunk_list(COUNTRIES, COUNTRY_BATCH_SIZE)
    period_batches = chunk_list(periods, PERIOD_BATCH_SIZE)
    jobs: list[RequestJob] = []

    for commodity, hs_codes in COMMODITIES:
        for hs_length, codes in group_hs_codes_by_length(hs_codes).items():
            for country_index, countries in enumerate(country_batches, start=1):
                for period_batch in period_batches:
                    for flow_code in ("X", "M"):
                        jobs.append(
                            RequestJob(
                                commodity=commodity,
                                hs_codes=tuple(codes),
                                hs_length=hs_length,
                                countries=tuple(countries),
                                country_batch_index=country_index,
                                periods=tuple(period_batch),
                                flow_code=flow_code,
                            )
                        )
    return jobs


def job_is_complete(job: RequestJob) -> bool:
    if not job.checkpoint_path.exists():
        return False
    try:
        frame = pd.read_csv(job.checkpoint_path, nrows=1)
    except (OSError, pd.errors.EmptyDataError):
        return False
    return set(CHECKPOINT_COLUMNS).issubset(frame.columns)


def normalize_records(job: RequestJob, records: list[dict]) -> pd.DataFrame:
    m49_to_iso3 = {m49: iso3 for iso3, m49 in ISO3_TO_M49.items()}
    totals: dict[tuple[str, str], float] = {}

    for record in records:
        reporter_raw = record.get("reporterCode")

        try:
            reporter = f"{int(reporter_raw):03d}"
        except (TypeError, ValueError):
            continue

        period = str(record.get("period", "")).strip()

        iso3 = m49_to_iso3.get(reporter)

        if iso3 not in job.countries or period not in job.periods:
            continue

        value = float(record.get("primaryValue") or 0.0)
        key = (iso3, period)
        totals[key] = totals.get(key, 0.0) + value

    rows = []
    for country in job.countries:
        for period in job.periods:
            rows.append(
                {
                    "country": country,
                    "commodity": job.commodity,
                    "period": period,
                    "flow_code": job.flow_code,
                    "trade_value_usd": totals.get((country, period)),
                }
            )

    return pd.DataFrame(rows, columns=CHECKPOINT_COLUMNS)


def save_checkpoint(job: RequestJob, frame: pd.DataFrame) -> None:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    temp = job.checkpoint_path.with_suffix(".tmp")
    frame.to_csv(temp, index=False)
    temp.replace(job.checkpoint_path)


def request_job(
    job: RequestJob,
    *,
    label: str,
    api_key: str,
    tracker: UsageTracker,
    session: requests.Session,
) -> None:
    reporter_codes = ",".join(ISO3_TO_M49[c] for c in job.countries)
    params = {
        "reporterCode": reporter_codes,
        "period": ",".join(job.periods),
        "partnerCode": PARTNER_CODE,
        "cmdCode": ",".join(job.hs_codes),
        "flowCode": job.flow_code,
        "includeDesc": "false",
        "breakdownMode": "classic",
        "maxRecords": str(MAX_RECORDS),
    }
    headers = {"Ocp-Apim-Subscription-Key": api_key}

    for attempt in range(1, MAX_RETRIES + 1):
        if not tracker.can_call(label):
            raise KeyUnavailableError(f"{label} reached its daily limit.")

        tracker.record_attempt(label)

        try:
            response = session.get(
                BASE_URL,
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )

            if response.status_code in (401, 403):
                tracker.record_failure(label)
                tracker.mark_exhausted(label)
                raise KeyUnavailableError(
                    f"{label} rejected with HTTP {response.status_code}."
                )

            if response.status_code == 429:
                tracker.record_failure(label)
                retry_after = response.headers.get("Retry-After")
                wait_seconds = float(retry_after) if retry_after else 30.0 * attempt

                if attempt == MAX_RETRIES:
                    tracker.mark_exhausted(label)
                    raise KeyUnavailableError(
                        f"{label} repeatedly received HTTP 429."
                    )

                print(f"[{label}] HTTP 429; waiting {wait_seconds:.1f}s")
                time.sleep(wait_seconds)
                continue

            response.raise_for_status()
            records = response.json().get("data", [])

            if len(records) >= MAX_RECORDS:
                raise RuntimeError(
                    "Response reached MAX_RECORDS. Reduce batch sizes."
                )

            save_checkpoint(job, normalize_records(job, records))
            tracker.record_success(label)
            return

        except KeyUnavailableError:
            raise
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            tracker.record_failure(label)
            if attempt == MAX_RETRIES:
                raise RuntimeError(
                    f"Failed job {job.checkpoint_path.name}: {exc}"
                ) from exc
            wait_seconds = min(60.0, 2.0 ** attempt)
            print(
                f"[{label}] attempt {attempt}/{MAX_RETRIES} failed: "
                f"{exc}; retrying in {wait_seconds:.1f}s"
            )
            time.sleep(wait_seconds)


def worker(
    label: str,
    api_key: str,
    tracker: UsageTracker,
    jobs: queue.Queue[RequestJob],
    stop_event: threading.Event,
) -> None:
    session = requests.Session()
    last_call = 0.0

    while not stop_event.is_set():
        try:
            job = jobs.get_nowait()
        except queue.Empty:
            return

        try:
            if job_is_complete(job):
                continue

            delay = MIN_SECONDS_BETWEEN_CALLS_PER_KEY - (
                time.monotonic() - last_call
            )
            if delay > 0:
                time.sleep(delay)

            try:
                request_job(
                    job,
                    label=label,
                    api_key=api_key,
                    tracker=tracker,
                    session=session,
                )
                last_call = time.monotonic()
                print(
                    f"[DONE] {label} | {job.commodity} | "
                    f"{job.flow_code} | {job.periods[0]}-{job.periods[-1]}"
                )
            except KeyUnavailableError as exc:
                jobs.put(job)
                print(f"[KEY STOPPED] {exc}")
                return
            except Exception as exc:
                jobs.put(job)
                stop_event.set()
                print(f"[FATAL] {exc}")
                return
        finally:
            jobs.task_done()


def combine_checkpoints() -> pd.DataFrame:
    frames = []
    for path in sorted(CHECKPOINT_DIR.glob("*.csv")):
        try:
            frame = pd.read_csv(
                path,
                dtype={
                    "country": str,
                    "commodity": str,
                    "period": str,
                    "flow_code": str,
                },
            )
        except pd.errors.EmptyDataError:
            continue

        if set(CHECKPOINT_COLUMNS).issubset(frame.columns):
            frames.append(frame[CHECKPOINT_COLUMNS])

    if not frames:
        return pd.DataFrame(columns=FINAL_COLUMNS)

    flows = pd.concat(frames, ignore_index=True)
    flows = (
        flows.groupby(
            ["country", "commodity", "period", "flow_code"],
            as_index=False,
            dropna=False,
        )["trade_value_usd"]
        .sum(min_count=1)
    )

    exports = (
        flows.loc[
            flows["flow_code"] == "X",
            ["country", "commodity", "period", "trade_value_usd"],
        ]
        .rename(columns={"trade_value_usd": "exports_usd"})
    )

    imports = (
        flows.loc[
            flows["flow_code"] == "M",
            ["country", "commodity", "period", "trade_value_usd"],
        ]
        .rename(columns={"trade_value_usd": "imports_usd"})
    )

    wide = exports.merge(
        imports,
        on=["country", "commodity", "period"],
        how="outer",
    )

    for column in ("exports_usd", "imports_usd"):
        if column not in wide.columns:
            wide[column] = pd.NA

    has_data = wide["exports_usd"].notna() | wide["imports_usd"].notna()
    wide["net_usd"] = pd.NA
    wide.loc[has_data, "net_usd"] = (
        wide.loc[has_data, "exports_usd"].fillna(0.0)
        - wide.loc[has_data, "imports_usd"].fillna(0.0)
    )
    wide["note"] = ""
    wide.loc[~has_data, "note"] = "No data from reporter"

    combined = wide[FINAL_COLUMNS].sort_values(
        ["commodity", "country", "period"]
    )

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    COMMODITY_DIR.mkdir(parents=True, exist_ok=True)
    combined.to_csv(COMBINED_OUTPUT_PATH, index=False)

    for commodity, _ in COMMODITIES:
        combined[combined["commodity"] == commodity].to_csv(
            COMMODITY_DIR / f"{safe_name(commodity)}.csv",
            index=False,
        )

    return combined.reset_index(drop=True)


def print_plan(jobs: list[RequestJob], key_count: int) -> None:
    completed = sum(job_is_complete(job) for job in jobs)
    remaining = len(jobs) - completed
    daily_capacity = key_count * DAILY_CALL_LIMIT_PER_KEY

    print("UN Comtrade backfill plan")
    print(f"Period range: {START_PERIOD} to {END_PERIOD}")
    print(f"Countries per request: {COUNTRY_BATCH_SIZE}")
    print(f"Months per request: {PERIOD_BATCH_SIZE}")
    print(f"API keys: {key_count}")
    print(f"Total request jobs: {len(jobs)}")
    print(f"Completed checkpoints: {completed}")
    print(f"Remaining API calls: {remaining}")
    print(f"Approximate calls per key: {math.ceil(remaining / key_count)}")
    print(f"Estimated quota days: {math.ceil(remaining / daily_capacity)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-jobs", type=int, default=None)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--combine-only", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    periods = make_periods(START_PERIOD, END_PERIOD)
    keys = get_api_keys()
    all_jobs = build_jobs(periods)

    print_plan(all_jobs, len(keys))

    if args.dry_run:
        return

    if args.combine_only:
        combined = combine_checkpoints()
        print(f"[COMBINED] {len(combined)} rows -> {COMBINED_OUTPUT_PATH}")
        return

    remaining_jobs = [job for job in all_jobs if not job_is_complete(job)]
    if args.max_jobs is not None:
        if args.max_jobs <= 0:
            raise ValueError("--max-jobs must be positive.")
        remaining_jobs = remaining_jobs[:args.max_jobs]

    if not remaining_jobs:
        combined = combine_checkpoints()
        print(f"[COMBINED] {len(combined)} rows -> {COMBINED_OUTPUT_PATH}")
        return

    worker_count = min(
        args.workers or len(keys),
        len(keys),
        len(remaining_jobs),
    )

    tracker = UsageTracker([label for label, _ in keys])
    job_queue: queue.Queue[RequestJob] = queue.Queue()
    for job in remaining_jobs:
        job_queue.put(job)

    stop_event = threading.Event()

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(
                worker,
                label,
                api_key,
                tracker,
                job_queue,
                stop_event,
            )
            for label, api_key in keys[:worker_count]
        ]
        for future in futures:
            future.result()

    tracker.print_summary()

    combined = combine_checkpoints()
    completed = sum(job_is_complete(job) for job in all_jobs)
    remaining = len(all_jobs) - completed

    print(f"\n[COMBINED] {len(combined)} rows -> {COMBINED_OUTPUT_PATH}")
    print(f"Completed checkpoints: {completed}/{len(all_jobs)}")
    print(f"Remaining checkpoints: {remaining}")

    if stop_event.is_set():
        raise RuntimeError(
            "A request job failed. Fix the reported error and rerun; "
            "completed checkpoints will be skipped."
        )

    if remaining == 0:
        print("Historical backfill completed successfully.")
    else:
        print("Backfill is incomplete. Rerun later to resume.")


if __name__ == "__main__":
    main()