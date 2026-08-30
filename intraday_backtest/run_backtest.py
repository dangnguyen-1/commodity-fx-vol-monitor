"""
Offline backtest of the intraday candidate's entry/exit formulas against
real multi-year 1-minute history from Dukascopy — the only free source
deep enough to backtest a 5-minute-cadence strategy (the live
TradingView feed only retains ~1-2 weeks of 1-minute bars).

Runs TWO formula variants side by side on identical data so the effect
of each change is visible, not just asserted:

  v0.1.0 (baseline) — the literal confirmed spec from the engineering
  handoff: a fixed +1/-1 relationship_direction, every return horizon
  normalized by the same 60-minute realized volatility, no correlation
  check, no liquidity awareness.

  v0.2.0 (candidate) — four changes, none touching production yet:
    1. Beta/R²-weighted divergence: expected_fx_impulse uses a rolling
       beta (60 trading days of daily log returns) instead of a static
       direction sign — beta is signed, so it replaces what direction
       was crudely doing, but continuously and data-driven.
    2. Volatility-normalization fix: each horizon's return is divided
       by realized_vol_60m scaled by sqrt(horizon/60) (square-root-of-
       time), not the raw 60-minute figure for all three horizons.
    3. Correlation-holding gate: no entries are evaluated for a
       relationship on a day where its rolling |correlation| is below
       0.15 (R² below ~0.0225) — a divergence against a currently-weak
       relationship is noise, not signal.
    4. Session/liquidity awareness: no entries during hours where
       either leg's average volume (measured empirically from its own
       history, not assumed market hours) is below 25% of its peak
       hour's average.

Scope limitations (unchanged from the original plan, still apply to
both variants):
  - Only 11 of the 30 live relationships are covered — Dukascopy has no
    Brazilian Real pair at all (drops Coffee/Soybeans/Sugar) and
    doesn't offer several commodities outright (Aluminum, Cattle, Coal,
    Corn, Wheat, Gasoline, Heating Oil, Iron Ore, LNG, Lithium/Lithium
    Hydroxide, Lumber, Nickel, Zinc).
  - No historical news data exists for this period, so only the
    "Divergence" entry mode (no news required) is testable. The
    "Confirmed" mode's real hit rate is unknown until forward paper
    trading runs with the live news pipeline.
  - Position sizing uses the spec's floor/cap (0.50-2.00) on raw
    |divergence| only — the live system's additional annual
    relationship weight (1.0/0.5) comes from a 2-year rolling daily
    backtest this script has no equivalent for.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

ROUND_TRIP_COST = 0.0002 + 2 * 0.00005  # 2bps + 0.5bps slippage/side

COMMODITY_WEIGHTS = (0.50, 0.30, 0.20)  # 15m, 60m, 240m
DIVERGENCE_ENTRY_COMMODITY_THRESHOLD = 1.50
DIVERGENCE_ENTRY_DIVERGENCE_THRESHOLD = 1.00
REVERSAL_STRENGTH_THRESHOLD = 0.75
CONVERGENCE_THRESHOLD = 0.25
MAX_HOLDING_MINUTES = 240
VOLATILITY_STOP_UNITS = 1.50
SIGNAL_FLOOR, SIGNAL_CAP = 0.50, 2.00
EVAL_FREQ_MINUTES = 5

# --- v0.2.0 candidate-only parameters ---
BETA_R2_WINDOW_DAYS = 60
MIN_ABS_CORRELATION = 0.15  # ~R^2 0.0225 — below this, relationship is "not holding"
LIQUIDITY_MIN_FRACTION_OF_PEAK = 0.25


@dataclass
class Relationship:
    relationship_id: str
    commodity_instrument: str
    fx_instrument: str
    fx_invert: bool
    fx_direction_multiplier: int


RELATIONSHIPS = [
    Relationship("Brent Oil__CAD__DERIVED:CADUSD", "brentcmdusd", "usdcad", True, 1),
    Relationship("Crude Oil__CAD__DERIVED:CADUSD", "lightcmdusd", "usdcad", True, 1),
    Relationship("Natural Gas__CAD__DERIVED:CADUSD", "gascmdusd", "usdcad", True, 1),
    Relationship("Gold__AUD__FX:AUDUSD", "xauusd", "audusd", False, 1),
    Relationship("Gold__USD__FX:EURUSD", "xauusd", "eurusd", False, 1),
    Relationship("Copper__AUD__FX:AUDUSD", "coppercmdusd", "audusd", False, 1),
    Relationship("Cocoa__GBP__FX:GBPUSD", "cocoacmdusd", "gbpusd", False, 1),
    Relationship("Cotton__USD__FX:EURUSD", "cottoncmdusx", "eurusd", False, 1),
    Relationship("Platinum__USD__FX:EURUSD", "xptcmdusd", "eurusd", False, 1),
    Relationship("Palladium__USD__FX:EURUSD", "xpdcmdusd", "eurusd", False, 1),
    Relationship("Silver__USD__FX:EURUSD", "xagusd", "eurusd", False, 1),
]


def load_instrument(instrument: str) -> pd.DataFrame:
    """Loads all downloaded Dukascopy m1 CSVs for one instrument into a
    single OHLCV frame indexed by UTC minute timestamp."""
    matches = sorted(glob.glob(os.path.join(DATA_DIR, f"{instrument}-m1-*.csv")))
    if not matches:
        raise FileNotFoundError(f"No downloaded data for {instrument}")
    frames = [pd.read_csv(f) for f in matches]
    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset="timestamp")
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.tz_localize(None)
    df = df.sort_values("timestamp").set_index("timestamp")
    return df


def realized_vol_60m(log_returns_1m: pd.Series) -> pd.Series:
    return np.sqrt((log_returns_1m ** 2).rolling(60).sum())


def build_features(price: pd.Series, fix_vol_scaling: bool) -> pd.DataFrame:
    log_price = np.log(price)
    ret_1m = log_price.diff()
    vol60 = realized_vol_60m(ret_1m).clip(lower=1e-6)

    out = pd.DataFrame(index=price.index)
    for horizon in (15, 60, 240):
        raw_ret = log_price.diff(horizon)
        # v0.2.0 fix: scale the 60-minute vol measure to each horizon's
        # own timescale (square-root-of-time) instead of dividing every
        # horizon by the same, unscaled 60-minute figure.
        denom = vol60 * np.sqrt(horizon / 60.0) if fix_vol_scaling else vol60
        out[f"norm_ret_{horizon}m"] = raw_ret / denom.clip(lower=1e-6)
    out["vol60"] = vol60
    out["price"] = price
    return out


def resample_to_eval_grid(features: pd.DataFrame) -> pd.DataFrame:
    on_grid = features.index.minute % EVAL_FREQ_MINUTES == 0
    return features.loc[on_grid]


def rolling_beta_r2_corr(commodity_price: pd.Series, fx_price: pd.Series) -> pd.DataFrame:
    """Daily-resampled rolling beta/R^2/correlation between the FX and
    commodity legs, over a trailing BETA_R2_WINDOW_DAYS window — a
    slow-moving statistic, deliberately not recomputed every 5 minutes.
    Indexed by calendar date; callers look up the most recent available
    date's value for each intraday timestamp."""
    daily_commodity = commodity_price.resample("1D").last().dropna()
    daily_fx = fx_price.resample("1D").last().dropna()
    combined = pd.DataFrame({"commodity": daily_commodity, "fx": daily_fx}).dropna()
    log_rets = np.log(combined / combined.shift(1)).dropna()

    roll_corr = log_rets["fx"].rolling(BETA_R2_WINDOW_DAYS).corr(log_rets["commodity"])
    roll_std_fx = log_rets["fx"].rolling(BETA_R2_WINDOW_DAYS).std()
    roll_std_commodity = log_rets["commodity"].rolling(BETA_R2_WINDOW_DAYS).std()
    roll_beta = roll_corr * roll_std_fx / roll_std_commodity.replace(0, np.nan)
    roll_r2 = roll_corr ** 2

    return pd.DataFrame({"correlation": roll_corr, "beta": roll_beta, "r2": roll_r2}).dropna()


