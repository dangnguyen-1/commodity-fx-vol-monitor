from pathlib import Path

import numpy as np
import pandas as pd


INPUT_PATH = Path("strategy/output/daily_risk_approved_trades.csv")
OUTPUT_DIR = Path("strategy/output/backtest")

BACKTEST_START_DATE = "2010-01-01"
BACKTEST_END_DATE = None

INITIAL_CAPITAL = 100_000.0

# Round-trip transaction cost assumption.
# 2 bps means 0.02% of notional per completed trade.
ROUND_TRIP_COST_BPS = 2.0

ANNUALIZATION_DAYS = 252


def load_risk_approved_trades() -> pd.DataFrame:
    if not INPUT_PATH.exists():
        raise FileNotFoundError(f"Missing input file: {INPUT_PATH}")

    df = pd.read_csv(INPUT_PATH)
    df["date"] = pd.to_datetime(df["date"])

    if BACKTEST_START_DATE:
        df = df[df["date"] >= pd.to_datetime(BACKTEST_START_DATE)]

    if BACKTEST_END_DATE:
        df = df[df["date"] <= pd.to_datetime(BACKTEST_END_DATE)]

    return df.sort_values(["date", "relationship_id"]).reset_index(drop=True)


def choose_forward_return(row: pd.Series) -> float:
    hold_days = int(row["default_holding_period_days"])

    preferred_col = f"fx_forward_return_{hold_days}d"

    if preferred_col in row.index:
        return row[preferred_col]

    if hold_days <= 1:
        return row.get("fx_forward_return_1d", np.nan)

    if hold_days <= 3:
        return row.get("fx_forward_return_3d", np.nan)

    return row.get("fx_forward_return_5d", np.nan)


