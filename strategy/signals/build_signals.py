from pathlib import Path

import numpy as np
import pandas as pd


INPUT_PATH = Path("strategy/output/daily_features.csv")
OUTPUT_PATH = Path("strategy/output/daily_signals.csv")

# Minimum thresholds for each layer to count as active.
PRICE_LAYER_THRESHOLD = 0.30
SENTIMENT_LAYER_THRESHOLD = 0.05
FUNDAMENTAL_LAYER_THRESHOLD = 0.10

# More agreeing active layers produce greater conviction.
LAYER_MULTIPLIERS = {
    0: 0.0,
    1: 0.3,
    2: 0.8,
    3: 1.0,
}


REQUIRED_COLUMNS = [
    "date",
    "relationship_id",
    "commodity",
    "currency",
    "fx_symbol",
    "priority",
    "commodity_return_1d_aligned",
    "commodity_return_3d_aligned",
    "commodity_return_5d_aligned",
    "sentiment_signal",
    "total_news_count",
    "sentiment_confidence_weighted",
    "fundamental_signal",
    "has_active_fundamental",
]


def safe_sign(value: float) -> int:
    if pd.isna(value) or value == 0:
        return 0
    return int(np.sign(value))


def weighted_price_layer(row: pd.Series) -> float:
    """
    Convert aligned commodity returns into one signed price layer.

    Positive value means commodity price action points toward FX strength.
    Negative value means commodity price action points toward FX weakness.

    Output range: [-1, 1].
    """
    weights = {
        "commodity_return_1d_aligned": 0.50,
        "commodity_return_3d_aligned": 0.30,
        "commodity_return_5d_aligned": 0.20,
    }

    numerator = 0.0
    denominator = 0.0

    for col, weight in weights.items():
        value = row.get(col)

        if pd.notna(value):
            numerator += weight * safe_sign(value)
            denominator += weight

    if denominator == 0:
        return 0.0

    return numerator / denominator


def sentiment_layer(row: pd.Series) -> float:
    """
    Convert relationship-level sentiment into a signed layer.

    Positive values support FX strength in the mapped relationship.
    Negative values support FX weakness.

    Confidence weighting affects signal strength but not direction.
    Output range: [-1, 1].
    """
    news_count = row.get("total_news_count", 0)

    if pd.isna(news_count) or news_count <= 0:
        return 0.0

    weighted_sentiment = row.get(
        "sentiment_confidence_weighted",
        np.nan,
    )

    raw_sentiment = row.get(
        "sentiment_signal",
        np.nan,
    )

    # Fall back to the raw relationship-level score if confidence
    # is unavailable.
    value = (
        weighted_sentiment
        if pd.notna(weighted_sentiment)
        else raw_sentiment
    )

    if pd.isna(value):
        return 0.0

    return float(np.clip(value, -1.0, 1.0))


def fundamental_layer(row: pd.Series) -> float:
    """
    Convert the usable relationship-level fundamental signal into a layer.

    fundamental_signal is already directionally aligned:
    positive values support FX strength and negative values support weakness.

    Output range: [-1, 1].
    """
    active = row.get(
        "has_active_fundamental",
        0,
    )

    if pd.isna(active) or active != 1:
        return 0.0

    value = row.get(
        "fundamental_signal",
        np.nan,
    )

    if pd.isna(value):
        return 0.0

    return float(np.clip(value, -1.0, 1.0))


def get_layer_direction(layer_value: float, threshold: float) -> int:
    if pd.isna(layer_value):
        return 0

    if abs(layer_value) < threshold:
        return 0

    return safe_sign(layer_value)


def confirmation_bucket(score: float) -> str:
    if score >= 0.70:
        return "strong"
    if score >= 0.40:
        return "moderate"
    if score > 0:
        return "weak"
    return "none"


