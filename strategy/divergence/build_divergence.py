from pathlib import Path

import numpy as np
import pandas as pd


INPUT_PATH = Path("strategy/output/daily_signals.csv")
OUTPUT_PATH = Path("strategy/output/daily_divergence.csv")

# A full divergence opportunity means FX has underreacted by roughly
# one unit of its own recent daily volatility.
FULL_DIVERGENCE_VOL_UNITS = 1.0

# A full expected response means the model expects an FX move of roughly
# one unit of recent daily FX volatility.
FULL_EXPECTED_RESPONSE_VOL_UNITS = 1.0

# Minimum score for saying there is a real divergence opportunity.
DIVERGENCE_OPPORTUNITY_THRESHOLD = 0.25


REQUIRED_COLUMNS = [
    "date",
    "relationship_id",
    "commodity",
    "currency",
    "fx_symbol",
    "signal_direction",
    "confirmation_score",
    "is_confirmed_setup",
    "expected_fx_return_1d",
    "fx_return_1d",
    "fx_volatility_20d",
]


def safe_divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    denominator = denominator.replace(0, np.nan)
    return numerator / denominator


def bounded_score(value: pd.Series, full_value: float) -> pd.Series:
    """
    Convert a positive raw value into a 0-1 score.

    0 means no opportunity.
    1 means full opportunity or stronger.
    """
    score = value / full_value
    return score.clip(lower=0.0, upper=1.0).fillna(0.0)


def divergence_bucket(score: float) -> str:
    if score >= 0.70:
        return "strong"
    if score >= 0.40:
        return "moderate"
    if score >= DIVERGENCE_OPPORTUNITY_THRESHOLD:
        return "weak"
    return "none"


def validate_input_columns(df: pd.DataFrame) -> None:
    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]

    if missing:
        raise ValueError(
            "Missing required columns in daily_signals.csv:\n"
            + "\n".join(f"- {col}" for col in missing)
        )


