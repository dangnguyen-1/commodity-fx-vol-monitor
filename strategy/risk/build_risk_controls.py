from pathlib import Path
import pandas as pd


INPUT_PATH = Path("strategy/output/daily_sized_trades.csv")
OUTPUT_PATH = Path("strategy/output/daily_risk_approved_trades.csv")


PORTFOLIO_VALUE = 100_000

# New-trade exposure limits per day.
# Full open-position exposure will be handled more carefully inside the backtest.
MAX_DAILY_NEW_GROSS_EXPOSURE_PCT = 0.10
MAX_CURRENCY_NEW_GROSS_EXPOSURE_PCT = 0.04

# Crowding controls.
MAX_POSITIONS_PER_DAY = 5
MAX_POSITIONS_PER_CURRENCY_PER_DAY = 2
MAX_POSITIONS_PER_COMMODITY_PER_DAY = 1
MAX_POSITIONS_PER_FX_SYMBOL_PER_DAY = 1

# Minimum position after any risk scaling.
MIN_POSITION_PCT = 0.001

# Rule priority: strictest / highest-quality setups get first access to risk budget.
RULE_PRIORITY = {
    "confirmed_divergence": 3,
    "confirmed": 2,
    "baseline": 1,
    "no_trade": 0,
}


REQUIRED_COLUMNS = [
    "date",
    "relationship_id",
    "commodity",
    "currency",
    "fx_symbol",
    "primary_trade_rule",
    "trade_candidate",
    "trade_direction",
    "has_position",
    "position_size_usd",
    "signed_position_usd",
    "position_size_pct",
    "combined_trade_score",
    "confirmation_score",
    "divergence_score",
    "default_holding_period_days",
]


def validate_input_columns(df: pd.DataFrame) -> None:
    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]

    if missing:
        raise ValueError(
            "Missing required columns in daily_sized_trades.csv:\n"
            + "\n".join(f"- {col}" for col in missing)
        )


def initialize_risk_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["risk_approved"] = 0
    df["risk_rejection_reason"] = "not_a_trade"
    df["risk_adjusted_position_size_usd"] = 0.0
    df["risk_adjusted_position_pct"] = 0.0
    df["risk_adjusted_signed_position_usd"] = 0.0
    df["risk_adjusted_signed_position_pct"] = 0.0
    df["risk_was_scaled"] = 0

    df.loc[
        (df["trade_candidate"] == 1) & (df["has_position"] == 0),
        "risk_rejection_reason",
    ] = "position_too_small"

    return df


