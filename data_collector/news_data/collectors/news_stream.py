import time

from data_collector.news_data.collectors.news_collector import (
    collect_news,
    save_output,
)
from data_collector.news_data.collectors.load_news_to_db import load_news_articles


POLL_INTERVAL_SECONDS = 60


def run_once() -> None:
    print("Collecting news...")

    df = collect_news()
    save_output(df)

    if df.empty:
        print("No articles collected.")
        return

    load_news_articles()


def main() -> None:
    print(f"Starting news stream. Polling every {POLL_INTERVAL_SECONDS} seconds.")

    while True:
        try:
            run_once()
        except Exception as exc:
            print(f"ERROR: news stream cycle failed: {exc}")

        print(f"Sleeping {POLL_INTERVAL_SECONDS} seconds...\n")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()