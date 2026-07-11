from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


CANDIDATE_INPUT_PATH = Path("strategy/output/daily_sized_trades.csv")
FX_PRICE_INPUT_PATH = Path("strategy/output/daily_fx_prices.csv")
OUTPUT_DIR = Path("strategy/output/backtest")

BACKTEST_START_DATE = "2010-01-01"
BACKTEST_END_DATE = None

INITIAL_CAPITAL = 100_000.0
ROUND_TRIP_COST_BPS = 2.0
ANNUALIZATION_DAYS = 252

# Final open-portfolio risk limits. These are enforced against positions that
# are already open, not only against same-day candidates.
MAX_PORTFOLIO_GROSS_EXPOSURE_PCT = 0.10
MAX_CURRENCY_GROSS_EXPOSURE_PCT = 0.04
MAX_DAILY_NEW_GROSS_EXPOSURE_PCT = 0.10

MAX_OPEN_POSITIONS = 5
MAX_OPEN_POSITIONS_PER_CURRENCY = 2
MAX_OPEN_POSITIONS_PER_COMMODITY = 1
MAX_OPEN_POSITIONS_PER_FX_SYMBOL = 1

MIN_POSITION_PCT = 0.001

RULE_PRIORITY = {
    "confirmed_divergence": 3,
    "confirmed": 2,
    "baseline": 1,
    "no_trade": 0,
}

REQUIRED_CANDIDATE_COLUMNS = [
    "date",
    "relationship_id",
    "commodity",
    "currency",
    "fx_symbol",
    "primary_trade_rule",
    "trade_candidate",
    "trade_direction",
    "has_position",
    "position_size_pct",
    "combined_trade_score",
    "confirmation_score",
    "divergence_score",
    "default_holding_period_days",
]

REQUIRED_FX_PRICE_COLUMNS = [
    "date",
    "fx_symbol",
    "fx_open",
    "fx_high",
    "fx_low",
    "fx_close",
]


def safe_divide(numerator: float, denominator: float) -> float:
    if denominator == 0 or pd.isna(denominator):
        return np.nan
    return numerator / denominator


def validate_columns(
    df: pd.DataFrame,
    required_columns: list[str],
    file_label: str,
) -> None:
    missing = [col for col in required_columns if col not in df.columns]
    if missing:
        raise ValueError(
            f"Missing required columns in {file_label}:\n"
            + "\n".join(f"- {col}" for col in missing)
        )


def load_candidates() -> pd.DataFrame:
    if not CANDIDATE_INPUT_PATH.exists():
        raise FileNotFoundError(f"Missing input file: {CANDIDATE_INPUT_PATH}")

    df = pd.read_csv(CANDIDATE_INPUT_PATH)
    validate_columns(df, REQUIRED_CANDIDATE_COLUMNS, CANDIDATE_INPUT_PATH.name)

    df["date"] = pd.to_datetime(df["date"])

    if BACKTEST_START_DATE:
        df = df[df["date"] >= pd.to_datetime(BACKTEST_START_DATE)]

    if BACKTEST_END_DATE:
        df = df[df["date"] <= pd.to_datetime(BACKTEST_END_DATE)]

    numeric_cols = [
        "trade_candidate",
        "trade_direction",
        "has_position",
        "position_size_pct",
        "combined_trade_score",
        "confirmation_score",
        "divergence_score",
        "default_holding_period_days",
    ]

    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["risk_rule_priority"] = (
        df["primary_trade_rule"]
        .map(RULE_PRIORITY)
        .fillna(0)
        .astype(int)
    )

    return df.sort_values(["date", "relationship_id"]).reset_index(drop=True)