def add_risk_priority(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["risk_rule_priority"] = (
        df["primary_trade_rule"]
        .map(RULE_PRIORITY)
        .fillna(0)
        .astype(int)
    )

    return df


def approve_daily_trades(day_df: pd.DataFrame) -> pd.DataFrame:
    day_df = day_df.copy()

    max_daily_gross_usd = PORTFOLIO_VALUE * MAX_DAILY_NEW_GROSS_EXPOSURE_PCT
    max_currency_gross_usd = PORTFOLIO_VALUE * MAX_CURRENCY_NEW_GROSS_EXPOSURE_PCT
    min_position_usd = PORTFOLIO_VALUE * MIN_POSITION_PCT

    candidates = day_df[
        (day_df["trade_candidate"] == 1)
        & (day_df["has_position"] == 1)
        & (day_df["position_size_usd"] > 0)
    ].copy()

    if candidates.empty:
        return day_df

    candidates = candidates.sort_values(
        [
            "risk_rule_priority",
            "combined_trade_score",
            "position_size_usd",
            "relationship_id",
        ],
        ascending=[False, False, False, True],
        kind="mergesort",
    )

    approved_count = 0
    daily_gross_used = 0.0
    currency_gross_used: dict[str, float] = {}
    currency_position_count: dict[str, int] = {}
    commodity_position_count: dict[str, int] = {}
    fx_symbol_position_count: dict[str, int] = {}

    for idx, row in candidates.iterrows():
        currency = row["currency"]
        commodity = row["commodity"]
        fx_symbol = row["fx_symbol"]
        requested_size = float(row["position_size_usd"])

        currency_gross_used.setdefault(currency, 0.0)
        currency_position_count.setdefault(currency, 0)
        commodity_position_count.setdefault(commodity, 0)
        fx_symbol_position_count.setdefault(fx_symbol, 0)

        if approved_count >= MAX_POSITIONS_PER_DAY:
            day_df.loc[idx, "risk_rejection_reason"] = "max_positions_per_day"
            continue

        if currency_position_count[currency] >= MAX_POSITIONS_PER_CURRENCY_PER_DAY:
            day_df.loc[idx, "risk_rejection_reason"] = "max_positions_per_currency"
            continue

        if commodity_position_count[commodity] >= MAX_POSITIONS_PER_COMMODITY_PER_DAY:
            day_df.loc[idx, "risk_rejection_reason"] = "max_positions_per_commodity"
            continue
        
        if fx_symbol_position_count[fx_symbol] >= MAX_POSITIONS_PER_FX_SYMBOL_PER_DAY:
            day_df.loc[idx, "risk_rejection_reason"] = (
                "max_positions_per_fx_symbol"
            )
            continue

        remaining_daily_capacity = max_daily_gross_usd - daily_gross_used
        remaining_currency_capacity = (
            max_currency_gross_usd - currency_gross_used[currency]
        )

        allowed_size = min(
            requested_size,
            remaining_daily_capacity,
            remaining_currency_capacity,
        )

        if allowed_size < min_position_usd:
            day_df.loc[idx, "risk_rejection_reason"] = "insufficient_risk_capacity"
            continue

        was_scaled = int(allowed_size < requested_size)

        day_df.loc[idx, "risk_approved"] = 1
        day_df.loc[idx, "risk_rejection_reason"] = (
            "approved_scaled" if was_scaled else "approved"
        )
        day_df.loc[idx, "risk_adjusted_position_size_usd"] = allowed_size
        day_df.loc[idx, "risk_adjusted_position_pct"] = allowed_size / PORTFOLIO_VALUE
        day_df.loc[idx, "risk_adjusted_signed_position_usd"] = (
            row["trade_direction"] * allowed_size
        )
        day_df.loc[idx, "risk_adjusted_signed_position_pct"] = (
            row["trade_direction"] * allowed_size / PORTFOLIO_VALUE
        )
        day_df.loc[idx, "risk_was_scaled"] = was_scaled

        approved_count += 1
        daily_gross_used += allowed_size
        currency_gross_used[currency] += allowed_size
        currency_position_count[currency] += 1
        commodity_position_count[commodity] += 1
        fx_symbol_position_count[fx_symbol] += 1

    return day_df


def apply_risk_controls(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    parts = []

    for _, day_df in df.groupby("date", sort=False):
        parts.append(approve_daily_trades(day_df))

    if not parts:
        return df

    df = pd.concat(parts, ignore_index=True)

    return df


def add_daily_risk_summary_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    approved = df[df["risk_approved"] == 1].copy()

    if approved.empty:
        df["daily_approved_gross_exposure_usd"] = 0.0
        df["daily_approved_gross_exposure_pct"] = 0.0
        df["daily_approved_position_count"] = 0
        return df

    daily_summary = (
        approved.groupby("date")
        .agg(
            daily_approved_gross_exposure_usd=(
                "risk_adjusted_position_size_usd",
                "sum",
            ),
            daily_approved_position_count=("risk_approved", "sum"),
        )
        .reset_index()
    )

    daily_summary["daily_approved_gross_exposure_pct"] = (
        daily_summary["daily_approved_gross_exposure_usd"] / PORTFOLIO_VALUE
    )

    df = df.merge(daily_summary, on="date", how="left")

    fill_cols = [
        "daily_approved_gross_exposure_usd",
        "daily_approved_gross_exposure_pct",
        "daily_approved_position_count",
    ]

    for col in fill_cols:
        df[col] = df[col].fillna(0)

    df["daily_approved_position_count"] = df[
        "daily_approved_position_count"
    ].astype(int)

    return df


def build_risk_controls(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()
    validate_input_columns(df)

    df["date"] = pd.to_datetime(df["date"])

    df = initialize_risk_columns(df)
    df = add_risk_priority(df)
    df = apply_risk_controls(df)
    df = add_daily_risk_summary_columns(df)

    front_cols = [
        "date",
        "relationship_id",
        "commodity",
        "currency",
        "fx_symbol",
        "primary_trade_rule",
        "trade_candidate",
        "trade_direction",
        "risk_approved",
        "risk_rejection_reason",
        "risk_adjusted_position_size_usd",
        "risk_adjusted_position_pct",
        "risk_adjusted_signed_position_usd",
        "risk_adjusted_signed_position_pct",
        "risk_was_scaled",
        "daily_approved_gross_exposure_usd",
        "daily_approved_gross_exposure_pct",
        "daily_approved_position_count",
        "position_size_usd",
        "position_size_pct",
        "combined_trade_score",
        "confirmation_score",
        "divergence_score",
        "layers_triggered",
        "default_holding_period_days",
    ]

    remaining_cols = [col for col in df.columns if col not in front_cols]
    df = df[front_cols + remaining_cols]

    return df.sort_values(["date", "commodity", "currency"]).reset_index(drop=True)


def main() -> None:
    if not INPUT_PATH.exists():
        raise FileNotFoundError(f"Missing input file: {INPUT_PATH}")

    df = pd.read_csv(INPUT_PATH)
    risk_controlled = build_risk_controls(df)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    risk_controlled.to_csv(OUTPUT_PATH, index=False)

    print(f"Saved risk-approved trades to {OUTPUT_PATH}")
    print(f"Rows: {len(risk_controlled)}")
    print(f"Columns: {len(risk_controlled.columns)}")

    if not risk_controlled.empty:
        print("\nRisk approval summary:")
        print(risk_controlled["risk_approved"].value_counts(dropna=False))

        print("\nRejection / approval reasons:")
        print(risk_controlled["risk_rejection_reason"].value_counts(dropna=False))

        print("\nApproved trades by rule:")
        print(
            risk_controlled[risk_controlled["risk_approved"] == 1][
                "primary_trade_rule"
            ].value_counts(dropna=False)
        )

        print("\nApproved gross exposure by date:")
        daily = (
            risk_controlled.groupby("date")[
                "daily_approved_gross_exposure_usd"
            ]
            .max()
            .sort_index()
        )
        print(daily.tail(15))

        print("\nMax approved daily gross exposure:")
        print(daily.max())


if __name__ == "__main__":
    main()