from __future__ import annotations

import hashlib
import json
import random
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import pandas as pd
import requests
from bs4 import BeautifulSoup
from googlenewsdecoder import gnewsdecoder

from data_collector.news_data.config.news_sources import (
    RSS_FEEDS,
    SCRAPE_TARGETS,
    USER_AGENTS,
)


OUTPUT_DIR = Path("data_collector/news_data/output")
OUTPUT_FILE = OUTPUT_DIR / "news_articles.csv"

# Resolving a Google News redirect costs a real network round trip
# (~1s each). This process re-polls the same RSS feeds every cycle, and
# the large majority of entries in any given poll were already seen
# last time — without a cache, every cycle would re-decode ~100
# already-known URLs per feed, which would make collection fall
# further behind its own polling cadence every time it runs.
GOOGLE_URL_CACHE_FILE = OUTPUT_DIR / "google_url_cache.json"


def article_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


def fetch_url(url: str) -> str | None:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        print(f"WARNING: Failed to fetch {url}: {exc}")
        return None


def load_google_url_cache() -> dict[str, str]:
    if not GOOGLE_URL_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(GOOGLE_URL_CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_google_url_cache(cache: dict[str, str]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    GOOGLE_URL_CACHE_FILE.write_text(json.dumps(cache))


def resolve_article_url(url: str, cache: dict[str, str]) -> str:
    """Google News RSS (used for Reuters, since Reuters no longer offers
    a direct public feed) doesn't link to the real article — its <link>
    is an opaque, JS-only redirect (news.google.com/rss/articles/...)
    that a plain HTTP follow can't resolve; it just lands back on
    Google's own page. This decodes it to the actual source URL.

    This does NOT get us the article body — Reuters blocks automated
    fetches of its article pages with DataDome bot-detection (a 401
    challenge page), and that's not something to try to defeat. What
    this does fix: article_id() was hashing Google's redirect URL, not
    the real one, so the same article surfacing in two different
    Google News searches (e.g. both the "oil" and "gold" queries) could
    get stored as two separate "articles". Resolving to the canonical
    URL first fixes that dedup bug and gives a link that actually goes
    to the source, even though the summary stays headline-only for
    Reuters specifically until a licensed feed replaces this path.

    `cache` is mutated in place with any newly-resolved URL, keyed by
    the raw Google redirect — callers persist it across runs so a
    already-seen entry never gets re-decoded on a later poll."""
    if "news.google.com" not in url:
        return url
    if url in cache:
        return cache[url]
    try:
        result = gnewsdecoder(url, interval=1)
        if result.get("status") and result.get("decoded_url"):
            resolved = result["decoded_url"]
            cache[url] = resolved
            return resolved
    except Exception as exc:
        print(f"WARNING: Could not resolve Google News URL {url}: {exc}")
    return url


def collect_rss_articles(source: str, feed_urls: list[str], google_url_cache: dict[str, str]) -> list[dict]:
    rows = []

    for feed_url in feed_urls:
        content = fetch_url(feed_url)

        if not content:
            continue

        feed = feedparser.parse(content)

        for entry in feed.entries:
            url = getattr(entry, "link", "").strip()
            title = getattr(entry, "title", "").strip()

            if not url or not title:
                continue

            url = resolve_article_url(url, google_url_cache)

            rows.append(
                {
                    "article_id": article_id(url),
                    "source": source,
                    "title": title,
                    "url": url,
                    "published": getattr(
                        entry,
                        "published",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                    "summary": getattr(entry, "summary", "")[:1000],
                }
            )

    return rows


def collect_html_articles(source: str) -> list[dict]:
    config = SCRAPE_TARGETS.get(source)

    if not config:
        return []

    html = fetch_url(config["url"])

    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")

    rows = []
    base_url = config["url"].rstrip("/")

    for selector in config["selectors"]:
        matches = soup.select(selector)

        for tag in matches:
            title = tag.get_text(strip=True)
            url = tag.get("href", "")

            if not title or len(title) < 20:
                continue

            if url.startswith("/"):
                url = base_url + url

            if not url.startswith("http"):
                continue

            rows.append(
                {
                    "article_id": article_id(url),
                    "source": source,
                    "title": title,
                    "url": url,
                    "published": datetime.utcnow().isoformat(),
                    "summary": "",
                }
            )

        if rows:
            break

    return rows


def deduplicate_articles(rows: list[dict]) -> list[dict]:
    seen = set()
    unique_rows = []

    for row in rows:
        if row["article_id"] in seen:
            continue

        seen.add(row["article_id"])
        unique_rows.append(row)

    return unique_rows


def collect_news() -> pd.DataFrame:
    rows = []
    google_url_cache = load_google_url_cache()

    for source, feed_urls in RSS_FEEDS.items():
        print(f"Collecting RSS: {source}")

        source_rows = collect_rss_articles(source, feed_urls, google_url_cache)

        if not source_rows:
            print(f"RSS empty, trying HTML scrape: {source}")
            source_rows = collect_html_articles(source)

        rows.extend(source_rows)

    save_google_url_cache(google_url_cache)

    rows = deduplicate_articles(rows)

    return pd.DataFrame(rows)


def save_output(df: pd.DataFrame) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    df.to_csv(OUTPUT_FILE, index=False)

    print(f"Saved {len(df)} articles -> {OUTPUT_FILE}")


def main() -> None:
    df = collect_news()
    save_output(df)

    print()
    print(df[["source", "title"]].head(20).to_string(index=False))


if __name__ == "__main__":
    main()