def load_fx_prices() -> pd.DataFrame:
    if not FX_PRICE_INPUT_PATH.exists():
        raise FileNotFoundError(f"Missing input file: {FX_PRICE_INPUT_PATH}")

    df = pd.read_csv(FX_PRICE_INPUT_PATH)
    validate_columns(df, REQUIRED_FX_PRICE_COLUMNS, FX_PRICE_INPUT_PATH.name)

    df["date"] = pd.to_datetime(df["date"])

    if BACKTEST_START_DATE:
        df = df[df["date"] >= pd.to_datetime(BACKTEST_START_DATE)]

    if BACKTEST_END_DATE:
        df = df[df["date"] <= pd.to_datetime(BACKTEST_END_DATE)]

    price_cols = ["fx_open", "fx_high", "fx_low", "fx_close"]
    for col in price_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    duplicate_mask = df.duplicated(["date", "fx_symbol"], keep=False)
    if duplicate_mask.any():
        duplicate_rows = df.loc[duplicate_mask, ["date", "fx_symbol"]]
        raise ValueError(
            "Duplicate date/fx_symbol rows found in daily_fx_prices.csv:\n"
            f"{duplicate_rows.head(20).to_string(index=False)}"
        )

    invalid_price_mask = (
        df[price_cols].isna().any(axis=1)
        | (df[price_cols] <= 0).any(axis=1)
    )
    if invalid_price_mask.any():
        bad_rows = df.loc[
            invalid_price_mask,
            ["date", "fx_symbol", *price_cols],
        ]
        raise ValueError(
            "Missing or non-positive FX execution prices found:\n"
            f"{bad_rows.head(20).to_string(index=False)}"
        )

    return df.sort_values(["date", "fx_symbol"]).reset_index(drop=True)


def attach_execution_schedule(
    candidates: pd.DataFrame,
    fx_prices: pd.DataFrame,
) -> pd.DataFrame:
    """
    Signals are formed after the close on signal_date.

    Entry occurs at the next available FX open. A holding period of N days
    exits at the FX open N trading observations after entry.
    """
    candidates = candidates.copy()

    candidates["signal_date"] = candidates["date"]
    candidates["entry_date"] = pd.NaT
    candidates["scheduled_exit_date"] = pd.NaT
    candidates["scheduled_entry_price"] = np.nan
    candidates["scheduled_exit_price"] = np.nan
    candidates["schedule_status"] = "not_a_trade"

    price_groups: dict[str, pd.DataFrame] = {
        symbol: group.sort_values("date").reset_index(drop=True)
        for symbol, group in fx_prices.groupby("fx_symbol", sort=False)
    }

    candidate_mask = (
        (candidates["trade_candidate"] == 1)
        & (candidates["has_position"] == 1)
        & (candidates["trade_direction"].isin([-1, 1]))
        & (candidates["position_size_pct"] > 0)
        & (candidates["default_holding_period_days"] >= 1)
    )

    for idx, row in candidates.loc[candidate_mask].iterrows():
        symbol = row["fx_symbol"]
        price_group = price_groups.get(symbol)

        if price_group is None or price_group.empty:
            candidates.at[idx, "schedule_status"] = "missing_fx_symbol"
            continue

        dates = price_group["date"].to_numpy(dtype="datetime64[ns]")
        signal_date = np.datetime64(row["signal_date"], "ns")

        entry_position = int(np.searchsorted(dates, signal_date, side="right"))
        if entry_position >= len(price_group):
            candidates.at[idx, "schedule_status"] = "missing_next_open"
            continue

        hold_days = int(row["default_holding_period_days"])
        exit_position = entry_position + hold_days

        if exit_position >= len(price_group):
            candidates.at[idx, "schedule_status"] = "missing_exit_open"
            continue

        entry_row = price_group.iloc[entry_position]
        exit_row = price_group.iloc[exit_position]

        candidates.at[idx, "entry_date"] = entry_row["date"]
        candidates.at[idx, "scheduled_exit_date"] = exit_row["date"]
        candidates.at[idx, "scheduled_entry_price"] = float(entry_row["fx_open"])
        candidates.at[idx, "scheduled_exit_price"] = float(exit_row["fx_open"])
        candidates.at[idx, "schedule_status"] = "scheduled"

    return candidates


def mark_position(position: dict[str, Any], price: float) -> float:
    return (
        position["trade_direction"]
        * position["notional_usd"]
        * (price / position["entry_price"] - 1.0)
    )


