"""
Commodity news sentiment via the pipeline's read-only API
(api/app.py's /news/latest route), LLM-classified real
headlines with direction, confidence, and reasoning, rather than raw tone
averaging. Sentiment is scaled by 10 (pipeline sentiment is -1..+1; the
rest of this app's thresholds and the confluence screener were built
to a roughly -10..+10 tone range) so nothing downstream needs
to change to accommodate the new source.
"""

from __future__ import annotations

import logging
import time

import requests

from config import PIPELINE_API_BASE_URL

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 8
_CACHE_TTL = 15 * 60  # 15 minutes

# Circuit breaker: once the pipeline API proves unreachable, stop spending
# a full timeout on every subsequent commodity switch for a while. Nine
# commodities x an unreachable host used to mean the News tab could sit
# "loading" for a long time; this caps the damage to one slow attempt per
# cooldown window and falls back immediately otherwise.
_CIRCUIT_COOLDOWN = 90  # seconds
_last_failure_at: float | None = None


def _pipeline_available() -> bool:
    return _last_failure_at is None or (time.time() - _last_failure_at) >= _CIRCUIT_COOLDOWN


def news_service_unreachable() -> bool:
    """True when the last pipeline API attempt failed and we're still in
    the cooldown window, lets the UI say "the news service is
    unreachable right now" instead of the misleading "no news found for
    this topic" when the real cause is a dead connection."""
    return _last_failure_at is not None and not _pipeline_available()


# Dashboard commodity name -> the pipeline's news-classifier asset name
# (data_collector/news_data/config/assets.py's naming, distinct from both
# the Yahoo/TradingView symbol and the Comtrade commodity name).
COMMODITY_NEWS_ASSET: dict[str, str] = {
    "WTI Crude":   "Crude Oil",
    "Brent Crude": "Brent Oil",
    "Natural Gas": "Natural Gas",
    "Gold":        "Gold",
    "Silver":      "Silver",
    "Copper":      "Copper",
    "Wheat":       "Wheat",
    "Corn":        "Corn",
    "Soybeans":    "Soybeans",
}

_cache: dict[str, tuple[float, list[dict]]] = {}


def _cached_news_latest(asset: str, asset_type: str, limit: int) -> list[dict]:
    cache_key = f"{asset}|{asset_type}|{limit}"
    cached = _cache.get(cache_key)
    if cached is not None and (time.time() - cached[0]) < _CACHE_TTL:
        return cached[1]

    if not _pipeline_available():
        logger.debug("Pipeline news API circuit open, skipping request for %r", asset)
        return []

    global _last_failure_at
    try:
        resp = requests.get(
            f"{PIPELINE_API_BASE_URL}/news/latest",
            params={"asset": asset, "asset_type": asset_type, "limit": limit},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        _cache[cache_key] = (time.time(), items)
        _last_failure_at = None
        return items
    except Exception as exc:
        logger.warning("Pipeline news fetch failed: %s", exc)
        _last_failure_at = time.time()
        return []


# Google News appends " - <Publisher>" to every headline it syndicates, so
# 1,401 of the stored titles end in " - Reuters". Stripping the last " - X"
# blindly would also damage real headlines: the same scan found endings like
# "- Why does it matter?" and "- report". So only known publishers are
# removed, matched case-insensitively.
_PUBLISHER_SUFFIXES = (
    "Reuters", "reuters.com",
    "Bloomberg", "Bloomberg.com",
    "Investing.com", "MarketWatch", "WSJ",
)

# The stored source is the RSS_FEEDS key, which is lower case. These are
# what a reader should see.
SOURCE_DISPLAY_NAMES = {
    "reuters": "Reuters",
    "investing": "Investing",
    "bloomberg": "Bloomberg",
    "marketwatch": "MarketWatch",
}


def strip_publisher_suffix(headline: str) -> str:
    """Remove a trailing publisher attribution, leaving the headline."""
    for separator in (" - ", " \u2013 ", " \u2014 "):
        for publisher in _PUBLISHER_SUFFIXES:
            suffix = f"{separator}{publisher}"
            if headline.lower().endswith(suffix.lower()):
                return headline[: -len(suffix)].strip()
    return headline


def display_source(source: str) -> str:
    """Publisher name as shown to a reader, from the stored feed key."""
    key = (source or "").strip()
    return SOURCE_DISPLAY_NAMES.get(key.lower(), key.title())


def _format_articles(raw: list[dict]) -> list[dict]:
    out = []
    for a in raw:
        headline = strip_publisher_suffix((a.get("headline") or "").strip())
        if not headline:
            continue
        tone = round(float(a.get("sentiment") or 0.0) * 10, 1)
        published = (a.get("publication_timestamp_utc") or "")[:10].replace("-", "")
        out.append({
            "title": headline,
            "url": a.get("url", ""),
            "source": display_source(a.get("source_name", "")),
            "date": published,
            "tone": tone,
            "reasoning": a.get("reasoning", ""),
            "confidence": a.get("confidence"),
        })
    return out


def commodity_news(commodity: str, max_records: int = 8) -> list[dict]:
    """Latest classified news for a commodity. Returns list of {title,
    url, source, date, tone, reasoning, confidence}."""
    asset = COMMODITY_NEWS_ASSET.get(commodity)
    if not asset:
        return []
    items = _cached_news_latest(asset, "commodity", max_records)
    return _format_articles(items)


def news_sentiment_score(commodity: str) -> float:
    """
    Mean classified sentiment for latest commodity news, scaled to match
    a -10..+10 tone range. Negative = bearish/risk-on. Returns float in
    roughly [-10, +10] range (0.0 when nothing's been classified for this
    commodity yet, same as "no news found").
    """
    articles = commodity_news(commodity, max_records=20)
    if not articles:
        return 0.0
    return round(sum(a["tone"] for a in articles) / len(articles), 2)