def attach_exit_dates(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["exit_date"] = pd.NaT

    for _, group in df.sort_values(["relationship_id", "date"]).groupby("relationship_id"):
        group = group.sort_values("date")
        dates = group["date"].to_numpy()
        indices = group.index.to_numpy()

        for position, index in enumerate(indices):
            hold_days = int(df.at[index, "default_holding_period_days"])
            exit_position = position + hold_days

            if exit_position < len(dates):
                df.at[index, "exit_date"] = dates[exit_position]

    return df


def build_trade_ledger(df: pd.DataFrame) -> pd.DataFrame:
    df = attach_exit_dates(df)

    trades = df[df["risk_approved"] == 1].copy()

    trades["holding_period_days"] = trades["default_holding_period_days"].astype(int)
    trades["realized_fx_return"] = trades.apply(choose_forward_return, axis=1)

    trades = trades.dropna(
        subset=[
            "exit_date",
            "realized_fx_return",
            "risk_adjusted_position_size_usd",
            "risk_adjusted_signed_position_usd",
        ]
    ).copy()

    trades["gross_pnl_usd"] = (
        trades["risk_adjusted_signed_position_usd"]
        * trades["realized_fx_return"]
    )

    trades["transaction_cost_usd"] = (
        trades["risk_adjusted_position_size_usd"]
        * ROUND_TRIP_COST_BPS
        / 10_000
    )

    trades["net_pnl_usd"] = trades["gross_pnl_usd"] - trades["transaction_cost_usd"]

    trades["trade_return_on_notional"] = (
        trades["net_pnl_usd"] / trades["risk_adjusted_position_size_usd"]
    )

    trades["winning_trade"] = (trades["net_pnl_usd"] > 0).astype(int)

    keep_cols = [
        "date",
        "exit_date",
        "relationship_id",
        "commodity",
        "currency",
        "fx_symbol",
        "primary_trade_rule",
        "trade_direction",
        "holding_period_days",
        "risk_adjusted_position_size_usd",
        "risk_adjusted_signed_position_usd",
        "realized_fx_return",
        "gross_pnl_usd",
        "transaction_cost_usd",
        "net_pnl_usd",
        "trade_return_on_notional",
        "winning_trade",
        "combined_trade_score",
        "confirmation_score",
        "divergence_score",
        "layers_triggered",
        "priority",
        "relationship_type",
    ]

    keep_cols = [col for col in keep_cols if col in trades.columns]

    return trades[keep_cols].sort_values(["date", "commodity", "currency"]).reset_index(drop=True)


def build_equity_curve(all_rows: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    calendar = pd.DataFrame({"date": sorted(all_rows["date"].unique())})

    daily_pnl = (
        trades.groupby("exit_date", as_index=False)
        .agg(
            gross_pnl_usd=("gross_pnl_usd", "sum"),
            transaction_cost_usd=("transaction_cost_usd", "sum"),
            net_pnl_usd=("net_pnl_usd", "sum"),
            trades_exited=("net_pnl_usd", "count"),
        )
        .rename(columns={"exit_date": "date"})
    )

    equity = calendar.merge(daily_pnl, on="date", how="left")

    fill_cols = [
        "gross_pnl_usd",
        "transaction_cost_usd",
        "net_pnl_usd",
        "trades_exited",
    ]

    for col in fill_cols:
        equity[col] = equity[col].fillna(0)

    equity["starting_equity_usd"] = INITIAL_CAPITAL + equity["net_pnl_usd"].cumsum().shift(1).fillna(0)
    equity["equity_usd"] = INITIAL_CAPITAL + equity["net_pnl_usd"].cumsum()

    equity["daily_return"] = np.where(
        equity["starting_equity_usd"] != 0,
        equity["net_pnl_usd"] / equity["starting_equity_usd"],
        0,
    )

    equity["running_max_equity_usd"] = equity["equity_usd"].cummax()
    equity["drawdown_usd"] = equity["equity_usd"] - equity["running_max_equity_usd"]
    equity["drawdown_pct"] = (
        equity["equity_usd"] / equity["running_max_equity_usd"] - 1
    )

    return equity


def safe_divide(numerator: float, denominator: float) -> float:
    if denominator == 0 or pd.isna(denominator):
        return np.nan

    return numerator / denominator


def calculate_summary(trades: pd.DataFrame, equity: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(
            [
                {
                    "initial_capital": INITIAL_CAPITAL,
                    "ending_equity": INITIAL_CAPITAL,
                    "total_return_pct": 0,
                    "annualized_return_pct": 0,
                    "annualized_volatility_pct": 0,
                    "sharpe_ratio": np.nan,
                    "max_drawdown_pct": 0,
                    "total_trades": 0,
                    "win_rate_pct": np.nan,
                }
            ]
        )

    start_date = equity["date"].min()
    end_date = equity["date"].max()
    years = max((end_date - start_date).days / 365.25, 1 / 365.25)

    ending_equity = equity["equity_usd"].iloc[-1]
    total_return = ending_equity / INITIAL_CAPITAL - 1

    annualized_return = (1 + total_return) ** (1 / years) - 1

    daily_returns = equity["daily_return"]
    annualized_volatility = daily_returns.std(ddof=1) * np.sqrt(ANNUALIZATION_DAYS)

    sharpe_ratio = safe_divide(
        daily_returns.mean() * ANNUALIZATION_DAYS,
        daily_returns.std(ddof=1) * np.sqrt(ANNUALIZATION_DAYS),
    )

    gross_profit = trades.loc[trades["net_pnl_usd"] > 0, "net_pnl_usd"].sum()
    gross_loss = -trades.loc[trades["net_pnl_usd"] < 0, "net_pnl_usd"].sum()

    summary = {
        "initial_capital": INITIAL_CAPITAL,
        "ending_equity": ending_equity,
        "total_pnl_usd": trades["net_pnl_usd"].sum(),
        "total_return_pct": total_return * 100,
        "annualized_return_pct": annualized_return * 100,
        "annualized_volatility_pct": annualized_volatility * 100,
        "sharpe_ratio": sharpe_ratio,
        "max_drawdown_pct": equity["drawdown_pct"].min() * 100,
        "max_drawdown_usd": equity["drawdown_usd"].min(),
        "total_trades": len(trades),
        "winning_trades": int(trades["winning_trade"].sum()),
        "losing_trades": int((trades["net_pnl_usd"] < 0).sum()),
        "win_rate_pct": trades["winning_trade"].mean() * 100,
        "avg_trade_pnl_usd": trades["net_pnl_usd"].mean(),
        "median_trade_pnl_usd": trades["net_pnl_usd"].median(),
        "avg_trade_return_on_notional_pct": trades["trade_return_on_notional"].mean() * 100,
        "gross_profit_usd": gross_profit,
        "gross_loss_usd": gross_loss,
        "profit_factor": safe_divide(gross_profit, gross_loss),
        "round_trip_cost_bps": ROUND_TRIP_COST_BPS,
        "backtest_start_date": start_date.date(),
        "backtest_end_date": end_date.date(),
    }

    return pd.DataFrame([summary])


def build_group_reports(trades: pd.DataFrame, equity: pd.DataFrame) -> dict[str, pd.DataFrame]:
    reports = {}

    trades = trades.copy()
    trades["entry_year"] = trades["date"].dt.year
    trades["exit_year"] = trades["exit_date"].dt.year

    reports["by_year"] = (
        trades.groupby("exit_year", as_index=False)
        .agg(
            trades=("net_pnl_usd", "count"),
            net_pnl_usd=("net_pnl_usd", "sum"),
            gross_pnl_usd=("gross_pnl_usd", "sum"),
            transaction_cost_usd=("transaction_cost_usd", "sum"),
            win_rate=("winning_trade", "mean"),
            avg_trade_pnl_usd=("net_pnl_usd", "mean"),
            avg_trade_return_on_notional=("trade_return_on_notional", "mean"),
        )
    )

    reports["by_year"]["win_rate_pct"] = reports["by_year"]["win_rate"] * 100
    reports["by_year"]["avg_trade_return_on_notional_pct"] = (
        reports["by_year"]["avg_trade_return_on_notional"] * 100
    )
    reports["by_year"] = reports["by_year"].drop(
        columns=["win_rate", "avg_trade_return_on_notional"]
    )

    for group_col in ["commodity", "currency", "fx_symbol", "primary_trade_rule"]:
        reports[f"by_{group_col}"] = (
            trades.groupby(group_col, as_index=False)
            .agg(
                trades=("net_pnl_usd", "count"),
                net_pnl_usd=("net_pnl_usd", "sum"),
                win_rate=("winning_trade", "mean"),
                avg_trade_pnl_usd=("net_pnl_usd", "mean"),
                avg_trade_return_on_notional=("trade_return_on_notional", "mean"),
            )
            .sort_values("net_pnl_usd", ascending=False)
        )

        reports[f"by_{group_col}"]["win_rate_pct"] = (
            reports[f"by_{group_col}"]["win_rate"] * 100
        )
        reports[f"by_{group_col}"]["avg_trade_return_on_notional_pct"] = (
            reports[f"by_{group_col}"]["avg_trade_return_on_notional"] * 100
        )
        reports[f"by_{group_col}"] = reports[f"by_{group_col}"].drop(
            columns=["win_rate", "avg_trade_return_on_notional"]
        )

    return reports


def save_outputs(
    trades: pd.DataFrame,
    equity: pd.DataFrame,
    summary: pd.DataFrame,
    reports: dict[str, pd.DataFrame],
) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    trades.to_csv(OUTPUT_DIR / "backtest_trades.csv", index=False)
    equity.to_csv(OUTPUT_DIR / "backtest_equity_curve.csv", index=False)
    summary.to_csv(OUTPUT_DIR / "backtest_summary.csv", index=False)

    for name, report in reports.items():
        report.to_csv(OUTPUT_DIR / f"backtest_{name}.csv", index=False)


def main() -> None:
    all_rows = load_risk_approved_trades()
    trades = build_trade_ledger(all_rows)
    equity = build_equity_curve(all_rows, trades)
    summary = calculate_summary(trades, equity)
    reports = build_group_reports(trades, equity)

    save_outputs(trades, equity, summary, reports)

    print(f"Saved backtest outputs to {OUTPUT_DIR}")
    print()
    print("Summary:")
    print(summary.T)

    print()
    print("Trades:", len(trades))
    print("Equity curve rows:", len(equity))

    if not trades.empty:
        print()
        print("Top commodities by PnL:")
        print(
            reports["by_commodity"]
            .head(10)[["commodity", "trades", "net_pnl_usd", "win_rate_pct"]]
            .to_string(index=False)
        )


if __name__ == "__main__":
    main()