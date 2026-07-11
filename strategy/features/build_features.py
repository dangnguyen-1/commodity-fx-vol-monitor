from pathlib import Path

import numpy as np
import pandas as pd


INPUT_PATH = Path("strategy/output/daily_research_panel.csv")
OUTPUT_PATH = Path("strategy/output/daily_features.csv")

ROLLING_WINDOW = 20
MIN_PERIODS = 5
RETURN_WINDOWS = [1, 3, 5]


def rolling_zscore(series: pd.Series, window: int = ROLLING_WINDOW) -> pd.Series:
    """
    Score today's observation against information available through yesterday.

    This prevents the current observation from influencing the mean and
    standard deviation used to normalize itself.
    """
    historical = series.shift(1)

    rolling_mean = historical.rolling(
        window=window,
        min_periods=MIN_PERIODS,
    ).mean()

    rolling_std = historical.rolling(
        window=window,
        min_periods=MIN_PERIODS,
    ).std()

    return (series - rolling_mean) / rolling_std.replace(0, np.nan)


def add_relationship_id(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["relationship_id"] = (
        df["commodity"]
        + "__"
        + df["currency"]
        + "__"
        + df["fx_symbol"]
    )

    return df


def add_aligned_returns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for window in RETURN_WINDOWS:
        df[f"commodity_return_{window}d_aligned"] = (
            df["expected_sign"] * df[f"commodity_return_{window}d"]
        )

    return df


def add_sentiment_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["commodity_sentiment_aligned"] = (
        df["expected_sign"] * df["commodity_sentiment_score"]
    )

    # Most FX symbols are expressed as currency/USD.
    # Positive currency sentiment should usually mean the FX symbol rises.
    df["currency_sentiment_fx_sign"] = 1

    # Special case:
    # USD inverse mappings are represented using EURUSD.
    # Positive USD sentiment should imply EURUSD falls.
    usd_inverse_mask = (
        (df["currency"] == "USD")
        & (df["fx_symbol"] == "FX:EURUSD")
    )

    df.loc[usd_inverse_mask, "currency_sentiment_fx_sign"] = -1

    df["currency_sentiment_aligned"] = (
        df["currency_sentiment_fx_sign"] * df["currency_sentiment_score"]
    )

    df["net_sentiment_aligned"] = (
        df["commodity_sentiment_aligned"]
        + df["currency_sentiment_aligned"]
    )

    df["has_commodity_news"] = (df["commodity_news_count"] > 0).astype(int)
    df["has_currency_news"] = (df["currency_news_count"] > 0).astype(int)

    df["has_any_news"] = (
        (df["commodity_news_count"] > 0)
        | (df["currency_news_count"] > 0)
    ).astype(int)

    return df


def add_rolling_beta_and_divergence(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    parts = []

    for relationship_id, group in df.groupby("relationship_id", sort=False):
        group = group.sort_values("date").copy()

        x = group["commodity_return_1d_aligned"]
        y = group["fx_return_1d"]

        # Estimate the relationship using information through the previous day.
        historical_x = x.shift(1)
        historical_y = y.shift(1)

        rolling_cov = historical_x.rolling(
            window=ROLLING_WINDOW,
            min_periods=MIN_PERIODS,
        ).cov(historical_y)

        rolling_var = historical_x.rolling(
            window=ROLLING_WINDOW,
            min_periods=MIN_PERIODS,
        ).var()

        group["rolling_beta_20d"] = rolling_cov / rolling_var.replace(0, np.nan)

        group["expected_fx_return_1d"] = (
            group["rolling_beta_20d"] * group["commodity_return_1d_aligned"]
        )

        group["fx_divergence_1d"] = (
            group["expected_fx_return_1d"] - group["fx_return_1d"]
        )

        group["fx_divergence_z_20d"] = rolling_zscore(group["fx_divergence_1d"])

        parts.append(group)

    return pd.concat(parts, ignore_index=True) if parts else df


def add_momentum_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    parts = []

    for relationship_id, group in df.groupby("relationship_id", sort=False):
        group = group.sort_values("date").copy()

        group["commodity_momentum_z_20d"] = rolling_zscore(
            group["commodity_return_5d_aligned"]
        )

        group["fx_momentum_z_20d"] = rolling_zscore(
            group["fx_return_5d"]
        )

        group["sentiment_z_20d"] = rolling_zscore(
            group["net_sentiment_aligned"]
        )

        parts.append(group)

    return pd.concat(parts, ignore_index=True) if parts else df


def add_simple_direction_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["commodity_direction_1d"] = np.sign(df["commodity_return_1d_aligned"])
    df["commodity_direction_3d"] = np.sign(df["commodity_return_3d_aligned"])
    df["commodity_direction_5d"] = np.sign(df["commodity_return_5d_aligned"])

    df["sentiment_direction"] = np.sign(df["net_sentiment_aligned"])
    df["fx_direction_1d"] = np.sign(df["fx_return_1d"])

    df["price_sentiment_agree"] = (
        (df["commodity_direction_1d"] == df["sentiment_direction"])
        & (df["sentiment_direction"] != 0)
    ).astype(int)

    return df


def add_forward_targets(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    parts = []

    for relationship_id, group in df.groupby("relationship_id", sort=False):
        group = group.sort_values("date").copy()

        for window in RETURN_WINDOWS:
            group[f"fx_forward_return_{window}d"] = (
                group["fx_close"].shift(-window) / group["fx_close"] - 1
            )

            group[f"fx_forward_direction_{window}d"] = np.sign(
                group[f"fx_forward_return_{window}d"]
            )

        parts.append(group)

    return pd.concat(parts, ignore_index=True) if parts else df


def add_volatility_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    parts = []

    for relationship_id, group in df.groupby("relationship_id", sort=False):
        group = group.sort_values("date").copy()

        historical_commodity_returns = (
            group["commodity_return_1d_aligned"].shift(1)
        )

        historical_fx_returns = group["fx_return_1d"].shift(1)

        group["commodity_volatility_20d"] = (
            historical_commodity_returns
            .rolling(window=ROLLING_WINDOW, min_periods=MIN_PERIODS)
            .std()
        )

        group["fx_volatility_20d"] = (
            historical_fx_returns
            .rolling(window=ROLLING_WINDOW, min_periods=MIN_PERIODS)
            .std()
        )

        group["fx_divergence_vol_scaled"] = (
            group["fx_divergence_1d"]
            / group["fx_volatility_20d"].replace(0, np.nan)
        )

        parts.append(group)

    return pd.concat(parts, ignore_index=True) if parts else df


def add_news_intensity_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["total_news_count"] = (
        df["commodity_news_count"] + df["currency_news_count"]
    )

    df["log_total_news_count"] = np.log1p(df["total_news_count"])

    df["avg_total_sentiment_confidence"] = (
        df["commodity_sentiment_confidence"]
        + df["currency_sentiment_confidence"]
    ) / 2

    df["sentiment_confidence_weighted"] = (
        df["net_sentiment_aligned"] * df["avg_total_sentiment_confidence"]
    )

    return df


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])

    df = add_relationship_id(df)
    df = df.sort_values(["relationship_id", "date"]).reset_index(drop=True)
    
    duplicate_mask = df.duplicated(
        subset=["relationship_id", "date"],
        keep=False,
    )

    if duplicate_mask.any():
        duplicate_rows = df.loc[
            duplicate_mask,
            ["relationship_id", "date"],
        ]

        raise ValueError(
            "Duplicate relationship/date rows found:\n"
            f"{duplicate_rows.head(20).to_string(index=False)}"
        )

    df = add_aligned_returns(df)
    df = add_sentiment_features(df)
    df = add_rolling_beta_and_divergence(df)
    df = add_momentum_features(df)
    df = add_simple_direction_features(df)
    df = add_forward_targets(df)
    df = add_volatility_features(df)
    df = add_news_intensity_features(df)

    front_cols = [
        "date",
        "relationship_id",
        "commodity",
        "commodity_symbol",
        "currency",
        "fx_symbol",
        "expected_sign",
        "relationship_type",
        "priority",
    ]

    remaining_cols = [col for col in df.columns if col not in front_cols]
    df = df[front_cols + remaining_cols]

    return df.sort_values(["date", "commodity", "currency"]).reset_index(drop=True)


def main() -> None:
    if not INPUT_PATH.exists():
        raise FileNotFoundError(f"Missing input file: {INPUT_PATH}")

    df = pd.read_csv(INPUT_PATH)
    features = build_features(df)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    features.to_csv(OUTPUT_PATH, index=False)

    print(f"Saved features to {OUTPUT_PATH}")
    print(f"Rows: {len(features)}")
    print(f"Columns: {len(features.columns)}")

    if not features.empty:
        print("\nFeature columns added:")

        added_cols = [
            "relationship_id",
            "commodity_return_1d_aligned",
            "commodity_return_3d_aligned",
            "commodity_return_5d_aligned",
            "commodity_sentiment_aligned",
            "currency_sentiment_aligned",
            "net_sentiment_aligned",
            "rolling_beta_20d",
            "expected_fx_return_1d",
            "fx_divergence_1d",
            "fx_divergence_z_20d",
            "commodity_momentum_z_20d",
            "fx_momentum_z_20d",
            "sentiment_z_20d",
            "commodity_direction_1d",
            "sentiment_direction",
            "price_sentiment_agree",
            "fx_forward_return_1d",
            "fx_forward_return_3d",
            "fx_forward_return_5d",
            "commodity_volatility_20d",
            "fx_volatility_20d",
            "fx_divergence_vol_scaled",
            "total_news_count",
            "log_total_news_count",
            "avg_total_sentiment_confidence",
            "sentiment_confidence_weighted",
        ]

        for col in added_cols:
            if col in features.columns:
                non_null = features[col].notna().sum()
                print(f"- {col}: {non_null} non-null")


if __name__ == "__main__":
    main()