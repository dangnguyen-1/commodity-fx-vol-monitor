from pathlib import Path

import numpy as np
import pandas as pd


INPUT_PATH = Path("strategy/output/daily_trade_candidates.csv")
OUTPUT_PATH = Path("strategy/output/daily_sized_trades.csv")


PORTFOLIO_VALUE = 100_000

# Position caps
BASE_POSITION_PCT = 0.03      # normal max before signal/vol adjustments
MAX_POSITION_PCT = 0.05       # hard cap per trade
MIN_POSITION_PCT = 0.001      # ignore tiny trades below 0.10%

# Signal curvature
# >1 punishes weak confirmation scores more aggressively.
CONFIRMATION_ALPHA = 1.5

# Volatility normalization
VOL_LOOKBACK = 20
MIN_VOL_OBS = 5

# Layer agreement multipliers
LAYER_MULTIPLIERS = {
    0: 0.0,
    1: 0.5,
    2: 0.8,
    3: 1.0,
}

# Rule multipliers
# Baseline is intentionally smaller.
RULE_MULTIPLIERS = {
    "baseline": 0.60,
    "confirmed": 1.00,
    "confirmed_divergence": 1.20,
    "no_trade": 0.0,
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
    "confirmation_score",
    "divergence_score",
    "combined_trade_score",
    "layers_triggered",
    "fx_volatility_20d",
]


def validate_input_columns(df: pd.DataFrame) -> None:
    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]

    if missing:
        raise ValueError(
            "Missing required columns in daily_trade_candidates.csv:\n"
            + "\n".join(f"- {col}" for col in missing)
        )


def safe_clip(series: pd.Series, lower: float, upper: float) -> pd.Series:
    return series.clip(lower=lower, upper=upper)


def add_volatility_normalization(df: pd.DataFrame) -> pd.DataFrame:
    """
    Build a volatility penalty using FX realized volatility.

    fx_volatility_norm > 1:
        FX is more volatile than usual, so size should shrink.

    fx_volatility_norm < 1:
        FX is calmer than usual, so size can be slightly larger.

    For rows with limited history, we use neutral norm = 1.0.
    """
    df = df.copy()
    parts = []

    for relationship_id, group in df.groupby("relationship_id", sort=False):
        group = group.sort_values("date").copy()

        rolling_vol_mean = (
            group["fx_volatility_20d"]
            .rolling(window=VOL_LOOKBACK, min_periods=MIN_VOL_OBS)
            .mean()
        )

        group["fx_volatility_norm"] = (
            group["fx_volatility_20d"] / rolling_vol_mean.replace(0, np.nan)
        )

        parts.append(group)

    df = pd.concat(parts, ignore_index=True) if parts else df

    df["fx_volatility_norm"] = df["fx_volatility_norm"].replace(
        [np.inf, -np.inf],
        np.nan,
    )

    df["fx_volatility_norm"] = df["fx_volatility_norm"].fillna(1.0)

    # Avoid extreme sizing from very low/high short-sample volatility.
    df["fx_volatility_norm"] = df["fx_volatility_norm"].clip(0.50, 2.00)

    return df


