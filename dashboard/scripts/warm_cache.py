"""
Pre-warms the on-disk caches for UN Comtrade and World Bank data before the
app starts serving real traffic.

Without this, the first real visitor to hit Trade Flows or Country
Exposure after a fresh deploy triggers a cold-cache fetch — UN Comtrade's
rate limiting can stretch that first load past 60-90 seconds, long enough
to trip some platforms' request/proxy timeouts and show an error instead
of the data. Run this once as part of your platform's build or release
step (before the web process starts taking traffic), not inline in a
request — it can take several minutes across all 9 commodities.

Safe to run repeatedly: every fetch it triggers already goes through this
app's normal disk caches (7-day TTL for Comtrade, 24h for World Bank), so
re-running this after a cache is already warm just confirms it's warm
rather than refetching.

Usage: python scripts/warm_cache.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import NAMES
from data.comtrade import bilateral_export_partners_batch, bilateral_import_partners_batch
from data.trade_data import top_traders
from data.worldbank import fetch_country_indicators

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("warm_cache")

# Matches the Trade Flows tab's default "Show top N countries" slider
# value — the case an actual first-time visitor hits before touching any
# controls.
_DEFAULT_TOP_N = 10
_BILATERAL_PARTNERS_SHOWN = 8


def main() -> None:
    logger.info("Warming World Bank cache...")
    try:
        fetch_country_indicators()
    except Exception as exc:
        logger.warning("World Bank warm-up failed (non-fatal): %s", exc)

    for name in NAMES:
        logger.info("Warming Comtrade cache for %s...", name)
        try:
            traders = top_traders(name, top_n=_DEFAULT_TOP_N)
        except Exception as exc:
            logger.warning("Comtrade aggregate warm-up failed for %s: %s", name, exc)
            continue

        period = traders.get("period")
        if not period:
            continue

        if not traders["exporters"].empty:
            try:
                bilateral_export_partners_batch(
                    name, list(traders["exporters"]["reporter_iso3"]), period,
                    top_n_partners=_BILATERAL_PARTNERS_SHOWN,
                )
            except Exception as exc:
                logger.warning("Export bilateral warm-up failed for %s: %s", name, exc)

        if not traders["importers"].empty:
            try:
                bilateral_import_partners_batch(
                    name, list(traders["importers"]["reporter_iso3"]), period,
                    top_n_partners=_BILATERAL_PARTNERS_SHOWN,
                )
            except Exception as exc:
                logger.warning("Import bilateral warm-up failed for %s: %s", name, exc)

    logger.info("Cache warm-up complete.")


if __name__ == "__main__":
    main()