def add_confirmation_score(
    df: pd.DataFrame,
) -> pd.DataFrame:
    df = df.copy()

    df["price_layer_score"] = df.apply(
        weighted_price_layer,
        axis=1,
    )

    df["sentiment_layer_score"] = df.apply(
        sentiment_layer,
        axis=1,
    )

    df["fundamental_layer_score"] = df.apply(
        fundamental_layer,
        axis=1,
    )

    df["price_layer_direction"] = (
        df["price_layer_score"].apply(
            lambda value: get_layer_direction(
                value,
                PRICE_LAYER_THRESHOLD,
            )
        )
    )

    df["sentiment_layer_direction"] = (
        df["sentiment_layer_score"].apply(
            lambda value: get_layer_direction(
                value,
                SENTIMENT_LAYER_THRESHOLD,
            )
        )
    )

    df["fundamental_layer_direction"] = (
        df["fundamental_layer_score"].apply(
            lambda value: get_layer_direction(
                value,
                FUNDAMENTAL_LAYER_THRESHOLD,
            )
        )
    )

    layer_direction_cols = [
        "price_layer_direction",
        "sentiment_layer_direction",
        "fundamental_layer_direction",
    ]

    layer_score_cols = [
        "price_layer_score",
        "sentiment_layer_score",
        "fundamental_layer_score",
    ]

    df["layers_triggered"] = (
        df[layer_direction_cols]
        .ne(0)
        .sum(axis=1)
    )

    direction_sum = (
        df[layer_direction_cols]
        .sum(axis=1)
    )

    # +1 means the net active-layer direction is bullish for FX.
    # -1 means it is bearish.
    # 0 means there is no active signal or the active layers cancel.
    df["signal_direction"] = np.where(
        df["layers_triggered"].gt(0),
        np.sign(direction_sum),
        0,
    ).astype(int)

    triggered_denominator = (
        df["layers_triggered"]
        .replace(0, np.nan)
    )

    df["layer_agreement"] = (
        direction_sum.abs()
        / triggered_denominator
    ).fillna(0.0).clip(0.0, 1.0)

    # Calculate average absolute strength using only layers that passed
    # their activation thresholds.
    active_mask = (
        df[layer_direction_cols]
        .ne(0)
        .to_numpy()
    )

    absolute_scores = (
        df[layer_score_cols]
        .abs()
        .fillna(0.0)
        .to_numpy(dtype=float)
    )

    active_strength_sum = np.where(
        active_mask,
        absolute_scores,
        0.0,
    ).sum(axis=1)

    active_layer_count = active_mask.sum(axis=1)

    df["avg_active_layer_strength"] = np.divide(
        active_strength_sum,
        active_layer_count,
        out=np.zeros(
            len(df),
            dtype=float,
        ),
        where=active_layer_count > 0,
    )

    df["layer_multiplier"] = (
        df["layers_triggered"]
        .map(LAYER_MULTIPLIERS)
        .fillna(0.0)
    )

    df["confirmation_score"] = (
        df["avg_active_layer_strength"]
        * df["layer_agreement"]
        * df["layer_multiplier"]
    ).clip(0.0, 1.0)

    df["confirmation_bucket"] = (
        df["confirmation_score"]
        .apply(confirmation_bucket)
    )

    # A confirmed setup requires at least two active layers. Conflicting
    # layers reduce layer_agreement and therefore confirmation_score.
    df["is_confirmed_setup"] = (
        df["signal_direction"].ne(0)
        & df["layers_triggered"].ge(2)
        & df["confirmation_score"].ge(0.40)
    ).astype(int)

    df["signed_confirmation_score"] = (
        df["signal_direction"]
        * df["confirmation_score"]
    )

    return df


def validate_input_columns(df: pd.DataFrame) -> None:
    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]

    if missing:
        raise ValueError(
            "Missing required columns in daily_features.csv:\n"
            + "\n".join(f"- {col}" for col in missing)
        )


def build_signals(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()
    validate_input_columns(df)

    df["date"] = pd.to_datetime(df["date"])
    
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

    df = add_confirmation_score(df)

    front_cols = [
        "date",
        "relationship_id",
        "commodity",
        "currency",
        "fx_symbol",
        "priority",
        "signal_direction",
        "confirmation_score",
        "signed_confirmation_score",
        "confirmation_bucket",
        "layers_triggered",
        "layer_agreement",
        "is_confirmed_setup",
    ]

    remaining_cols = [col for col in df.columns if col not in front_cols]
    df = df[front_cols + remaining_cols]

    return df.sort_values(["date", "commodity", "currency"]).reset_index(drop=True)


def main() -> None:
    if not INPUT_PATH.exists():
        raise FileNotFoundError(f"Missing input file: {INPUT_PATH}")

    df = pd.read_csv(INPUT_PATH)
    signals = build_signals(df)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    signals.to_csv(OUTPUT_PATH, index=False)

    print(f"Saved signals to {OUTPUT_PATH}")
    print(f"Rows: {len(signals)}")
    print(f"Columns: {len(signals.columns)}")

    if not signals.empty:
        print("\nSignal summary:")
        print(signals["confirmation_bucket"].value_counts(dropna=False))

        print("\nConfirmed setups:")
        print(signals["is_confirmed_setup"].value_counts(dropna=False))

        print("\nLayers triggered:")
        print(signals["layers_triggered"].value_counts(dropna=False).sort_index())

        print("\nSignal direction:")
        print(signals["signal_direction"].value_counts(dropna=False).sort_index())


if __name__ == "__main__":
    main()