def hourly_liquidity_profile(volume_1m: pd.Series) -> pd.Series:
    """Average 1-minute volume by UTC hour-of-day, measured from the
    instrument's own history (not an assumed session calendar).
    Returns a Series indexed 0-23."""
    return volume_1m.groupby(volume_1m.index.hour).mean()


def is_liquid_hour(hour: int, profile: pd.Series) -> bool:
    if profile.empty or profile.max() <= 0:
        return True  # no volume data available -> don't filter
    return profile.get(hour, 0.0) >= LIQUIDITY_MIN_FRACTION_OF_PEAK * profile.max()


def run_relationship_backtest(rel: Relationship, formula: str) -> dict:
    """formula: 'v1' (baseline, per the confirmed spec) or 'v2' (candidate)."""
    is_v2 = formula == "v2"

    commodity_df = load_instrument(rel.commodity_instrument)
    fx_df_raw = load_instrument(rel.fx_instrument)

    commodity_price = commodity_df["close"]
    fx_price = (1.0 / fx_df_raw["close"]) if rel.fx_invert else fx_df_raw["close"]

    commodity_feat = build_features(commodity_price, fix_vol_scaling=is_v2)
    fx_feat = build_features(fx_price, fix_vol_scaling=is_v2)

    commodity_eval = resample_to_eval_grid(commodity_feat)
    fx_eval = resample_to_eval_grid(fx_feat)

    joined = commodity_eval.join(fx_eval, how="inner", lsuffix="_cmd", rsuffix="_fx")
    joined = joined.dropna(
        subset=["norm_ret_15m_cmd", "norm_ret_60m_cmd", "norm_ret_240m_cmd", "norm_ret_15m_fx"]
    )
    if joined.empty:
        return {"relationship_id": rel.relationship_id, "formula": formula, "trades": [], "error": "no overlapping data"}

    w15, w60, w240 = COMMODITY_WEIGHTS
    commodity_impulse = (
        w15 * joined["norm_ret_15m_cmd"]
        + w60 * joined["norm_ret_60m_cmd"]
        + w240 * joined["norm_ret_240m_cmd"]
    )
    observed_fx_impulse = joined["norm_ret_15m_fx"]

    if is_v2:
        stats = rolling_beta_r2_corr(commodity_price, fx_price)
        # Shift by one day before the as-of join: a day's rolling stat
        # is only "complete" once that day's close is in, so using it
        # for that same day's intraday evaluations would be lookahead.
        # Every intraday timestamp uses the most recent *prior* day's
        # completed beta/R^2/correlation.
        stats_asof = stats.shift(1).reindex(joined.index.normalize(), method="ffill")
        stats_asof.index = joined.index
        beta = stats_asof["beta"].fillna(rel.fx_direction_multiplier)
        r2 = stats_asof["r2"].fillna(0.0)
        expected_fx_impulse = beta * commodity_impulse

        commodity_liquidity = hourly_liquidity_profile(commodity_df["volume"])
        fx_liquidity = hourly_liquidity_profile(fx_df_raw["volume"])
        liquid_mask = joined.index.hour.map(
            lambda h: is_liquid_hour(h, commodity_liquidity) and is_liquid_hour(h, fx_liquidity)
        )
        relationship_holding_mask = (r2.values >= (MIN_ABS_CORRELATION ** 2))
    else:
        expected_fx_impulse = rel.fx_direction_multiplier * commodity_impulse
        liquid_mask = np.ones(len(joined), dtype=bool)
        relationship_holding_mask = np.ones(len(joined), dtype=bool)

    divergence = expected_fx_impulse - observed_fx_impulse
    tradeable_mask = np.asarray(liquid_mask) & np.asarray(relationship_holding_mask)

    trades = []
    position = None

    times = joined.index
    fx_prices = joined["price_fx"]
    fx_vol60 = joined["vol60_fx"]

    for i, t in enumerate(times):
        c_imp = commodity_impulse.iloc[i]
        div = divergence.iloc[i]
        px = fx_prices.iloc[i]
        vol = fx_vol60.iloc[i]
        can_enter = bool(tradeable_mask[i])

        entry_signal = None
        if can_enter and abs(c_imp) >= DIVERGENCE_ENTRY_COMMODITY_THRESHOLD and abs(div) >= DIVERGENCE_ENTRY_DIVERGENCE_THRESHOLD:
            entry_signal = 1 if div > 0 else -1

        if position is not None:
            held_minutes = (t - position["entry_time"]).total_seconds() / 60.0
            adverse_excursion = -position["side"] * (np.log(px / position["entry_price"])) / max(position["entry_vol"], 1e-6)

            exit_reason = None
            if entry_signal is not None and entry_signal != position["side"] and abs(div) >= REVERSAL_STRENGTH_THRESHOLD:
                exit_reason = "reversal"
            elif abs(div) < CONVERGENCE_THRESHOLD:
                exit_reason = "convergence"
            elif held_minutes >= MAX_HOLDING_MINUTES:
                exit_reason = "time"
            elif adverse_excursion >= VOLATILITY_STOP_UNITS:
                exit_reason = "vol_stop"

            if exit_reason:
                gross_return = position["side"] * np.log(px / position["entry_price"])
                net_return = gross_return - ROUND_TRIP_COST
                trades.append({
                    "entry_time": position["entry_time"],
                    "exit_time": t,
                    "side": position["side"],
                    "held_minutes": held_minutes,
                    "gross_return": gross_return,
                    "net_return": net_return,
                    "exit_reason": exit_reason,
                    "signal_strength": position["strength"],
                })
                position = None

        if position is None and entry_signal is not None:
            strength = float(np.clip(abs(div), SIGNAL_FLOOR, SIGNAL_CAP))
            position = {
                "side": entry_signal,
                "entry_time": t,
                "entry_price": px,
                "entry_vol": vol,
                "strength": strength,
            }

    return {
        "relationship_id": rel.relationship_id,
        "formula": formula,
        "n_eval_points": len(joined),
        "n_tradeable_points": int(tradeable_mask.sum()),
        "date_range": (times[0], times[-1]),
        "trades": trades,
    }