def summarize_open_exposure(
    open_positions: list[dict[str, Any]],
) -> dict[str, Any]:
    currency_gross: dict[str, float] = {}
    currency_count: dict[str, int] = {}
    commodity_count: dict[str, int] = {}
    fx_symbol_count: dict[str, int] = {}

    total_gross = 0.0

    for position in open_positions:
        notional = float(position["notional_usd"])
        currency = position["currency"]
        commodity = position["commodity"]
        fx_symbol = position["fx_symbol"]

        total_gross += notional
        currency_gross[currency] = currency_gross.get(currency, 0.0) + notional
        currency_count[currency] = currency_count.get(currency, 0) + 1
        commodity_count[commodity] = commodity_count.get(commodity, 0) + 1
        fx_symbol_count[fx_symbol] = fx_symbol_count.get(fx_symbol, 0) + 1

    return {
        "total_gross": total_gross,
        "currency_gross": currency_gross,
        "currency_count": currency_count,
        "commodity_count": commodity_count,
        "fx_symbol_count": fx_symbol_count,
    }


def run_event_backtest(
    candidates: pd.DataFrame,
    fx_prices: pd.DataFrame,
    *,
    initial_capital: float = INITIAL_CAPITAL,
    round_trip_cost_bps: float = ROUND_TRIP_COST_BPS,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    candidates = attach_execution_schedule(candidates, fx_prices)

    price_lookup = (
        fx_prices.set_index(["date", "fx_symbol"])[["fx_open", "fx_close"]]
        .to_dict("index")
    )

    calendar = sorted(fx_prices["date"].unique())

    scheduled_candidates = candidates[candidates["schedule_status"] == "scheduled"].copy()
    candidates_by_entry_date = {
        date: group.copy()
        for date, group in scheduled_candidates.groupby("entry_date", sort=False)
    }

    one_way_cost_bps = round_trip_cost_bps / 2.0

    cash_equity = float(initial_capital)
    previous_close_equity = float(initial_capital)

    open_positions: list[dict[str, Any]] = []
    closed_trades: list[dict[str, Any]] = []
    entry_decisions: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []

    # Preserve scheduling failures in the decision ledger instead of silently
    # dropping candidates near the end of the sample or with missing FX data.
    unscheduled_mask = (
        (candidates["trade_candidate"] == 1)
        & (candidates["has_position"] == 1)
        & (candidates["schedule_status"] != "scheduled")
    )

    for _, candidate in candidates.loc[unscheduled_mask].iterrows():
        entry_decisions.append(
            {
                "signal_date": candidate["signal_date"],
                "entry_date": pd.NaT,
                "scheduled_exit_date": pd.NaT,
                "relationship_id": candidate["relationship_id"],
                "commodity": candidate["commodity"],
                "currency": candidate["currency"],
                "fx_symbol": candidate["fx_symbol"],
                "primary_trade_rule": candidate["primary_trade_rule"],
                "trade_direction": int(candidate["trade_direction"]),
                "combined_trade_score": candidate["combined_trade_score"],
                "position_size_pct": candidate["position_size_pct"],
                "portfolio_equity_before_entry_usd": np.nan,
                "open_gross_before_entry_usd": np.nan,
                "requested_position_size_usd": 0.0,
                "approved_position_size_usd": 0.0,
                "entry_decision": "rejected",
                "entry_rejection_reason": candidate["schedule_status"],
                "entry_was_scaled": 0,
            }
        )

    next_position_id = 1

    for current_date in calendar:
        current_date = pd.Timestamp(current_date)

        # Mark existing positions to today's open when a bar is available.
        for position in open_positions:
            price_row = price_lookup.get((current_date, position["fx_symbol"]))
            if price_row is not None:
                position["last_mark_price"] = float(price_row["fx_open"])

        # Fixed-horizon exits occur at today's open before new entries.
        remaining_positions: list[dict[str, Any]] = []
        trades_exited_today = 0
        gross_pnl_exited_today = 0.0
        exit_costs_today = 0.0

        for position in open_positions:
            if position["scheduled_exit_date"] != current_date:
                remaining_positions.append(position)
                continue

            exit_price = float(position["last_mark_price"])
            gross_pnl = mark_position(position, exit_price)
            exit_cost = position["notional_usd"] * one_way_cost_bps / 10_000
            net_pnl = gross_pnl - position["entry_cost_usd"] - exit_cost

            cash_equity += gross_pnl - exit_cost

            closed_trade = dict(position)
            closed_trade.update(
                {
                    "exit_date": current_date,
                    "exit_price": exit_price,
                    "gross_pnl_usd": gross_pnl,
                    "exit_cost_usd": exit_cost,
                    "transaction_cost_usd": position["entry_cost_usd"] + exit_cost,
                    "net_pnl_usd": net_pnl,
                    "realized_fx_return": exit_price / position["entry_price"] - 1.0,
                    "trade_return_on_notional": net_pnl / position["notional_usd"],
                    "winning_trade": int(net_pnl > 0),
                    "actual_holding_calendar_days": (
                        current_date - position["entry_date"]
                    ).days,
                }
            )
            closed_trades.append(closed_trade)

            trades_exited_today += 1
            gross_pnl_exited_today += gross_pnl
            exit_costs_today += exit_cost

        open_positions = remaining_positions

        unrealized_at_open = sum(
            mark_position(position, position["last_mark_price"])
            for position in open_positions
        )
        equity_at_open = cash_equity + unrealized_at_open

        day_candidates = candidates_by_entry_date.get(current_date)
        daily_new_gross = 0.0
        trades_opened_today = 0
        entry_costs_today = 0.0
        rejected_today = 0

        if day_candidates is not None and not day_candidates.empty:
            day_candidates = day_candidates.sort_values(
                [
                    "risk_rule_priority",
                    "combined_trade_score",
                    "position_size_pct",
                    "relationship_id",
                ],
                ascending=[False, False, False, True],
                kind="mergesort",
            )

            for _, candidate in day_candidates.iterrows():
                exposure = summarize_open_exposure(open_positions)

                current_unrealized = sum(
                    mark_position(position, position["last_mark_price"])
                    for position in open_positions
                )
                current_equity = cash_equity + current_unrealized

                decision: dict[str, Any] = {
                    "signal_date": candidate["signal_date"],
                    "entry_date": current_date,
                    "scheduled_exit_date": candidate["scheduled_exit_date"],
                    "relationship_id": candidate["relationship_id"],
                    "commodity": candidate["commodity"],
                    "currency": candidate["currency"],
                    "fx_symbol": candidate["fx_symbol"],
                    "primary_trade_rule": candidate["primary_trade_rule"],
                    "trade_direction": int(candidate["trade_direction"]),
                    "combined_trade_score": candidate["combined_trade_score"],
                    "position_size_pct": candidate["position_size_pct"],
                    "portfolio_equity_before_entry_usd": current_equity,
                    "open_gross_before_entry_usd": exposure["total_gross"],
                    "requested_position_size_usd": 0.0,
                    "approved_position_size_usd": 0.0,
                    "entry_decision": "rejected",
                    "entry_rejection_reason": "unknown",
                    "entry_was_scaled": 0,
                }

                if current_equity <= 0:
                    decision["entry_rejection_reason"] = "non_positive_equity"
                    entry_decisions.append(decision)
                    rejected_today += 1
                    continue

                requested_size = float(candidate["position_size_pct"]) * current_equity
                decision["requested_position_size_usd"] = requested_size

                min_position_usd = MIN_POSITION_PCT * current_equity
                max_portfolio_gross_usd = (
                    MAX_PORTFOLIO_GROSS_EXPOSURE_PCT * current_equity
                )
                max_currency_gross_usd = (
                    MAX_CURRENCY_GROSS_EXPOSURE_PCT * current_equity
                )
                max_daily_new_gross_usd = (
                    MAX_DAILY_NEW_GROSS_EXPOSURE_PCT * current_equity
                )

                currency = candidate["currency"]
                commodity = candidate["commodity"]
                fx_symbol = candidate["fx_symbol"]
                relationship_id = candidate["relationship_id"]

                if len(open_positions) >= MAX_OPEN_POSITIONS:
                    decision["entry_rejection_reason"] = "max_open_positions"
                elif any(
                    position["relationship_id"] == relationship_id
                    for position in open_positions
                ):
                    decision["entry_rejection_reason"] = "relationship_already_open"
                elif exposure["currency_count"].get(currency, 0) >= MAX_OPEN_POSITIONS_PER_CURRENCY:
                    decision["entry_rejection_reason"] = "max_open_positions_per_currency"
                elif exposure["commodity_count"].get(commodity, 0) >= MAX_OPEN_POSITIONS_PER_COMMODITY:
                    decision["entry_rejection_reason"] = "max_open_positions_per_commodity"
                elif exposure["fx_symbol_count"].get(fx_symbol, 0) >= MAX_OPEN_POSITIONS_PER_FX_SYMBOL:
                    decision["entry_rejection_reason"] = "max_open_positions_per_fx_symbol"
                else:
                    remaining_portfolio_capacity = (
                        max_portfolio_gross_usd - exposure["total_gross"]
                    )
                    remaining_currency_capacity = (
                        max_currency_gross_usd
                        - exposure["currency_gross"].get(currency, 0.0)
                    )
                    remaining_daily_capacity = (
                        max_daily_new_gross_usd - daily_new_gross
                    )

                    allowed_size = min(
                        requested_size,
                        remaining_portfolio_capacity,
                        remaining_currency_capacity,
                        remaining_daily_capacity,
                    )

                    if allowed_size < min_position_usd:
                        decision["entry_rejection_reason"] = "insufficient_risk_capacity"
                    else:
                        entry_price = float(candidate["scheduled_entry_price"])
                        entry_cost = allowed_size * one_way_cost_bps / 10_000

                        position = {
                            "position_id": next_position_id,
                            "signal_date": candidate["signal_date"],
                            "entry_date": current_date,
                            "scheduled_exit_date": candidate["scheduled_exit_date"],
                            "relationship_id": relationship_id,
                            "commodity": commodity,
                            "currency": currency,
                            "fx_symbol": fx_symbol,
                            "primary_trade_rule": candidate["primary_trade_rule"],
                            "trade_direction": int(candidate["trade_direction"]),
                            "holding_period_days": int(candidate["default_holding_period_days"]),
                            "notional_usd": allowed_size,
                            "position_size_pct_at_entry": allowed_size / current_equity,
                            "entry_price": entry_price,
                            "last_mark_price": entry_price,
                            "entry_cost_usd": entry_cost,
                            "combined_trade_score": candidate["combined_trade_score"],
                            "confirmation_score": candidate["confirmation_score"],
                            "divergence_score": candidate["divergence_score"],
                            "layers_triggered": candidate.get("layers_triggered", np.nan),
                            "priority": candidate.get("priority", np.nan),
                            "relationship_type": candidate.get("relationship_type", np.nan),
                        }

                        open_positions.append(position)
                        next_position_id += 1

                        cash_equity -= entry_cost
                        daily_new_gross += allowed_size
                        trades_opened_today += 1
                        entry_costs_today += entry_cost

                        was_scaled = int(allowed_size < requested_size - 1e-12)
                        decision["approved_position_size_usd"] = allowed_size
                        decision["entry_decision"] = "approved"
                        decision["entry_rejection_reason"] = (
                            "approved_scaled" if was_scaled else "approved"
                        )
                        decision["entry_was_scaled"] = was_scaled

                if decision["entry_decision"] != "approved":
                    rejected_today += 1

                entry_decisions.append(decision)

        # Mark all open positions to today's close.
        for position in open_positions:
            price_row = price_lookup.get((current_date, position["fx_symbol"]))
            if price_row is not None:
                position["last_mark_price"] = float(price_row["fx_close"])

        unrealized_at_close = sum(
            mark_position(position, position["last_mark_price"])
            for position in open_positions
        )
        close_equity = cash_equity + unrealized_at_close

        daily_return = (
            close_equity / previous_close_equity - 1.0
            if previous_close_equity != 0
            else 0.0
        )

        close_exposure = summarize_open_exposure(open_positions)

        equity_rows.append(
            {
                "date": current_date,
                "starting_equity_usd": previous_close_equity,
                "cash_equity_usd": cash_equity,
                "unrealized_pnl_usd": unrealized_at_close,
                "equity_usd": close_equity,
                "daily_return": daily_return,
                "open_positions": len(open_positions),
                "open_gross_exposure_usd": close_exposure["total_gross"],
                "open_gross_exposure_pct": safe_divide(
                    close_exposure["total_gross"],
                    close_equity,
                ),
                "new_gross_exposure_usd": daily_new_gross,
                "trades_opened": trades_opened_today,
                "trades_exited": trades_exited_today,
                "candidates_rejected": rejected_today,
                "gross_pnl_exited_usd": gross_pnl_exited_today,
                "entry_costs_usd": entry_costs_today,
                "exit_costs_usd": exit_costs_today,
                "transaction_costs_usd": entry_costs_today + exit_costs_today,
            }
        )

        previous_close_equity = close_equity

    if open_positions:
        raise RuntimeError(
            "Backtest ended with open positions despite requiring a valid "
            "scheduled exit for every approved entry."
        )

    trades = pd.DataFrame(closed_trades)
    decisions = pd.DataFrame(entry_decisions)
    equity = pd.DataFrame(equity_rows)

    if not equity.empty:
        equity["running_max_equity_usd"] = equity["equity_usd"].cummax()
        equity["drawdown_usd"] = (
            equity["equity_usd"] - equity["running_max_equity_usd"]
        )
        equity["drawdown_pct"] = (
            equity["equity_usd"] / equity["running_max_equity_usd"] - 1.0
        )

    return trades, equity, decisions


def calculate_summary(
    trades: pd.DataFrame,
    equity: pd.DataFrame,
    decisions: pd.DataFrame,
    *,
    initial_capital: float = INITIAL_CAPITAL,
    round_trip_cost_bps: float = ROUND_TRIP_COST_BPS,
) -> pd.DataFrame:
    if equity.empty:
        raise ValueError("Equity curve is empty.")

    start_date = equity["date"].min()
    end_date = equity["date"].max()
    years = max((end_date - start_date).days / 365.25, 1 / 365.25)

    ending_equity = float(equity["equity_usd"].iloc[-1])
    total_return = ending_equity / initial_capital - 1.0
    annualized_return = (1.0 + total_return) ** (1.0 / years) - 1.0

    daily_returns = equity["daily_return"]
    daily_std = daily_returns.std(ddof=1)
    annualized_volatility = daily_std * np.sqrt(ANNUALIZATION_DAYS)
    sharpe_ratio = safe_divide(
        daily_returns.mean() * ANNUALIZATION_DAYS,
        daily_std * np.sqrt(ANNUALIZATION_DAYS),
    )

    if trades.empty:
        gross_profit = 0.0
        gross_loss = 0.0
        winning_trades = 0
        losing_trades = 0
        win_rate = np.nan
        avg_trade_pnl = np.nan
        median_trade_pnl = np.nan
        avg_trade_return = np.nan
        total_costs = 0.0
    else:
        gross_profit = trades.loc[trades["net_pnl_usd"] > 0, "net_pnl_usd"].sum()
        gross_loss = -trades.loc[trades["net_pnl_usd"] < 0, "net_pnl_usd"].sum()
        winning_trades = int((trades["net_pnl_usd"] > 0).sum())
        losing_trades = int((trades["net_pnl_usd"] < 0).sum())
        win_rate = winning_trades / len(trades) * 100
        avg_trade_pnl = trades["net_pnl_usd"].mean()
        median_trade_pnl = trades["net_pnl_usd"].median()
        avg_trade_return = trades["trade_return_on_notional"].mean() * 100
        total_costs = trades["transaction_cost_usd"].sum()

    approved_entries = (
        int((decisions["entry_decision"] == "approved").sum())
        if not decisions.empty
        else 0
    )

    summary = {
        "initial_capital": initial_capital,
        "ending_equity": ending_equity,
        "total_pnl_usd": ending_equity - initial_capital,
        "total_return_pct": total_return * 100,
        "annualized_return_pct": annualized_return * 100,
        "annualized_volatility_pct": annualized_volatility * 100,
        "sharpe_ratio": sharpe_ratio,
        "max_drawdown_pct": equity["drawdown_pct"].min() * 100,
        "max_drawdown_usd": equity["drawdown_usd"].min(),
        "total_trades": len(trades),
        "approved_entries": approved_entries,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "win_rate_pct": win_rate,
        "avg_trade_pnl_usd": avg_trade_pnl,
        "median_trade_pnl_usd": median_trade_pnl,
        "avg_trade_return_on_notional_pct": avg_trade_return,
        "gross_profit_usd": gross_profit,
        "gross_loss_usd": gross_loss,
        "profit_factor": safe_divide(gross_profit, gross_loss),
        "total_transaction_cost_usd": total_costs,
        "round_trip_cost_bps": round_trip_cost_bps,
        "backtest_start_date": start_date.date(),
        "backtest_end_date": end_date.date(),
    }

    return pd.DataFrame([summary])


def build_group_reports(trades: pd.DataFrame) -> dict[str, pd.DataFrame]:
    reports: dict[str, pd.DataFrame] = {}

    if trades.empty:
        return reports

    trades = trades.copy()
    trades["entry_year"] = trades["entry_date"].dt.year
    trades["exit_year"] = trades["exit_date"].dt.year

    group_specs = {
        "by_year": "exit_year",
        "by_commodity": "commodity",
        "by_currency": "currency",
        "by_fx_symbol": "fx_symbol",
        "by_primary_trade_rule": "primary_trade_rule",
    }

    for report_name, group_col in group_specs.items():
        report = (
            trades.groupby(group_col, as_index=False)
            .agg(
                trades=("net_pnl_usd", "count"),
                net_pnl_usd=("net_pnl_usd", "sum"),
                gross_pnl_usd=("gross_pnl_usd", "sum"),
                transaction_cost_usd=("transaction_cost_usd", "sum"),
                win_rate=("winning_trade", "mean"),
                avg_trade_pnl_usd=("net_pnl_usd", "mean"),
                avg_trade_return_on_notional=("trade_return_on_notional", "mean"),
            )
            .sort_values("net_pnl_usd", ascending=False)
        )

        report["win_rate_pct"] = report["win_rate"] * 100
        report["avg_trade_return_on_notional_pct"] = (
            report["avg_trade_return_on_notional"] * 100
        )
        report = report.drop(columns=["win_rate", "avg_trade_return_on_notional"])
        reports[report_name] = report

    return reports


def save_outputs(
    trades: pd.DataFrame,
    equity: pd.DataFrame,
    decisions: pd.DataFrame,
    summary: pd.DataFrame,
    reports: dict[str, pd.DataFrame],
) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    trades.to_csv(OUTPUT_DIR / "backtest_trades.csv", index=False)
    equity.to_csv(OUTPUT_DIR / "backtest_equity_curve.csv", index=False)
    decisions.to_csv(OUTPUT_DIR / "backtest_entry_decisions.csv", index=False)
    summary.to_csv(OUTPUT_DIR / "backtest_summary.csv", index=False)

    for name, report in reports.items():
        report.to_csv(OUTPUT_DIR / f"backtest_{name}.csv", index=False)


def main() -> None:
    candidates = load_candidates()
    fx_prices = load_fx_prices()

    trades, equity, decisions = run_event_backtest(candidates, fx_prices)
    summary = calculate_summary(trades, equity, decisions)
    reports = build_group_reports(trades)

    save_outputs(trades, equity, decisions, summary, reports)

    print(f"Saved event-driven backtest outputs to {OUTPUT_DIR}")
    print("\nSummary:")
    print(summary.T)

    print("\nClosed trades:", len(trades))
    print(
        "Approved entries:",
        int((decisions["entry_decision"] == "approved").sum())
        if not decisions.empty
        else 0,
    )
    print("Equity curve rows:", len(equity))

    if not decisions.empty:
        print("\nEntry decision summary:")
        print(decisions["entry_rejection_reason"].value_counts(dropna=False))

    if "by_commodity" in reports:
        print("\nTop commodities by PnL:")
        print(
            reports["by_commodity"]
            .head(10)[["commodity", "trades", "net_pnl_usd", "win_rate_pct"]]
            .to_string(index=False)
        )


if __name__ == "__main__":
    main()