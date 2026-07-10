from pathlib import Path

import numpy as np
import pandas as pd


INPUT_PATH = Path("strategy/output/daily_features.csv")
OUTPUT_PATH = Path("strategy/output/daily_signals.csv")

# Minimum thresholds for each layer to count as active.
PRICE_LAYER_THRESHOLD = 0.30
SENTIMENT_LAYER_THRESHOLD = 0.05

# Layer multipliers mirror the later sizing idea:
# 1 agreeing layer = weaker conviction, 2 layers = stronger, 3 later when fundamentals are added.
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
    "net_sentiment_aligned",
    "total_news_count",
    "sentiment_confidence_weighted",
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
    Convert aligned sentiment into one signed sentiment layer.

    Positive value means sentiment points toward FX strength.
    Negative value means sentiment points toward FX weakness.

    Output range: [-1, 1].
    """
    if row.get("total_news_count", 0) <= 0:
        return 0.0

    raw_sentiment = row.get("net_sentiment_aligned", 0.0)

    if pd.isna(raw_sentiment):
        return 0.0

    # net_sentiment_aligned can exceed +/-1 because commodity and currency
    # sentiment are combined, so we clip to keep the layer bounded.
    return float(np.clip(raw_sentiment, -1.0, 1.0))


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


def add_confirmation_score(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["price_layer_score"] = df.apply(weighted_price_layer, axis=1)
    df["sentiment_layer_score"] = df.apply(sentiment_layer, axis=1)

    # Fundamental layer will be added later after we handle monthly trade data properly.
    df["fundamental_layer_score"] = 0.0

    df["price_layer_direction"] = df["price_layer_score"].apply(
        lambda x: get_layer_direction(x, PRICE_LAYER_THRESHOLD)
    )

    df["sentiment_layer_direction"] = df["sentiment_layer_score"].apply(
        lambda x: get_layer_direction(x, SENTIMENT_LAYER_THRESHOLD)
    )

    df["fundamental_layer_direction"] = 0

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

    # Directional agreement:
    # +1 means all active layers point bullish for FX.
    # -1 means all active layers point bearish for FX.
    # 0 means no signal or layers cancel out.
    direction_sum = df[layer_direction_cols].sum(axis=1)

    df["signal_direction"] = np.where(
        df["layers_triggered"] == 0,
        0,
        np.sign(direction_sum),
    ).astype(int)

    # Agreement ratio:
    # 1.0 means active layers fully agree.
    # 0.0 means active layers fully cancel.
    df["layer_agreement"] = np.where(
        df["layers_triggered"] > 0,
        np.abs(direction_sum) / df["layers_triggered"],
        0.0,
    )

    # Average strength of active layers only.
    active_strengths = []

    for _, row in df.iterrows():
        strengths = []

        for direction_col, score_col in zip(layer_direction_cols, layer_score_cols):
            if row[direction_col] != 0:
                strengths.append(abs(row[score_col]))

        if strengths:
            active_strengths.append(float(np.mean(strengths)))
        else:
            active_strengths.append(0.0)

    df["avg_active_layer_strength"] = active_strengths

    df["layer_multiplier"] = df["layers_triggered"].map(LAYER_MULTIPLIERS).fillna(0.0)

    df["confirmation_score"] = (
        df["avg_active_layer_strength"]
        * df["layer_agreement"]
        * df["layer_multiplier"]
    )

    df["confirmation_score"] = df["confirmation_score"].clip(0.0, 1.0)

    df["confirmation_bucket"] = df["confirmation_score"].apply(confirmation_bucket)

    # This is not the final trade signal yet.
    # It only means the setup has enough confirmation to be considered later.
    df["is_confirmed_setup"] = (
        (df["signal_direction"] != 0)
        & (df["layers_triggered"] >= 2)
        & (df["confirmation_score"] >= 0.40)
    ).astype(int)

    df["signed_confirmation_score"] = (
        df["signal_direction"] * df["confirmation_score"]
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