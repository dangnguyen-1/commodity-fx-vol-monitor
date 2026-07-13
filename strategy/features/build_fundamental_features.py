from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2
from dotenv import load_dotenv

from strategy.config.asset_fx_mapping import CANDIDATE_ASSET_FX_MAPPINGS
from strategy.config.fundamental_mapping import (
    FUNDAMENTAL_RELATIONSHIP_MAPPINGS,
)


MONTHLY_OUTPUT_PATH = Path(
    "strategy/output/monthly_fundamental_features.csv"
)
DAILY_OUTPUT_PATH = Path(
    "strategy/output/daily_fundamental_features.csv"
)

PUBLICATION_LAG_DAYS = 90
MAX_REPORT_AGE_DAYS = 120

LEVEL_Z_WINDOW_MONTHS = 60
GROWTH_Z_WINDOW_MONTHS = 60
MOMENTUM_Z_WINDOW_MONTHS = 60

MIN_LEVEL_HISTORY = 12
MIN_GROWTH_HISTORY = 12
MIN_MOMENTUM_HISTORY = 12

Z_CLIP = 3.0
FUNDAMENTAL_DIRECTION_THRESHOLD = 0.15

COMPONENT_WEIGHTS = {
    "flow_level_z": 0.35,
    "flow_yoy_z": 0.40,
    "flow_momentum_z": 0.25,
}

TRADE_VALUE_COLUMNS = [
    "exports_usd",
    "imports_usd",
    "net_usd",
]


def get_connection():
    load_dotenv(dotenv_path=".env")

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set in your environment.")

    return psycopg2.connect(database_url)


def load_trade_data(conn) -> pd.DataFrame:
    query = """
        SELECT
            country,
            commodity,
            period,
            exports_usd,
            imports_usd,
            net_usd,
            note
        FROM fundamental_trade_data
        ORDER BY country, commodity, period;
    """

    return pd.read_sql_query(query, conn)


def load_market_date_range(conn) -> tuple[pd.Timestamp, pd.Timestamp]:
    query = """
        SELECT
            MIN(datetime_utc::date) AS start_date,
            MAX(datetime_utc::date) AS end_date
        FROM market_data
        WHERE timeframe IN ('1D', 'D')
          AND datetime_utc >= TIMESTAMPTZ '2010-01-01 00:00:00+00';
    """

    frame = pd.read_sql_query(query, conn)

    if frame.empty or frame.loc[0, "start_date"] is None:
        raise ValueError("No daily market data found from 2010 onward.")

    start_date = pd.Timestamp(frame.loc[0, "start_date"])
    end_date = pd.Timestamp(frame.loc[0, "end_date"])

    return start_date.normalize(), end_date.normalize()


def build_asset_mapping_lookup() -> dict[tuple[str, str], dict]:
    lookup: dict[tuple[str, str], dict] = {}

    for row in CANDIDATE_ASSET_FX_MAPPINGS:
        key = (row["commodity"], row["currency"])

        if key in lookup:
            raise ValueError(
                "Duplicate commodity/currency relationship in "
                f"asset_fx_mapping.py: {key}"
            )

        lookup[key] = row

    return lookup


def validate_fundamental_mappings() -> None:
    asset_lookup = build_asset_mapping_lookup()
    seen: set[tuple[str, str]] = set()

    valid_flow_metrics = set(TRADE_VALUE_COLUMNS)

    for row in FUNDAMENTAL_RELATIONSHIP_MAPPINGS:
        relationship_key = (row["commodity"], row["currency"])

        if relationship_key in seen:
            raise ValueError(
                "Duplicate fundamental relationship mapping: "
                f"{relationship_key}"
            )

        seen.add(relationship_key)

        if relationship_key not in asset_lookup:
            raise ValueError(
                "Fundamental relationship is absent from "
                f"asset_fx_mapping.py: {relationship_key}"
            )

        if row["flow_metric"] not in valid_flow_metrics:
            raise ValueError(
                f"Unsupported flow_metric {row['flow_metric']!r} "
                f"for {relationship_key}"
            )

        if row["fundamental_sign"] not in (-1, 1):
            raise ValueError(
                "fundamental_sign must be -1 or 1 for "
                f"{relationship_key}"
            )