def summarize(result: dict) -> dict:
    trades = result.get("trades", [])
    base = {"relationship_id": result["relationship_id"], "formula": result["formula"]}
    if not trades:
        base.update({"n_trades": 0, "note": result.get("error", "no trades triggered")})
        return base

    df = pd.DataFrame(trades)
    net = df["net_return"] * df["signal_strength"]
    wins = net[net > 0]
    losses = net[net <= 0]
    cumulative = (1 + net).cumprod()
    running_max = cumulative.cummax()
    drawdown = (cumulative / running_max - 1).min()

    n_days = max((df["exit_time"].max() - df["entry_time"].min()).total_seconds() / 86400, 1)
    trades_per_year = len(df) / n_days * 365
    sharpe = (net.mean() / net.std()) * np.sqrt(trades_per_year) if net.std() > 0 else float("nan")

    base.update({
        "n_trades": len(df),
        "total_return_pct": (cumulative.iloc[-1] - 1) * 100,
        "sharpe": sharpe,
        "max_drawdown_pct": drawdown * 100,
        "profit_factor": (wins.sum() / -losses.sum()) if losses.sum() != 0 else float("inf"),
        "win_rate_pct": (len(wins) / len(df)) * 100,
        "avg_holding_minutes": df["held_minutes"].mean(),
    })
    return base


def main():
    summaries = []
    for formula in ("v1", "v2"):
        for rel in RELATIONSHIPS:
            print(f"=== {rel.relationship_id} [{formula}] ===")
            try:
                result = run_relationship_backtest(rel, formula)
                summary = summarize(result)
            except FileNotFoundError as exc:
                summary = {"relationship_id": rel.relationship_id, "formula": formula, "n_trades": 0, "note": str(exc)}
            summaries.append(summary)
            for k, v in summary.items():
                print(f"  {k}: {v}")
            print()

    out_path = os.path.join(os.path.dirname(__file__), "backtest_summary.csv")
    pd.DataFrame(summaries).to_csv(out_path, index=False)
    print(f"Saved {out_path}")


if __name__ == "__main__":
    main()
