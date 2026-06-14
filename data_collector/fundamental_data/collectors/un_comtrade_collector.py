import os
import time
from pathlib import Path

import pandas as pd
import requests

from dotenv import load_dotenv
load_dotenv()

from data_collector.fundamental_data.config.countries import COUNTRIES, ISO3_TO_M49
from data_collector.fundamental_data.config.commodities import COMMODITIES


BASE_URL = "https://comtradeapi.un.org/data/v1/get"
TYPE_CODE = "C"
FREQ_CODE = "M"
CL_CODE = "HS"
PARTNER_CODE = "0"
SLEEP_SECONDS = 1.1

OUTPUT_DIR = Path("data_collector/fundamental_data/output")
OUTPUT_LONG = OUTPUT_DIR / "trade_data_long.csv"
OUTPUT_MATRIX = OUTPUT_DIR / "trade_data_matrix.csv"


def get_api_key() -> str:
    api_key = os.getenv("COMTRADE_API_KEY")

    if not api_key:
        raise ValueError(
            "Missing COMTRADE_API_KEY. Add it to your .env file or export it in your shell."
        )

    return api_key


def group_hs_codes_by_length(hs_codes: list[str]) -> dict[int, list[str]]:
    grouped = {}

    for code in hs_codes:
        grouped.setdefault(len(code), []).append(code)

    return grouped


def fetch_trade_flow(
    hs_codes: list[str],
    flow_code: str,
    reporter_codes: str,
    api_key: str,
) -> list[dict]:
    records = []
    url = f"{BASE_URL}/{TYPE_CODE}/{FREQ_CODE}/{CL_CODE}"

    for codes in group_hs_codes_by_length(hs_codes).values():
        params = {
            "reporterCode": reporter_codes,
            "period": "recent",
            "partnerCode": PARTNER_CODE,
            "cmdCode": ",".join(codes),
            "flowCode": flow_code,
            "includeDesc": "false",
        }

        headers = {
            "Ocp-Apim-Subscription-Key": api_key,
        }

        try:
            response = requests.get(url, params=params, headers=headers, timeout=90)
            response.raise_for_status()
            records.extend(response.json().get("data", []))
        except requests.RequestException as exc:
            print(f"WARNING: {flow_code} request failed for HS={codes}: {exc}")

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


def build_missing_rows(
    commodity: str,
    all_keys: set[tuple[str, str]],
) -> list[dict]:
    rows = []
    periods_seen = {period for _, period in all_keys}

    for iso3 in COUNTRIES:
        m49 = ISO3_TO_M49[iso3]
        country_periods = {period for reporter, period in all_keys if reporter == m49}

        for period in periods_seen - country_periods:
            rows.append(
                {
                    "country": iso3,
                    "commodity": commodity,
                    "period": period,
                    "exports_usd": None,
                    "imports_usd": None,
                    "net_usd": None,
                    "note": "No data from reporter",
                }
            )

    return rows


def chunk_list(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def collect_trade_data() -> pd.DataFrame:
    api_key = get_api_key()
    m49_to_iso3 = {m49: iso3 for iso3, m49 in ISO3_TO_M49.items()}

    rows = []

    for index, (commodity, hs_codes) in enumerate(COMMODITIES, start=1):
        print(f"[{index}/{len(COMMODITIES)}] {commodity}")

        if hs_codes is None:
            for iso3 in COUNTRIES:
                rows.append(
                    {
                        "country": iso3,
                        "commodity": commodity,
                        "period": None,
                        "exports_usd": None,
                        "imports_usd": None,
                        "net_usd": None,
                        "note": "No HS code",
                    }
                )
            continue

        export_records = []
        import_records = []

        for country_batch in chunk_list(COUNTRIES, 5):
            reporter_codes = ",".join(ISO3_TO_M49[iso3] for iso3 in country_batch)

            export_records.extend(
                fetch_trade_flow(hs_codes, "X", reporter_codes, api_key)
            )
            import_records.extend(
                fetch_trade_flow(hs_codes, "M", reporter_codes, api_key)
            )

        exports = aggregate_records(export_records)
        imports = aggregate_records(import_records)
        all_keys = set(exports.keys()) | set(imports.keys())

        for m49, period in all_keys:
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

        rows.extend(build_missing_rows(commodity, all_keys))

    return pd.DataFrame(rows)


def save_outputs(df: pd.DataFrame) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    df.to_csv(OUTPUT_LONG, index=False)
    print(f"Saved: {OUTPUT_LONG}")

    valid = df.dropna(subset=["net_usd"])

    if valid.empty:
        print("No valid data for matrix output.")
        return

    coverage = valid.groupby("period").apply(
        lambda group: group[["country", "commodity"]].drop_duplicates().shape[0]
    )

    best_period = coverage.idxmax()
    snapshot = valid[valid["period"] == best_period]

    matrix = (
        snapshot.assign(net_bn=lambda data: data["net_usd"] / 1e9)
        .pivot_table(
            index="country",
            columns="commodity",
            values="net_bn",
            aggfunc="sum",
        )
        .reindex(index=COUNTRIES)
        .round(2)
    )

    matrix.to_csv(OUTPUT_MATRIX)
    print(f"Saved: {OUTPUT_MATRIX}")
    print(f"Matrix period: {best_period}")


def main() -> None:
    print(
        f"Pulling UN Comtrade data for {len(COUNTRIES)} countries "
        f"and {len(COMMODITIES)} commodities."
    )

    df = collect_trade_data()
    save_outputs(df)


if __name__ == "__main__":
    main()