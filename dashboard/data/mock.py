"""Synthetic price series, for running the dashboard without a database.

Selected with DATA_SOURCE=mock. Every other source needs Postgres or a
network call; this one needs neither, so the UI can be worked on offline.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd


# Rough real-world levels, so charts and thresholds land in a plausible
# range rather than all starting at 100.
SEED_PRICES: dict[str, float] = {
    "CL1 Comdty": 80.0,
    "CO1 Comdty": 85.0,
    "NG1 Comdty": 2.5,
    "GC1 Comdty": 1950.0,
    "SI1 Comdty": 23.0,
    "HG1 Comdty": 3.8,
    "W 1 Comdty": 580.0,
    "C 1 Comdty": 430.0,
    "S 1 Comdty": 1200.0,
}


def fetch_mock_prices(
    tickers: list[str],
    lookback_days: int = 365,
) -> pd.DataFrame:
    """Geometric Brownian motion per ticker, indexed by business day.

    Seeded, so the same run produces the same series and a UI change can
    be compared against a previous screenshot.
    """
    rng = np.random.default_rng(42)
    dates = pd.date_range(end=date.today(), periods=lookback_days, freq="B")

    data: dict[str, np.ndarray] = {}
    for ticker in tickers:
        start = SEED_PRICES.get(ticker, 100.0)
        volatility = rng.uniform(0.15, 0.50)
        step = 1 / 252
        shocks = rng.normal(
            -0.5 * volatility**2 * step,
            volatility * step**0.5,
            len(dates),
        )
        data[ticker] = start * np.exp(np.cumsum(shocks))

    frame = pd.DataFrame(data, index=dates)
    frame.index.name = "date"
    return frame