def add_expected_response_model(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["expected_fx_response_1d"] = df["expected_fx_return_1d"]
    df["actual_fx_response_1d"] = df["fx_return_1d"]

    df["fx_underreaction_1d"] = (
        df["expected_fx_response_1d"]
        - df["actual_fx_response_1d"]
    )

    # Express both expected and actual FX moves in the strategy signal's
    # direction. Positive means movement in the intended trade direction.
    df["directional_expected_fx_response_1d"] = (
        df["signal_direction"]
        * df["expected_fx_response_1d"]
    )

    df["directional_actual_fx_response_1d"] = (
        df["signal_direction"]
        * df["actual_fx_response_1d"]
    )

    # Positive means the expected move in the signal direction has not yet
    # been fully reflected in the actual FX move.
    df["directional_fx_underreaction_1d"] = (
        df["directional_expected_fx_response_1d"]
        - df["directional_actual_fx_response_1d"]
    )

    # A negative value means the rolling beta model points opposite to the
    # strategy signal, so it should not qualify as a divergence opportunity.
    df["expected_response_direction_agrees"] = (
        (df["signal_direction"] != 0)
        & (df["directional_expected_fx_response_1d"] > 0)
    ).astype(int)

    # Absolute expected response retained as a diagnostic.
    df["expected_fx_response_vol_scaled"] = safe_divide(
        df["expected_fx_response_1d"].abs(),
        df["fx_volatility_20d"],
    )

    # This is the expected response specifically in the trade direction.
    df["directional_expected_fx_response_vol_scaled"] = safe_divide(
        df["directional_expected_fx_response_1d"],
        df["fx_volatility_20d"],
    )

    df["fx_underreaction_vol_scaled"] = safe_divide(
        df["fx_underreaction_1d"],
        df["fx_volatility_20d"],
    )

    df["directional_fx_underreaction_vol_scaled"] = safe_divide(
        df["directional_fx_underreaction_1d"],
        df["fx_volatility_20d"],
    )

    return df


def add_divergence_score(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # Timing score:
    # high only when FX has not yet reacted enough in the signal direction.
    df["divergence_timing_score"] = bounded_score(
        df["directional_fx_underreaction_vol_scaled"],
        FULL_DIVERGENCE_VOL_UNITS,
    )

    # Expected response score:
    # high only when the expected FX response is large enough to care about.
    df["expected_response_score"] = bounded_score(
        df["directional_expected_fx_response_vol_scaled"],
        FULL_EXPECTED_RESPONSE_VOL_UNITS,
    )

    # Combined divergence score:
    # geometric mean rewards cases where both expected move and underreaction exist.
    df["divergence_score"] = np.sqrt(
        df["divergence_timing_score"] * df["expected_response_score"]
    ).fillna(0.0)

    df["divergence_score"] = df["divergence_score"].clip(0.0, 1.0)

    df["divergence_bucket"] = df["divergence_score"].apply(divergence_bucket)

    # Divergence direction is only active when there is underreaction opportunity.
    df["divergence_direction"] = np.where(
        df["divergence_score"] >= DIVERGENCE_OPPORTUNITY_THRESHOLD,
        df["signal_direction"],
        0,
    ).astype(int)

    df["is_divergence_opportunity"] = (
        (df["signal_direction"] != 0)
        & (df["expected_response_direction_agrees"] == 1)
        & (df["divergence_score"] >= DIVERGENCE_OPPORTUNITY_THRESHOLD)
        & (df["directional_fx_underreaction_1d"] > 0)
    ).astype(int)

    # This is still not a final trade rule.
    # It means: confirmed signal + FX underreaction opportunity.
    df["is_confirmed_divergence_setup"] = (
        (df["is_confirmed_setup"] == 1)
        & (df["is_divergence_opportunity"] == 1)
    ).astype(int)

    df["signed_divergence_score"] = (
        df["divergence_direction"] * df["divergence_score"]
    )

    return df


def build_divergence(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()
    validate_input_columns(df)

    df["date"] = pd.to_datetime(df["date"])

    df = add_expected_response_model(df)
    df = add_divergence_score(df)

    front_cols = [
        "date",
        "relationship_id",
        "commodity",
        "currency",
        "fx_symbol",
        "signal_direction",
        "confirmation_score",
        "is_confirmed_setup",
        "expected_fx_response_1d",
        "actual_fx_response_1d",
        "directional_expected_fx_response_1d",
        "directional_actual_fx_response_1d",
        "expected_response_direction_agrees",
        "fx_underreaction_1d",
        "directional_fx_underreaction_1d",
        "divergence_score",
        "signed_divergence_score",
        "divergence_bucket",
        "divergence_direction",
        "is_divergence_opportunity",
        "is_confirmed_divergence_setup",
    ]

    remaining_cols = [col for col in df.columns if col not in front_cols]
    df = df[front_cols + remaining_cols]

    return df.sort_values(["date", "commodity", "currency"]).reset_index(drop=True)


def main() -> None:
    if not INPUT_PATH.exists():
        raise FileNotFoundError(f"Missing input file: {INPUT_PATH}")

    df = pd.read_csv(INPUT_PATH)
    divergence = build_divergence(df)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    divergence.to_csv(OUTPUT_PATH, index=False)

    print(f"Saved divergence model to {OUTPUT_PATH}")
    print(f"Rows: {len(divergence)}")
    print(f"Columns: {len(divergence.columns)}")

    if not divergence.empty:
        print("\nDivergence bucket summary:")
        print(divergence["divergence_bucket"].value_counts(dropna=False))

        print("\nDivergence opportunities:")
        print(divergence["is_divergence_opportunity"].value_counts(dropna=False))

        print("\nConfirmed divergence setups:")
        print(divergence["is_confirmed_divergence_setup"].value_counts(dropna=False))

        print("\nDivergence direction:")
        print(divergence["divergence_direction"].value_counts(dropna=False).sort_index())


if __name__ == "__main__":
    main()