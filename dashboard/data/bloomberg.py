"""
Bloomberg Terminal data fetcher using the official blpapi SDK.

Requires:
  - Bloomberg Terminal running locally
  - blpapi Python SDK installed (pip install blpapi)
  - Terminal listening on localhost:8194 (default)
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import blpapi
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_HOST = "localhost"
_PORT = 8194
_REF_DATA_SVC = "//blp/refdata"


class BloombergSession:
    """Context-manager wrapper around a blpapi session."""

    def __init__(self, host: str = _HOST, port: int = _PORT) -> None:
        opts = blpapi.SessionOptions()
        opts.setServerHost(host)
        opts.setServerPort(port)
        self._session = blpapi.Session(opts)

    def __enter__(self) -> "BloombergSession":
        if not self._session.start():
            raise RuntimeError(
                "Could not start Bloomberg session, is the Terminal running?"
            )
        if not self._session.openService(_REF_DATA_SVC):
            raise RuntimeError(f"Could not open Bloomberg service {_REF_DATA_SVC}")
        self._service = self._session.getService(_REF_DATA_SVC)
        logger.info("Bloomberg session started")
        return self

    def __exit__(self, *_) -> None:
        self._session.stop()
        logger.info("Bloomberg session stopped")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def historical_prices(
        self,
        tickers: list[str],
        start: date,
        end: date,
        field: str = "PX_LAST",
        periodicity: str = "DAILY",
    ) -> pd.DataFrame:
        """
        Return a DataFrame indexed by date with one column per ticker,
        containing `field` values (default: last price).
        """
        req = self._service.createRequest("HistoricalDataRequest")
        for t in tickers:
            req.append("securities", t)
        req.append("fields", field)
        req.set("startDate", start.strftime("%Y%m%d"))
        req.set("endDate", end.strftime("%Y%m%d"))
        req.set("periodicitySelection", periodicity)
        req.set("nonTradingDayFillOption", "PREVIOUS_VALUE")
        req.set("nonTradingDayFillMethod", "PREVIOUS_VALUE")

        self._session.sendRequest(req)

        raw: dict[str, dict] = {t: {} for t in tickers}

        while True:
            event = self._session.nextEvent(2_000)
            for msg in event:
                if msg.messageType() == blpapi.Name("HistoricalDataResponse"):
                    sec_data = msg.getElement("securityData")
                    ticker = sec_data.getElementAsString("security")
                    field_data = sec_data.getElement("fieldData")
                    for i in range(field_data.numValues()):
                        row = field_data.getValueAsElement(i)
                        dt = row.getElementAsDatetime("date")
                        px = row.getElementAsFloat(field)
                        raw[ticker][dt] = px
            if event.eventType() == blpapi.Event.RESPONSE:
                break

        df = pd.DataFrame(raw)
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()
        df.index.name = "date"
        return df


# ------------------------------------------------------------------
# Convenience function (opens/closes session for each call)
# ------------------------------------------------------------------

def fetch_prices(tickers: list[str], lookback_days: int = 365) -> pd.DataFrame:
    """Fetch `lookback_days` of daily close prices for the given tickers."""
    end = date.today()
    start = end - timedelta(days=lookback_days)
    with BloombergSession() as sess:
        return sess.historical_prices(tickers, start, end)


# ------------------------------------------------------------------
# Mock data (for development without a Terminal)
# ------------------------------------------------------------------

def fetch_mock_prices(tickers: list[str], lookback_days: int = 365) -> pd.DataFrame:
    """
    Generate synthetic commodity price series using geometric Brownian motion.
    Used when USE_MOCK_DATA is True in config.py.
    """
    rng = np.random.default_rng(42)
    dates = pd.date_range(end=date.today(), periods=lookback_days, freq="B")

    seed_prices = {
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

    data: dict[str, np.ndarray] = {}
    for t in tickers:
        s0 = seed_prices.get(t, 100.0)
        vol = rng.uniform(0.15, 0.50)
        drift = 0.0
        dt = 1 / 252
        shocks = rng.normal((drift - 0.5 * vol**2) * dt, vol * dt**0.5, len(dates))
        prices = s0 * np.exp(np.cumsum(shocks))
        data[t] = prices

    df = pd.DataFrame(data, index=dates)
    df.index.name = "date"
    return df
