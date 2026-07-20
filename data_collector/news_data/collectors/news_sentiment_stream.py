import time

from bs4 import BeautifulSoup

from data_collector.news_data.collectors.news_sentiment import (
    append_failure_log,
    get_openai_client,
    get_unscored_articles,
    normalize_impacts,
    save_failure,
    save_success,
    score_article,
)


POLL_INTERVAL_SECONDS = 60


def clean_summary(
    title: str,
    summary: str | None,
) -> str:
    """
    Remove RSS HTML. Return an empty summary when
    the feed merely repeats the article headline.
    """
    plain_text = BeautifulSoup(
        summary or "",
        "html.parser",
    ).get_text(
        " ",
        strip=True,
    )

    plain_text = " ".join(
        plain_text.split()
    )

    title_core = title.rsplit(
        " - ",
        1,
    )[0].strip()

    if not plain_text:
        return ""

    if (
        title_core
        and title_core.lower()
        in plain_text.lower()
        and len(plain_text)
        <= len(title_core) + 30
    ):
        return ""

    return plain_text


def run_once() -> None:
    articles = get_unscored_articles()

    if not articles:
        print("No unscored articles found")
        return

    print(f"Found {len(articles)} unscored articles")

    client = get_openai_client()

    for article_id, title, summary in articles:
        print()
        print(f"Scoring article {article_id}")
        print(title)

        try:
            response = score_article(
                client,
                title,
                clean_summary(
                    title,
                    summary,
                ),
            )

            raw_impacts = response.get(
                "impacts",
                [],
            )

            impacts = normalize_impacts(
                raw_impacts
            )

            save_success(
                article_id,
                impacts,
            )

            print(
                f"Inserted {len(impacts)} impacts"
            )

        except Exception as exc:
            error = str(exc)

            save_failure(
                article_id,
                error,
            )

            append_failure_log(
                article_id,
                title,
                error,
            )

            print(
                f"ERROR article {article_id}: "
                f"{error}"
            )


def main() -> None:
    print(
        "Starting sentiment stream. "
        f"Polling every "
        f"{POLL_INTERVAL_SECONDS} seconds."
    )

    while True:
        try:
            run_once()
        except Exception as exc:
            print(
                f"STREAM ERROR: {exc}"
            )

        print(
            f"Sleeping "
            f"{POLL_INTERVAL_SECONDS} seconds..."
        )
        print()

        time.sleep(
            POLL_INTERVAL_SECONDS
        )


if __name__ == "__main__":
    main()
