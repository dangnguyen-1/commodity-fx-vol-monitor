import os
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

from data_collector.fundamental_data.config.countries import COUNTRIES, ISO3_TO_M49
from data_collector.fundamental_data.config.commodities import COMMODITIES

load_dotenv()

BASE_URL = "https://comtradeapi.un.org/data/v1/get/C/M/HS"
PARTNER_CODE = "0"

START_PERIOD = "201001"
END_YEAR = 2026

COUNTRY_BATCH_SIZE = 5
PERIOD_BATCH_SIZE = 3
SLEEP_SECONDS = 1.5
MAX_RETRIES = 3

OUTPUT_DIR = Path("data_collector/fundamental_data/output/backfill")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def get_api_key() -> str:
    api_key = os.getenv("COMTRADE_API_KEY")
    if not api_key:
        raise ValueError("Missing COMTRADE_API_KEY in .env")
    return api_key


def make_periods(start_period: str, end_year: int) -> list[str]:
    start_year = int(start_period[:4])
    start_month = int(start_period[4:])

    periods = []

    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            if year == start_year and month < start_month:
                continue
            periods.append(f"{year}{month:02d}")

    return periods


def chunk_list(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def group_hs_codes_by_length(hs_codes: list[str]) -> dict[int, list[str]]:
    grouped = {}

    for code in hs_codes:
        grouped.setdefault(len(code), []).append(code)

    return grouped


def request_comtrade(params: dict, api_key: str) -> list[dict]:
    headers = {"Ocp-Apim-Subscription-Key": api_key}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(
                BASE_URL,
                params=params,
                headers=headers,
                timeout=90,
            )

            if response.status_code in (403, 429):
                print("Quota/rate limit hit. Stop for today and rerun tomorrow.")
                raise RuntimeError("quota_or_rate_limit")

            response.raise_for_status()
            return response.json().get("data", [])

        except RuntimeError:
            raise

        except requests.RequestException as exc:
            wait_time = SLEEP_SECONDS * attempt
            print(f"    Request failed attempt {attempt}/{MAX_RETRIES}: {exc}")
            time.sleep(wait_time)

    return []


def fetch_trade_flow(
    hs_codes: list[str],
    flow_code: str,
    reporter_codes: str,
    periods: list[str],
    api_key: str,
) -> list[dict]:
    records = []

    for codes in group_hs_codes_by_length(hs_codes).values():
        for period_batch in chunk_list(periods, PERIOD_BATCH_SIZE):
            params = {
                "reporterCode": reporter_codes,
                "period": ",".join(period_batch),
                "partnerCode": PARTNER_CODE,
                "cmdCode": ",".join(codes),
                "flowCode": flow_code,
                "includeDesc": "false",
            }

            records.extend(request_comtrade(params, api_key))
            time.sleep(SLEEP_SECONDS)

    return records


def aggregate_records(records: list[dict]) -> dict[tuple[str, str], float]:
    totals = {}

    for record in records:
        reporter = str(record.get("reporterCode", "")).strip()
        period = str(record.get("period", "")).strip()
        value = float(record.get("primaryValue") or 0)

        if not reporter or not period:
            continue

        key = (reporter, period)
        totals[key] = totals.get(key, 0.0) + value

    return totals


def build_rows(
    commodity: str,
    exports: dict[tuple[str, str], float],
    imports: dict[tuple[str, str], float],
) -> list[dict]:
    rows = []
    m49_to_iso3 = {m49: iso3 for iso3, m49 in ISO3_TO_M49.items()}
    all_keys = set(exports.keys()) | set(imports.keys())

    for m49, period in sorted(all_keys):
        iso3 = m49_to_iso3.get(m49)

        if iso3 is None:
            continue

        export_value = exports.get((m49, period))
        import_value = imports.get((m49, period))
        net_value = (export_value or 0.0) - (import_value or 0.0)

        rows.append(
            {
                "country": iso3,
                "commodity": commodity,
                "period": period,
                "exports_usd": export_value,
                "imports_usd": import_value,
                "net_usd": net_value,
                "note": "",
            }
        )

    return rows


def output_path_for_commodity(commodity: str) -> Path:
    safe_name = (
        commodity.lower()
        .replace(" / ", "_")
        .replace(" ", "_")
        .replace("/", "_")
    )
    return OUTPUT_DIR / f"{safe_name}.csv"


def backfill_commodity(
    commodity: str,
    hs_codes: list[str],
    periods: list[str],
    api_key: str,
) -> None:
    output_path = output_path_for_commodity(commodity)

    if output_path.exists():
        print(f"[SKIP] {commodity} already exists: {output_path}")
        return

    rows = []

    for country_batch in chunk_list(COUNTRIES, COUNTRY_BATCH_SIZE):
        reporter_codes = ",".join(ISO3_TO_M49[iso3] for iso3 in country_batch)

        print(f"    countries={','.join(country_batch)}")

        export_records = fetch_trade_flow(
            hs_codes=hs_codes,
            flow_code="X",
            reporter_codes=reporter_codes,
            periods=periods,
            api_key=api_key,
        )

        import_records = fetch_trade_flow(
            hs_codes=hs_codes,
            flow_code="M",
            reporter_codes=reporter_codes,
            periods=periods,
            api_key=api_key,
        )

        exports = aggregate_records(export_records)
        imports = aggregate_records(import_records)
        rows.extend(build_rows(commodity, exports, imports))

    df = pd.DataFrame(rows)
    df.to_csv(output_path, index=False)

    print(f"[SAVED] {commodity}: {len(df)} rows -> {output_path}")


def combine_outputs() -> None:
    files = sorted(OUTPUT_DIR.glob("*.csv"))

    if not files:
        print("No backfill files to combine.")
        return

    df = pd.concat((pd.read_csv(file) for file in files), ignore_index=True)

    combined_path = Path("data_collector/fundamental_data/output/trade_data_backfill_long.csv")
    df.to_csv(combined_path, index=False)

    print(f"[COMBINED] {len(df)} rows -> {combined_path}")


def main() -> None:
    api_key = get_api_key()
    periods = make_periods(START_PERIOD, END_YEAR)

    print(f"Historical backfill from {periods[0]} to {periods[-1]}")
    print("This may exceed the daily free quota. Rerun tomorrow to resume.")
    print()

    try:
        for index, (commodity, hs_codes) in enumerate(COMMODITIES, start=1):
            print(f"[{index}/{len(COMMODITIES)}] {commodity} HS={','.join(hs_codes)}")
            backfill_commodity(commodity, hs_codes, periods, api_key)

    except RuntimeError as exc:
        if str(exc) == "quota_or_rate_limit":
            print("Stopped safely because quota/rate limit was reached.")
        else:
            raise

    combine_outputs()


if __name__ == "__main__":
    main()