def prepare_trade_data(trade_df: pd.DataFrame) -> pd.DataFrame:
    if trade_df.empty:
        raise ValueError("fundamental_trade_data is empty.")

    trade_df = trade_df.copy()

    required = {
        "country",
        "commodity",
        "period",
        *TRADE_VALUE_COLUMNS,
    }
    missing = sorted(required - set(trade_df.columns))

    if missing:
        raise ValueError(
            "Trade data is missing required columns: "
            + ", ".join(missing)
        )

    trade_df["period"] = (
        trade_df["period"]
        .astype(str)
        .str.replace(r"\.0$", "", regex=True)
        .str.zfill(6)
    )

    valid_period = trade_df["period"].str.fullmatch(r"\d{6}")

    if not valid_period.all():
        bad = trade_df.loc[~valid_period, "period"].head(20).tolist()
        raise ValueError(f"Invalid YYYYMM periods found: {bad}")

    trade_df["report_month"] = pd.to_datetime(
        trade_df["period"] + "01",
        format="%Y%m%d",
        errors="raise",
    )

    trade_df["report_month_end"] = (
        trade_df["report_month"]
        + pd.offsets.MonthEnd(0)
    ).dt.normalize()

    # Conservative point-in-time assumption:
    # monthly trade data becomes usable 90 calendar days after month-end,
    # and no earlier than the following business day.
    trade_df["report_available_date"] = (
        trade_df["report_month_end"]
        + pd.to_timedelta(PUBLICATION_LAG_DAYS, unit="D")
        + pd.offsets.BDay(1)
    ).dt.normalize()

    for column in TRADE_VALUE_COLUMNS:
        trade_df[column] = pd.to_numeric(
            trade_df[column],
            errors="coerce",
        )

    duplicate_mask = trade_df.duplicated(
        subset=["country", "commodity", "period"],
        keep=False,
    )

    if duplicate_mask.any():
        bad = trade_df.loc[
            duplicate_mask,
            ["country", "commodity", "period"],
        ]

        raise ValueError(
            "Duplicate country/commodity/period rows found:\n"
            f"{bad.head(20).to_string(index=False)}"
        )

    return trade_df


def causal_rolling_zscore(
    series: pd.Series,
    *,
    window: int,
    min_periods: int,
) -> pd.Series:
    """
    Normalize the current value using only observations through the
    previous report month.
    """
    historical = series.shift(1)

    rolling_mean = historical.rolling(
        window=window,
        min_periods=min_periods,
    ).mean()

    rolling_std = historical.rolling(
        window=window,
        min_periods=min_periods,
    ).std(ddof=0)

    zscore = (
        (series - rolling_mean)
        / rolling_std.replace(0, np.nan)
    )

    return zscore.clip(-Z_CLIP, Z_CLIP)


def weighted_component_score(group: pd.DataFrame) -> pd.Series:
    numerator = pd.Series(0.0, index=group.index)
    denominator = pd.Series(0.0, index=group.index)

    for column, weight in COMPONENT_WEIGHTS.items():
        available = group[column].notna()

        numerator = numerator + (
            group[column].fillna(0.0) * weight
        )
        denominator = denominator + (
            available.astype(float) * weight
        )

    score = numerator / denominator.replace(0, np.nan)

    return score


