"""Does a commodity transmit better to an FX cross than to its USD pair?

THE PROBLEM
-----------
All thirty relationships trade through five instruments, and every one is a
USD pair: AUDUSD, USDCAD, EURUSD, BRLUSD, GBPUSD. So every position is
partly a dollar position, whether or not the signal said anything about the
dollar.

Worse, the measured transmission may itself be mostly dollar. Gold transmits
to EUR at 0.544 on intraday data, and Europe exports no gold. The reason
EURUSD moves with gold is that gold is priced in USD: gold up is partly USD
down, and USD down lifts every USD pair. That is mechanical, not economic.

If that is the bulk of the effect, the strategy has been trading a lagged
dollar view against instruments that price the dollar instantly, which would
explain the negative edge better than "the premise is wrong".

WHAT THIS MEASURES
------------------
T1: each commodity against every available FX instrument, USD pairs and
    crosses alike, ranked. If a cross beats the USD pair, the relative
    exposure is the real signal. If it does not, the USD leg was.

T2: the same relationships after removing the common cross-sectional FX
    move, which is a crude dollar-factor proxy. What survives is
    commodity-specific.

T3: the best cross per commodity, which is the pairing a revised strategy
    would trade.

Daily bars are the primary sample: sixteen years, every cross. One-minute
data is thinner for crosses (about a day) and is reported separately as a
sanity check rather than a conclusion.

Usage:
    .venv/bin/python3 -m strategy.research.cross_transmission --timeframe 1D
"""

from __future__ import annotations

import argparse
import math
import os
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path

import psycopg2
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = (
    PROJECT_ROOT / "paper_trading" / "data" / "paper_trading.db"
)

MINIMUM_OBSERVATIONS = 200


def ols(xs: list[float], ys: list[float]) -> tuple[float, float, float] | None:
    """Slope, R^2 and t-statistic of y on x."""
    n = len(xs)
    if n < MINIMUM_OBSERVATIONS:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx <= 0 or syy <= 0:
        return None
    beta = sxy / sxx
    r2 = (sxy * sxy) / (sxx * syy)
    residual_var = (syy - beta * sxy) / (n - 2)
    se = math.sqrt(residual_var / sxx) if residual_var > 0 else 0.0
    return beta, r2, (beta / se if se > 0 else 0.0)


def standardize(values: dict[str, float]) -> dict[str, float]:
    """Convert to z-scores so betas are comparable across instruments.

    The strategy compares volatility-normalized quantities, so raw-return
    betas would be in the wrong units for the question being asked.
    """
    series = list(values.values())
    if len(series) < 2:
        return {}
    mean = statistics.fmean(series)
    sd = statistics.pstdev(series)
    if sd <= 0:
        return {}
    return {k: (v - mean) / sd for k, v in values.items()}


