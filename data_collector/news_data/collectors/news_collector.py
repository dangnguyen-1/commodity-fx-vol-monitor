from __future__ import annotations

import hashlib
import random
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import pandas as pd
import requests
from bs4 import BeautifulSoup

from data_collector.news_data.config.news_sources import (
    RSS_FEEDS,
    SCRAPE_TARGETS,
    USER_AGENTS,
)


OUTPUT_DIR = Path("data_collector/news_data/output")
OUTPUT_FILE = OUTPUT_DIR / "news_articles.csv"


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


def collect_rss_articles(source: str, feed_urls: list[str]) -> list[dict]:
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

    for source, feed_urls in RSS_FEEDS.items():
        print(f"Collecting RSS: {source}")

        source_rows = collect_rss_articles(source, feed_urls)

        if not source_rows:
            print(f"RSS empty, trying HTML scrape: {source}")
            source_rows = collect_html_articles(source)

        rows.extend(source_rows)

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