def add_sizing_components(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["confirmation_scaled"] = (
        df["confirmation_score"].clip(0.0, 1.0) ** CONFIRMATION_ALPHA
    )

    df["layer_multiplier"] = (
        df["layers_triggered"]
        .map(LAYER_MULTIPLIERS)
        .fillna(0.0)
    )

    df["rule_multiplier"] = (
        df["primary_trade_rule"]
        .map(RULE_MULTIPLIERS)
        .fillna(0.0)
    )

    # Timing multiplier:
    # - baseline and confirmed trades do not require divergence, so they get neutral 1.0.
    # - confirmed divergence trades get rewarded by divergence_score.
    df["divergence_timing_multiplier"] = 1.0

    divergence_mask = df["primary_trade_rule"] == "confirmed_divergence"

    df.loc[divergence_mask, "divergence_timing_multiplier"] = (
        df.loc[divergence_mask, "divergence_score"]
        .clip(0.0, 1.0)
    )

    return df


def add_position_sizes(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    base_notional = PORTFOLIO_VALUE * BASE_POSITION_PCT
    hard_cap = PORTFOLIO_VALUE * MAX_POSITION_PCT
    min_notional = PORTFOLIO_VALUE * MIN_POSITION_PCT

    df["base_notional_usd"] = base_notional
    df["hard_cap_usd"] = hard_cap
    df["min_notional_usd"] = min_notional

    raw_size = (
        df["base_notional_usd"]
        * df["confirmation_scaled"]
        * df["layer_multiplier"]
        * df["rule_multiplier"]
        * df["divergence_timing_multiplier"]
        / df["fx_volatility_norm"].replace(0, np.nan)
    )

    raw_size = raw_size.replace([np.inf, -np.inf], np.nan).fillna(0.0)

    df["pre_cap_position_size_usd"] = raw_size

    df["position_size_usd"] = np.minimum(
        df["pre_cap_position_size_usd"],
        df["hard_cap_usd"],
    )

    # No position for non-trades.
    df.loc[df["trade_candidate"] == 0, "position_size_usd"] = 0.0

    # Remove tiny positions.
    tiny_mask = (
        (df["trade_candidate"] == 1)
        & (df["position_size_usd"] < df["min_notional_usd"])
    )

    df.loc[tiny_mask, "position_size_usd"] = 0.0

    df["position_size_pct"] = df["position_size_usd"] / PORTFOLIO_VALUE

    df["signed_position_usd"] = (
        df["trade_direction"] * df["position_size_usd"]
    )

    df["signed_position_pct"] = (
        df["trade_direction"] * df["position_size_pct"]
    )

    df["has_position"] = (df["position_size_usd"] > 0).astype(int)

    return df


def add_strategy_specific_sizes(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add three sizing views for later strategy comparison.

    Strategy A:
        Equal-size baseline.

    Strategy B:
        Signal + volatility adjusted sizing.

    Strategy C:
        Same as B for now, but reserved for later adaptive/Kelly sizing
        after backtest results exist.
    """
    df = df.copy()

    equal_size_usd = PORTFOLIO_VALUE * 0.01

    df["strategy_a_equal_size_usd"] = np.where(
        df["trade_candidate"] == 1,
        equal_size_usd,
        0.0,
    )

    df["strategy_a_signed_equal_size_usd"] = (
        df["trade_direction"] * df["strategy_a_equal_size_usd"]
    )

    df["strategy_b_signal_vol_size_usd"] = df["position_size_usd"]
    df["strategy_b_signed_signal_vol_size_usd"] = df["signed_position_usd"]

    # Placeholder for later Step 10/11 after we estimate edge from backtests.
    df["strategy_c_adaptive_size_usd"] = df["position_size_usd"]
    df["strategy_c_signed_adaptive_size_usd"] = df["signed_position_usd"]

    return df


def build_position_sizes(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()
    validate_input_columns(df)

    df["date"] = pd.to_datetime(df["date"])

    df = add_volatility_normalization(df)
    df = add_sizing_components(df)
    df = add_position_sizes(df)
    df = add_strategy_specific_sizes(df)

    front_cols = [
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
        "position_size_pct",
        "signed_position_usd",
        "signed_position_pct",
        "strategy_a_equal_size_usd",
        "strategy_b_signal_vol_size_usd",
        "strategy_c_adaptive_size_usd",
        "confirmation_score",
        "divergence_score",
        "combined_trade_score",
        "layers_triggered",
        "fx_volatility_norm",
        "default_holding_period_days",
    ]

    remaining_cols = [col for col in df.columns if col not in front_cols]
    df = df[front_cols + remaining_cols]

    return df.sort_values(["date", "commodity", "currency"]).reset_index(drop=True)


def main() -> None:
    if not INPUT_PATH.exists():
        raise FileNotFoundError(f"Missing input file: {INPUT_PATH}")

    df = pd.read_csv(INPUT_PATH)
    sized = build_position_sizes(df)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    sized.to_csv(OUTPUT_PATH, index=False)

    print(f"Saved sized trades to {OUTPUT_PATH}")
    print(f"Rows: {len(sized)}")
    print(f"Columns: {len(sized.columns)}")

    if not sized.empty:
        print("\nPosition summary:")
        print(sized["has_position"].value_counts(dropna=False))

        print("\nPrimary rule among sized positions:")
        print(
            sized[sized["has_position"] == 1]["primary_trade_rule"]
            .value_counts(dropna=False)
        )

        print("\nPosition size stats:")
        print(
            sized[sized["has_position"] == 1]["position_size_usd"]
            .describe()
        )

        print("\nTotal absolute notional by date, before portfolio risk controls:")
        daily_abs = (
            sized.groupby("date")["position_size_usd"]
            .sum()
            .sort_index()
        )
        print(daily_abs.tail(10))


if __name__ == "__main__":
    main()