def load_returns(cursor, symbol: str, timeframe: str) -> dict[str, float]:
    cursor.execute(
        """
        SELECT to_char(to_timestamp(timestamp) AT TIME ZONE 'UTC',
                       'YYYY-MM-DD HH24:MI'),
               close
        FROM market_data
        WHERE symbol = %s AND timeframe = %s
        ORDER BY timestamp
        """,
        (symbol, timeframe),
    )
    prices: dict[str, float] = {}
    for stamp, close in cursor.fetchall():
        if close is not None and float(close) > 0:
            prices[str(stamp)] = float(close)
    ordered = sorted(prices.items())
    return {
        day: math.log(cur / prev)
        for (_, prev), (day, cur) in zip(ordered, ordered[1:])
        if prev > 0 and cur > 0
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Commodity transmission to FX crosses versus USD pairs."
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--timeframe", default="1D")
    parser.add_argument("--top", type=int, default=4)
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is not set.")

    sqlite_connection = sqlite3.connect(args.database)
    registry = sqlite_connection.execute(
        """
        SELECT DISTINCT r.commodity, l.live_commodity_symbol,
               l.live_fx_symbol, l.fx_price_transform
        FROM relationships r
        JOIN live_instrument_registry l
          ON l.relationship_id = r.relationship_id
        WHERE l.active = 1
        ORDER BY r.commodity
        """
    ).fetchall()
    sqlite_connection.close()

    postgres = psycopg2.connect(database_url)
    cursor = postgres.cursor()

    cursor.execute(
        """
        SELECT DISTINCT symbol FROM market_data
        WHERE symbol LIKE 'FX:%' AND timeframe = %s
        ORDER BY symbol
        """,
        (args.timeframe,),
    )
    fx_symbols = [str(r[0]) for r in cursor.fetchall()]

    print(f"\nCommodity transmission, timeframe {args.timeframe}")
    print(f"{len(fx_symbols)} FX instruments available\n")

    fx_returns = {s: load_returns(cursor, s, args.timeframe) for s in fx_symbols}
    fx_z = {s: standardize(v) for s, v in fx_returns.items()}

    # A crude dollar factor: the average standardized move across every USD
    # pair, oriented so a positive value means a weaker dollar.
    usd_pairs = [s for s in fx_symbols if "USD" in s]
    dollar_factor: dict[str, list[float]] = defaultdict(list)
    for symbol in usd_pairs:
        sign = -1.0 if symbol.startswith("FX:USD") else 1.0
        for day, value in fx_z.get(symbol, {}).items():
            dollar_factor[day].append(sign * value)
    dollar = {
        day: statistics.fmean(values)
        for day, values in dollar_factor.items()
        if values
    }

    commodities: dict[str, str] = {}
    current_pairs: dict[str, str] = {}
    for commodity, commodity_symbol, fx_symbol, transform in registry:
        commodities.setdefault(str(commodity), str(commodity_symbol))
        current_pairs.setdefault(str(commodity), str(fx_symbol))

    header = (
        f"{'commodity':18} {'instrument':16} {'beta':>7} {'R2':>7} "
        f"{'t':>7}   {'beta ex-USD':>11} {'R2 ex-USD':>10}"
    )

    summary: list[tuple[str, str, float, str, float, float]] = []

    for commodity, commodity_symbol in commodities.items():
        commodity_returns = load_returns(cursor, commodity_symbol, args.timeframe)
        commodity_z = standardize(commodity_returns)
        if not commodity_z:
            continue

        rows = []
        for symbol in fx_symbols:
            target = fx_z.get(symbol) or {}
            days = sorted(set(commodity_z) & set(target))
            if len(days) < MINIMUM_OBSERVATIONS:
                continue
            xs = [commodity_z[d] for d in days]
            ys = [target[d] for d in days]
            fit = ols(xs, ys)
            if fit is None:
                continue

            # T2: strip the dollar factor from both sides, then refit.
            shared = [d for d in days if d in dollar]
            ex_beta = ex_r2 = float("nan")
            if len(shared) >= MINIMUM_OBSERVATIONS:
                dxs = [dollar[d] for d in shared]
                cx = [commodity_z[d] for d in shared]
                cy = [target[d] for d in shared]
                fx_on_d = ols(dxs, cy)
                cm_on_d = ols(dxs, cx)
                if fx_on_d and cm_on_d:
                    y_res = [
                        cy[i] - fx_on_d[0] * dxs[i] for i in range(len(shared))
                    ]
                    x_res = [
                        cx[i] - cm_on_d[0] * dxs[i] for i in range(len(shared))
                    ]
                    ex_fit = ols(x_res, y_res)
                    if ex_fit:
                        ex_beta, ex_r2 = ex_fit[0], ex_fit[1]
            rows.append((symbol, fit[0], fit[1], fit[2], ex_beta, ex_r2))

        if not rows:
            continue

        rows.sort(key=lambda r: -abs(r[1]))
        current = current_pairs.get(commodity, "")
        print(f"\n{commodity}  (currently trades {current.split(':')[-1]})")
        print(header)
        print("-" * len(header))
        for symbol, beta, r2, t, ex_beta, ex_r2 in rows[: args.top]:
            marker = " <- current" if symbol == current else ""
            ex_b = "        -" if math.isnan(ex_beta) else f"{ex_beta:11.3f}"
            ex_r = "         -" if math.isnan(ex_r2) else f"{ex_r2:10.4f}"
            print(
                f"{'':18} {symbol.split(':')[-1]:16} {beta:7.3f} {r2:7.4f} "
                f"{t:7.2f}   {ex_b} {ex_r}{marker}"
            )
        # Also show the current pair when it fell outside the top rows.
        if current and not any(r[0] == current for r in rows[: args.top]):
            for symbol, beta, r2, t, ex_beta, ex_r2 in rows:
                if symbol == current:
                    ex_b = "        -" if math.isnan(ex_beta) else f"{ex_beta:11.3f}"
                    ex_r = "         -" if math.isnan(ex_r2) else f"{ex_r2:10.4f}"
                    print(
                        f"{'':18} {symbol.split(':')[-1]:16} {beta:7.3f} "
                        f"{r2:7.4f} {t:7.2f}   {ex_b} {ex_r} <- current"
                    )

        best = rows[0]
        cur = next((r for r in rows if r[0] == current), None)
        summary.append(
            (
                commodity,
                best[0].split(":")[-1],
                best[2],
                current.split(":")[-1],
                cur[2] if cur else float("nan"),
                best[5],
            )
        )

    postgres.close()

    print("\n" + "=" * 78)
    print("T3: best instrument per commodity, against what it trades today\n")
    print(
        f"{'commodity':18} {'best':10} {'R2':>8}   "
        f"{'current':10} {'R2':>8}   {'gain':>7}  {'ex-USD R2':>9}"
    )
    print("-" * 78)
    gains = []
    for commodity, best, best_r2, current, cur_r2, ex_r2 in sorted(
        summary, key=lambda s: -s[2]
    ):
        gain = (
            "       -"
            if math.isnan(cur_r2)
            else f"{best_r2 - cur_r2:+7.4f}"
        )
        if not math.isnan(cur_r2):
            gains.append(best_r2 - cur_r2)
        cur_text = "       -" if math.isnan(cur_r2) else f"{cur_r2:8.4f}"
        ex_text = "        -" if math.isnan(ex_r2) else f"{ex_r2:9.4f}"
        print(
            f"{commodity[:18]:18} {best:10} {best_r2:8.4f}   "
            f"{current:10} {cur_text}   {gain}  {ex_text}"
        )

    if gains:
        print(
            f"\nmedian R2 gain from switching instrument: "
            f"{statistics.median(gains):+.4f}"
        )
    print(
        "\nbeta/R2 ex-USD strips the common dollar move from both legs. A "
        "relationship that\nsurvives it is commodity-specific; one that "
        "collapses was the dollar."
    )


if __name__ == "__main__":
    main()
