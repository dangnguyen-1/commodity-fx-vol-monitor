from pathlib import Path

import numpy as np
import pandas as pd


INPUT_PATH = Path("strategy/output/daily_divergence.csv")
OUTPUT_PATH = Path("strategy/output/daily_trade_candidates.csv")


# Strategy A: simple baseline.
# Uses any directional signal with at least weak confirmation.
BASELINE_MIN_CONFIRMATION = 0.30

# Strategy B: main confirmed-signal strategy.
# Requires two-layer confirmation from Step 4.
CONFIRMED_MIN_CONFIRMATION = 0.40

# Strategy C: confirmed divergence strategy.
# Requires confirmation + FX underreaction from Step 5.
DIVERGENCE_MIN_CONFIRMATION = 0.40
DIVERGENCE_MIN_SCORE = 0.25


DEFAULT_HOLD_DAYS = {
    "baseline": 3,
    "confirmed": 3,
    "confirmed_divergence": 5,
}


REQUIRED_COLUMNS = [
    "date",
    "relationship_id",
    "commodity",
    "currency",
    "fx_symbol",
    "signal_direction",
    "confirmation_score",
    "is_confirmed_setup",
    "divergence_score",
    "is_divergence_opportunity",
    "is_confirmed_divergence_setup",
]


def validate_input_columns(df: pd.DataFrame) -> None:
    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]

    if missing:
        raise ValueError(
            "Missing required columns in daily_divergence.csv:\n"
            + "\n".join(f"- {col}" for col in missing)
        )


def add_entry_rules(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # Strategy A: baseline directional setup.
    # This captures weak one-layer signals and is useful as a benchmark.
    df["baseline_entry"] = (
        (df["signal_direction"] != 0)
        & (df["confirmation_score"] >= BASELINE_MIN_CONFIRMATION)
    ).astype(int)

    df["baseline_direction"] = np.where(
        df["baseline_entry"] == 1,
        df["signal_direction"],
        0,
    ).astype(int)

    # Strategy B: confirmed setup.
    # Requires multi-layer agreement from the confirmation score model.
    df["confirmed_entry"] = (
        (df["signal_direction"] != 0)
        & (df["is_confirmed_setup"] == 1)
        & (df["confirmation_score"] >= CONFIRMED_MIN_CONFIRMATION)
    ).astype(int)

    df["confirmed_direction"] = np.where(
        df["confirmed_entry"] == 1,
        df["signal_direction"],
        0,
    ).astype(int)

    # Strategy C: confirmed divergence setup.
    # Requires confirmation plus FX underreaction.
    df["confirmed_divergence_entry"] = (
        (df["signal_direction"] != 0)
        & (df["is_confirmed_divergence_setup"] == 1)
        & (df["is_divergence_opportunity"] == 1)
        & (df["confirmation_score"] >= DIVERGENCE_MIN_CONFIRMATION)
        & (df["divergence_score"] >= DIVERGENCE_MIN_SCORE)
    ).astype(int)

    df["confirmed_divergence_direction"] = np.where(
        df["confirmed_divergence_entry"] == 1,
        df["signal_direction"],
        0,
    ).astype(int)

    return df


def assign_primary_trade_rule(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    conditions = [
        df["confirmed_divergence_entry"] == 1,
        df["confirmed_entry"] == 1,
        df["baseline_entry"] == 1,
    ]

    choices = [
        "confirmed_divergence",
        "confirmed",
        "baseline",
    ]

    df["primary_trade_rule"] = np.select(
        conditions,
        choices,
        default="no_trade",
    )

    df["trade_candidate"] = (df["primary_trade_rule"] != "no_trade").astype(int)

    df["trade_direction"] = np.where(
        df["trade_candidate"] == 1,
        df["signal_direction"],
        0,
    ).astype(int)

    df["default_holding_period_days"] = df["primary_trade_rule"].map(
        {
            "baseline": DEFAULT_HOLD_DAYS["baseline"],
            "confirmed": DEFAULT_HOLD_DAYS["confirmed"],
            "confirmed_divergence": DEFAULT_HOLD_DAYS["confirmed_divergence"],
            "no_trade": 0,
        }
    ).astype(int)

    return df


def add_trade_strength(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # Baseline and confirmed trades rely mostly on confirmation.
    df["confirmation_component"] = df["confirmation_score"].clip(0.0, 1.0)

    # Divergence trades combine confirmation quality and underreaction timing.
    df["combined_trade_score"] = np.where(
        df["confirmed_divergence_entry"] == 1,
        0.60 * df["confirmation_score"] + 0.40 * df["divergence_score"],
        df["confirmation_score"],
    )

    df["combined_trade_score"] = df["combined_trade_score"].clip(0.0, 1.0)

    df["signed_trade_score"] = (
        df["trade_direction"] * df["combined_trade_score"]
    )

    return df


def add_rule_metadata(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # These are instructions for the later backtest engine.
    # The current file does not execute exits yet.
    df["exit_after_holding_period"] = 1
    df["exit_on_signal_flip"] = 1
    df["exit_on_divergence_close"] = np.where(
        df["primary_trade_rule"] == "confirmed_divergence",
        1,
        0,
    )

    df["needs_position_sizing"] = df["trade_candidate"]

    return df


def build_trade_rules(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()
    validate_input_columns(df)

    df["date"] = pd.to_datetime(df["date"])

    df = add_entry_rules(df)
    df = assign_primary_trade_rule(df)
    df = add_trade_strength(df)
    df = add_rule_metadata(df)

    front_cols = [
        "date",
        "relationship_id",
        "commodity",
        "currency",
        "fx_symbol",
        "primary_trade_rule",
        "trade_candidate",
        "trade_direction",
        "combined_trade_score",
        "signed_trade_score",
        "default_holding_period_days",
        "baseline_entry",
        "confirmed_entry",
        "confirmed_divergence_entry",
        "confirmation_score",
        "divergence_score",
    ]

    remaining_cols = [col for col in df.columns if col not in front_cols]
    df = df[front_cols + remaining_cols]

    return df.sort_values(["date", "commodity", "currency"]).reset_index(drop=True)


def main() -> None:
    if not INPUT_PATH.exists():
        raise FileNotFoundError(f"Missing input file: {INPUT_PATH}")

    df = pd.read_csv(INPUT_PATH)
    trade_rules = build_trade_rules(df)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    trade_rules.to_csv(OUTPUT_PATH, index=False)

    print(f"Saved trade candidates to {OUTPUT_PATH}")
    print(f"Rows: {len(trade_rules)}")
    print(f"Columns: {len(trade_rules.columns)}")

    if not trade_rules.empty:
        print("\nPrimary trade rule summary:")
        print(trade_rules["primary_trade_rule"].value_counts(dropna=False))

        print("\nTrade candidates:")
        print(trade_rules["trade_candidate"].value_counts(dropna=False))

        print("\nTrade direction:")
        print(trade_rules["trade_direction"].value_counts(dropna=False).sort_index())

        print("\nEntry rule counts:")
        print("baseline_entry:", int(trade_rules["baseline_entry"].sum()))
        print("confirmed_entry:", int(trade_rules["confirmed_entry"].sum()))
        print(
            "confirmed_divergence_entry:",
            int(trade_rules["confirmed_divergence_entry"].sum()),
        )


if __name__ == "__main__":
    main()