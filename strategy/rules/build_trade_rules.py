from pathlib import Path

import numpy as np
import pandas as pd


INPUT_PATH = Path("strategy/output/daily_divergence.csv")
OUTPUT_PATH = Path("strategy/output/daily_trade_candidates.csv")


# Strategy A is a pure price-horizon-consensus benchmark.
# Multiplying price strength by 0.30 preserves the current baseline score scale.
BASELINE_PRICE_MULTIPLIER = 0.30
BASELINE_MIN_SIGNAL_SCORE = 0.30

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
    "price_layer_score",
    "price_layer_direction",
]


def validate_input_columns(df: pd.DataFrame) -> None:
    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]

    if missing:
        raise ValueError(
            "Missing required columns in daily_divergence.csv:\n"
            + "\n".join(f"- {col}" for col in missing)
        )


def validate_input_rows(df: pd.DataFrame) -> None:
    duplicate_mask = df.duplicated(
        subset=["relationship_id", "date"],
        keep=False,
    )

    if duplicate_mask.any():
        bad_rows = df.loc[
            duplicate_mask,
            ["relationship_id", "date"],
        ]

        raise ValueError(
            "Duplicate relationship/date rows found:\n"
            f"{bad_rows.head(20).to_string(index=False)}"
        )

    weekend_mask = df["date"].dt.dayofweek >= 5

    if weekend_mask.any():
        bad_rows = df.loc[
            weekend_mask,
            ["relationship_id", "date"],
        ]

        raise ValueError(
            "Weekend rows found in daily_divergence.csv:\n"
            f"{bad_rows.head(20).to_string(index=False)}"
        )

    for column in [
        "signal_direction",
        "price_layer_direction",
    ]:
        invalid_mask = ~df[column].fillna(0).isin(
            [-1, 0, 1]
        )

        if invalid_mask.any():
            bad_rows = df.loc[
                invalid_mask,
                [
                    "relationship_id",
                    "date",
                    column,
                ],
            ]

            raise ValueError(
                f"Invalid values in {column}:\n"
                f"{bad_rows.head(20).to_string(index=False)}"
            )


def add_entry_rules(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # Strategy A: pure price-only benchmark.
    # It must remain independent of sentiment and fundamentals so that future
    # strategy versions can be compared against the same fixed baseline.
    df["baseline_signal_score"] = (
        df["price_layer_score"].abs() * BASELINE_PRICE_MULTIPLIER
    ).clip(0.0, 1.0)

    df["baseline_entry"] = (
        (df["price_layer_direction"] != 0)
        & (df["baseline_signal_score"] >= BASELINE_MIN_SIGNAL_SCORE)
    ).astype(int)

    df["baseline_direction"] = np.where(
        df["baseline_entry"] == 1,
        df["price_layer_direction"],
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

    df["trade_direction"] = np.select(
        [
            df["primary_trade_rule"] == "confirmed_divergence",
            df["primary_trade_rule"] == "confirmed",
            df["primary_trade_rule"] == "baseline",
        ],
        [
            df["confirmed_divergence_direction"],
            df["confirmed_direction"],
            df["baseline_direction"],
        ],
        default=0,
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


def add_trade_strength(
    df: pd.DataFrame,
) -> pd.DataFrame:
    df = df.copy()

    confirmation_score = (
        pd.to_numeric(
            df["confirmation_score"],
            errors="coerce",
        )
        .fillna(0.0)
        .clip(0.0, 1.0)
    )

    divergence_score = (
        pd.to_numeric(
            df["divergence_score"],
            errors="coerce",
        )
        .fillna(0.0)
        .clip(0.0, 1.0)
    )

    baseline_score = (
        pd.to_numeric(
            df["baseline_signal_score"],
            errors="coerce",
        )
        .fillna(0.0)
        .clip(0.0, 1.0)
    )

    confirmed_rule = (
        df["primary_trade_rule"]
        == "confirmed"
    )

    divergence_rule = (
        df["primary_trade_rule"]
        == "confirmed_divergence"
    )

    # Baseline trades must not carry sentiment/fundamental confirmation.
    df["confirmation_component"] = np.where(
        confirmed_rule | divergence_rule,
        confirmation_score,
        0.0,
    )

    # Only the divergence strategy uses divergence strength.
    df["divergence_component"] = np.where(
        divergence_rule,
        divergence_score,
        0.0,
    )

    divergence_combined_score = (
        0.60 * confirmation_score
        + 0.40 * divergence_score
    )

    # Each strategy is scored only with the information defining it.
    df["combined_trade_score"] = np.select(
        [
            divergence_rule,
            confirmed_rule,
            df["primary_trade_rule"].eq("baseline"),
        ],
        [
            divergence_combined_score,
            confirmation_score,
            baseline_score,
        ],
        default=0.0,
    )

    df["combined_trade_score"] = (
        pd.Series(
            df["combined_trade_score"],
            index=df.index,
        )
        .fillna(0.0)
        .clip(0.0, 1.0)
    )

    df["signed_trade_score"] = (
        df["trade_direction"]
        * df["combined_trade_score"]
    )

    return df


def add_rule_metadata(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # These are instructions for the later event-driven backtester.
    # They are active only for actual trade candidates.
    df["exit_after_holding_period"] = df["trade_candidate"]
    df["exit_on_signal_flip"] = df["trade_candidate"]

    df["exit_on_divergence_close"] = (
        df["primary_trade_rule"] == "confirmed_divergence"
    ).astype(int)

    df["needs_position_sizing"] = df["trade_candidate"]

    return df


def build_trade_rules(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()
    validate_input_columns(df)

    df["date"] = pd.to_datetime(df["date"])

    validate_input_rows(df)

    df = add_entry_rules(df)

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
        "baseline_signal_score",
        "combined_trade_score",
        "signed_trade_score",
        "confirmation_component",
        "divergence_component",
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