def build_one_relationship_monthly(
    trade_df: pd.DataFrame,
    mapping: dict,
    asset_mapping: dict,
) -> pd.DataFrame:
    country = mapping["trade_country"]
    trade_commodity = mapping["trade_commodity"]
    flow_metric = mapping["flow_metric"]

    group = trade_df[
        (trade_df["country"] == country)
        & (trade_df["commodity"] == trade_commodity)
    ].copy()

    if group.empty:
        raise ValueError(
            "No Comtrade rows found for "
            f"{country} / {trade_commodity}"
        )

    group = group.sort_values("report_month").reset_index(drop=True)

    group["flow_value_usd"] = group[flow_metric]
    group["exports_missing"] = group["exports_usd"].isna().astype(int)
    group["imports_missing"] = group["imports_usd"].isna().astype(int)
    group["selected_flow_missing"] = (
        group["flow_value_usd"].isna().astype(int)
    )

    # Trade values are non-negative. log1p stabilizes very large nominal
    # differences across countries, commodities, and time.
    group["flow_log_value"] = np.log1p(
        group["flow_value_usd"].clip(lower=0)
    )

    group["flow_12m_sum"] = (
        group["flow_value_usd"]
        .rolling(window=12, min_periods=9)
        .sum()
    )

    group["flow_12m_log"] = np.log1p(
        group["flow_12m_sum"].clip(lower=0)
    )

    group["flow_yoy_log_growth"] = (
        group["flow_12m_log"]
        - group["flow_12m_log"].shift(12)
    )

    group["flow_3m_average"] = (
        group["flow_value_usd"]
        .rolling(window=3, min_periods=2)
        .mean()
    )

    group["flow_previous_3m_average"] = (
        group["flow_3m_average"].shift(3)
    )

    group["flow_momentum_3m"] = (
        np.log1p(group["flow_3m_average"].clip(lower=0))
        - np.log1p(
            group["flow_previous_3m_average"].clip(lower=0)
        )
    )

    group["flow_level_z"] = causal_rolling_zscore(
        group["flow_log_value"],
        window=LEVEL_Z_WINDOW_MONTHS,
        min_periods=MIN_LEVEL_HISTORY,
    )

    group["flow_yoy_z"] = causal_rolling_zscore(
        group["flow_yoy_log_growth"],
        window=GROWTH_Z_WINDOW_MONTHS,
        min_periods=MIN_GROWTH_HISTORY,
    )

    group["flow_momentum_z"] = causal_rolling_zscore(
        group["flow_momentum_3m"],
        window=MOMENTUM_Z_WINDOW_MONTHS,
        min_periods=MIN_MOMENTUM_HISTORY,
    )

    group["fundamental_component_score"] = weighted_component_score(
        group
    )

    group["fundamental_layer_score"] = (
        mapping["fundamental_sign"]
        * np.tanh(group["fundamental_component_score"] / 1.5)
    )

    # Never use a score when the selected report flow is missing.
    group.loc[
        group["selected_flow_missing"].eq(1),
        "fundamental_layer_score",
    ] = np.nan

    group["fundamental_available"] = (
        group["fundamental_layer_score"].notna()
    ).astype(int)

    group["fundamental_layer_direction"] = np.where(
        group["fundamental_layer_score"].abs()
        >= FUNDAMENTAL_DIRECTION_THRESHOLD,
        np.sign(group["fundamental_layer_score"]),
        0,
    ).astype(int)

    group["commodity"] = mapping["commodity"]
    group["currency"] = mapping["currency"]
    group["fx_symbol"] = asset_mapping["fx_symbol"]
    group["relationship_id"] = (
        mapping["commodity"]
        + "__"
        + mapping["currency"]
        + "__"
        + asset_mapping["fx_symbol"]
    )

    group["trade_country"] = country
    group["trade_commodity"] = trade_commodity
    group["flow_metric"] = flow_metric
    group["fundamental_sign"] = mapping["fundamental_sign"]
    group["fundamental_role"] = mapping["fundamental_role"]

    columns = [
        "report_available_date",
        "report_month",
        "report_month_end",
        "period",
        "relationship_id",
        "commodity",
        "currency",
        "fx_symbol",
        "trade_country",
        "trade_commodity",
        "flow_metric",
        "fundamental_sign",
        "fundamental_role",
        "exports_usd",
        "imports_usd",
        "net_usd",
        "flow_value_usd",
        "exports_missing",
        "imports_missing",
        "selected_flow_missing",
        "flow_12m_sum",
        "flow_yoy_log_growth",
        "flow_momentum_3m",
        "flow_level_z",
        "flow_yoy_z",
        "flow_momentum_z",
        "fundamental_component_score",
        "fundamental_layer_score",
        "fundamental_layer_direction",
        "fundamental_available",
    ]

    return group[columns]


