import time

from data_collector.news_data.collectors.news_sentiment import (
    get_unscored_articles,
    score_article,
)

POLL_INTERVAL_SECONDS = 60


def main():
    print(
        f"Starting sentiment stream. Polling every {POLL_INTERVAL_SECONDS} seconds."
    )

    while True:
        try:
            articles = get_unscored_articles()

            if articles:
                print(f"Found {len(articles)} unscored articles")

                for article_id, title, summary in articles:
                    try:
                        print()
                        print(f"Scoring article {article_id}")
                        print(title)

                        inserted = score_article(
                            article_id=article_id,
                            title=title,
                            summary=summary,
                        )

                        print(f"Inserted {inserted} impacts")

                    except Exception as exc:
                        print(
                            f"ERROR article {article_id}: {exc}"
                        )

            else:
                print("No unscored articles found")

        except Exception as exc:
            print(f"STREAM ERROR: {exc}")

        print(f"Sleeping {POLL_INTERVAL_SECONDS} seconds...")
        print()

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()