def build_monthly_features(trade_df: pd.DataFrame) -> pd.DataFrame:
    validate_fundamental_mappings()
    asset_lookup = build_asset_mapping_lookup()

    parts = []

    for mapping in FUNDAMENTAL_RELATIONSHIP_MAPPINGS:
        key = (mapping["commodity"], mapping["currency"])
        asset_mapping = asset_lookup[key]

        parts.append(
            build_one_relationship_monthly(
                trade_df,
                mapping,
                asset_mapping,
            )
        )

    monthly = pd.concat(parts, ignore_index=True)

    duplicate_mask = monthly.duplicated(
        subset=["relationship_id", "period"],
        keep=False,
    )

    if duplicate_mask.any():
        bad = monthly.loc[
            duplicate_mask,
            ["relationship_id", "period"],
        ]

        raise ValueError(
            "Duplicate relationship/period rows found:\n"
            f"{bad.head(20).to_string(index=False)}"
        )

    return monthly.sort_values(
        ["relationship_id", "report_month"]
    ).reset_index(drop=True)


def build_daily_features(
    monthly: pd.DataFrame,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> pd.DataFrame:
    calendar = pd.DataFrame(
        {
            "date": pd.date_range(
                start=start_date,
                end=end_date,
                freq="B",
            )
        }
    )

    parts = []

    for relationship_id, reports in monthly.groupby(
        "relationship_id",
        sort=False,
    ):
        reports = reports.sort_values(
            "report_available_date"
        ).copy()

        static_columns = [
            "relationship_id",
            "commodity",
            "currency",
            "fx_symbol",
            "trade_country",
            "trade_commodity",
            "flow_metric",
            "fundamental_sign",
            "fundamental_role",
        ]

        static = {
            column: reports.iloc[0][column]
            for column in static_columns
        }

        daily = pd.merge_asof(
            calendar.sort_values("date"),
            reports.sort_values("report_available_date"),
            left_on="date",
            right_on="report_available_date",
            direction="backward",
            allow_exact_matches=True,
        )

        for column, value in static.items():
            daily[column] = value

        daily["report_age_days"] = (
            daily["date"]
            - daily["report_available_date"]
        ).dt.days.astype("Int64")

        daily["fundamental_stale"] = (
            daily["report_age_days"].gt(MAX_REPORT_AGE_DAYS)
        ).fillna(True).astype(int)

        valid_daily_score = (
            daily["fundamental_available"].eq(1)
            & daily["fundamental_stale"].eq(0)
        )

        daily.loc[
            ~valid_daily_score,
            "fundamental_layer_score",
        ] = np.nan

        daily["fundamental_available"] = (
            valid_daily_score.astype(int)
        )

        daily["fundamental_layer_direction"] = np.where(
            daily["fundamental_layer_score"].abs()
            >= FUNDAMENTAL_DIRECTION_THRESHOLD,
            np.sign(daily["fundamental_layer_score"]),
            0,
        ).astype(int)

        parts.append(daily)

    daily = pd.concat(parts, ignore_index=True)

    selected_columns = [
        "date",
        "relationship_id",
        "commodity",
        "currency",
        "fx_symbol",
        "trade_country",
        "trade_commodity",
        "flow_metric",
        "fundamental_role",
        "period",
        "report_month",
        "report_available_date",
        "report_age_days",
        "fundamental_stale",
        "exports_usd",
        "imports_usd",
        "net_usd",
        "flow_value_usd",
        "exports_missing",
        "imports_missing",
        "selected_flow_missing",
        "flow_12m_sum",
        "flow_yoy_log_growth",
        "flow_momentum_3m",
        "flow_level_z",
        "flow_yoy_z",
        "flow_momentum_z",
        "fundamental_component_score",
        "fundamental_layer_score",
        "fundamental_layer_direction",
        "fundamental_available",
    ]

    daily = daily[selected_columns]

    duplicate_mask = daily.duplicated(
        subset=["relationship_id", "date"],
        keep=False,
    )

    if duplicate_mask.any():
        bad = daily.loc[
            duplicate_mask,
            ["relationship_id", "date"],
        ]

        raise ValueError(
            "Duplicate relationship/date rows found:\n"
            f"{bad.head(20).to_string(index=False)}"
        )

    return daily.sort_values(
        ["date", "commodity", "currency"]
    ).reset_index(drop=True)


def validate_outputs(
    monthly: pd.DataFrame,
    daily: pd.DataFrame,
) -> None:
    future_report_mask = (
        daily["report_available_date"].notna()
        & (daily["report_available_date"] > daily["date"])
    )

    if future_report_mask.any():
        bad = daily.loc[
            future_report_mask,
            [
                "date",
                "relationship_id",
                "period",
                "report_available_date",
            ],
        ]

        raise ValueError(
            "Look-ahead detected: future report used on daily row:\n"
            f"{bad.head(20).to_string(index=False)}"
        )

    invalid_scores = daily["fundamental_layer_score"].dropna().abs() > 1.0

    if invalid_scores.any():
        raise ValueError(
            "fundamental_layer_score contains values outside [-1, 1]."
        )

    unavailable_with_score = (
        daily["fundamental_available"].eq(0)
        & daily["fundamental_layer_score"].notna()
    )

    if unavailable_with_score.any():
        raise ValueError(
            "Unavailable daily fundamental rows contain active scores."
        )

    expected_relationships = len(
        FUNDAMENTAL_RELATIONSHIP_MAPPINGS
    )
    actual_monthly_relationships = monthly[
        "relationship_id"
    ].nunique()
    actual_daily_relationships = daily[
        "relationship_id"
    ].nunique()

    if actual_monthly_relationships != expected_relationships:
        raise ValueError(
            "Monthly relationship count mismatch: "
            f"{actual_monthly_relationships} vs "
            f"{expected_relationships}"
        )

    if actual_daily_relationships != expected_relationships:
        raise ValueError(
            "Daily relationship count mismatch: "
            f"{actual_daily_relationships} vs "
            f"{expected_relationships}"
        )


def main() -> None:
    with get_connection() as conn:
        trade_df = load_trade_data(conn)
        start_date, end_date = load_market_date_range(conn)

    trade_df = prepare_trade_data(trade_df)
    monthly = build_monthly_features(trade_df)
    daily = build_daily_features(
        monthly,
        start_date=start_date,
        end_date=end_date,
    )

    validate_outputs(monthly, daily)

    MONTHLY_OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    monthly.to_csv(MONTHLY_OUTPUT_PATH, index=False)
    daily.to_csv(DAILY_OUTPUT_PATH, index=False)

    print("Fundamental feature build completed")
    print(f"Mappings: {len(FUNDAMENTAL_RELATIONSHIP_MAPPINGS)}")
    print(f"Monthly rows: {len(monthly):,}")
    print(f"Daily rows: {len(daily):,}")
    print(
        "Daily date range: "
        f"{daily['date'].min().date()} to "
        f"{daily['date'].max().date()}"
    )
    print(
        "Daily rows with active fundamental score: "
        f"{int(daily['fundamental_available'].sum()):,}"
    )
    print(
        "Monthly rows with selected flow missing: "
        f"{int(monthly['selected_flow_missing'].sum()):,}"
    )
    print(
        "Daily score range: "
        f"{daily['fundamental_layer_score'].min():.4f} to "
        f"{daily['fundamental_layer_score'].max():.4f}"
    )

    print("\nOutputs:")
    print(f"- {MONTHLY_OUTPUT_PATH}")
    print(f"- {DAILY_OUTPUT_PATH}")


if __name__ == "__main__":
    main()