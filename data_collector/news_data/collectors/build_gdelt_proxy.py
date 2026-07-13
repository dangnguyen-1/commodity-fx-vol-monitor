from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import numpy as np
import pandas as pd


DEFAULT_INPUT = Path(
    "data_collector/news_data/input/gdelt_historical.csv"
)
DEFAULT_ARTICLE_OUTPUT = Path(
    "data_collector/news_data/output/gdelt_clean_articles.csv"
)
DEFAULT_SOURCE_DAILY_OUTPUT = Path(
    "data_collector/news_data/output/gdelt_source_daily_sentiment.csv"
)
DEFAULT_DAILY_OUTPUT = Path(
    "data_collector/news_data/output/gdelt_daily_sentiment.csv"
)

REQUIRED_COLUMNS = {
    "date",
    "GoldsteinScale",
    "NumArticles",
    "AvgTone",
    "SOURCEURL",
}

ROLLING_WINDOW_DAYS = 365
MIN_HISTORY_OBSERVATIONS = 30
Z_CLIP = 3.0
EPSILON = 1e-9

SOURCE_ORDER = ["reuters", "marketwatch", "investing"]


def canonical_url(url: str) -> str:
    """Normalize a URL without changing the underlying article path."""
    if not isinstance(url, str):
        return ""

    url = url.strip()
    if not url:
        return ""

    try:
        parts = urlsplit(url)

        scheme = parts.scheme.lower() or "https"
        host = parts.netloc.lower().split(":")[0]

        if host.startswith("www."):
            host = host[4:]

        excluded_query_keys = {
            "ref",
            "src",
            "output",
            "ved",
            "cmpid",
            "mod",
        }

        query_items = [
            (key, value)
            for key, value in parse_qsl(
                parts.query,
                keep_blank_values=True,
            )
            if not key.lower().startswith("utm_")
            and key.lower() not in excluded_query_keys
        ]

        normalized_path = parts.path.rstrip("/") or "/"

        return urlunsplit(
            (
                scheme,
                host,
                normalized_path,
                urlencode(query_items, doseq=True),
                "",
            )
        )

    except ValueError:
        return ""


def source_family(url: str) -> str | None:
    """Map historical URLs to the same three source families used live."""
    if not isinstance(url, str):
        return None

    try:
        host = urlsplit(url).netloc.lower().split(":")[0]
    except ValueError:
        return None

    if host == "reuters.com" or host.endswith(".reuters.com"):
        return "reuters"

    if (
        host == "thomsonreuters.com"
        or host.endswith(".thomsonreuters.com")
    ):
        return "reuters"

    if host == "marketwatch.com" or host.endswith(".marketwatch.com"):
        return "marketwatch"

    if host == "investing.com" or host.endswith(".investing.com"):
        return "investing"

    return None


def robust_mad(values: np.ndarray) -> float:
    median = np.median(values)
    return float(np.median(np.abs(values - median)))


def add_causal_source_zscore(group: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize one source using only observations available before each date.

    The current observation is excluded with shift(1), preventing look-ahead.
    """
    group = group.sort_values("date").copy()
    indexed = group.set_index("date")

    raw = indexed["news_sentiment_raw"]
    history = raw.shift(1)

    rolling = history.rolling(
        f"{ROLLING_WINDOW_DAYS}D",
        min_periods=MIN_HISTORY_OBSERVATIONS,
    )

    center = rolling.median()
    mad = rolling.apply(robust_mad, raw=True)
    robust_scale = 1.4826 * mad
    fallback_std = rolling.std(ddof=0)

    scale = robust_scale.where(
        robust_scale > EPSILON,
        fallback_std,
    )
    scale = scale.where(scale > EPSILON)

    indexed["source_sentiment_center"] = center
    indexed["source_sentiment_scale"] = scale
    indexed["source_sentiment_z"] = (
        (raw - center) / scale
    ).clip(-Z_CLIP, Z_CLIP)

    return indexed.reset_index()


def load_and_clean_raw(input_path: Path) -> pd.DataFrame:
    if not input_path.exists():
        raise FileNotFoundError(f"Missing input file: {input_path}")

    raw = pd.read_csv(input_path)

    missing_columns = sorted(REQUIRED_COLUMNS - set(raw.columns))
    if missing_columns:
        raise ValueError(
            "GDELT file is missing required columns: "
            + ", ".join(missing_columns)
        )

    raw["news_date"] = pd.to_datetime(
        raw["date"],
        errors="coerce",
    ).dt.normalize()

    for column in ("AvgTone", "GoldsteinScale", "NumArticles"):
        raw[column] = pd.to_numeric(raw[column], errors="coerce")

    raw["canonical_url"] = raw["SOURCEURL"].map(canonical_url)
    raw["source"] = raw["canonical_url"].map(source_family)

    raw = raw[
        raw["news_date"].notna()
        & raw["AvgTone"].notna()
        & raw["canonical_url"].ne("")
        & raw["source"].notna()
    ].copy()

    return raw


def deduplicate_articles(raw: pd.DataFrame) -> pd.DataFrame:
    """
    Keep one causal article observation per canonical URL.

    If GDELT repeats the same URL on later dates, only rows from the URL's
    earliest observed date are used. Multiple GDELT event rows on that first
    date are collapsed with medians.
    """
    earliest_date = raw.groupby(
        "canonical_url"
    )["news_date"].transform("min")

    earliest_rows = raw[
        raw["news_date"].eq(earliest_date)
    ].copy()

    articles = (
        earliest_rows.groupby(
            ["canonical_url", "news_date", "source"],
            as_index=False,
        )
        .agg(
            source_url=("SOURCEURL", "first"),
            news_sentiment_raw=("AvgTone", "median"),
            goldstein_median=("GoldsteinScale", "median"),
            gdelt_num_articles_median=("NumArticles", "median"),
            gdelt_event_rows=("SOURCEURL", "size"),
        )
    )

    # The raw file contains dates but not reliable publication timestamps.
    # Make every article usable no earlier than the next business day.
    articles["date"] = (
        articles["news_date"] + pd.offsets.BDay(1)
    ).dt.normalize()

    articles = articles[
        [
            "date",
            "news_date",
            "source",
            "source_url",
            "canonical_url",
            "news_sentiment_raw",
            "goldstein_median",
            "gdelt_num_articles_median",
            "gdelt_event_rows",
        ]
    ].sort_values(
        ["date", "source", "canonical_url"]
    )

    return articles.reset_index(drop=True)


def build_source_daily(articles: pd.DataFrame) -> pd.DataFrame:
    source_daily = (
        articles.groupby(
            ["date", "source"],
            as_index=False,
        )
        .agg(
            news_sentiment_raw=(
                "news_sentiment_raw",
                "median",
            ),
            news_sentiment_mean=(
                "news_sentiment_raw",
                "mean",
            ),
            news_article_count=(
                "canonical_url",
                "size",
            ),
            goldstein_median=(
                "goldstein_median",
                "median",
            ),
        )
    )

    normalized_frames = []

    for _, group in source_daily.groupby(
        "source",
        sort=False,
    ):
        normalized_frames.append(
            add_causal_source_zscore(group)
        )

    normalized = pd.concat(
        normalized_frames,
        ignore_index=True,
    )

    normalized["source"] = pd.Categorical(
        normalized["source"],
        categories=SOURCE_ORDER,
        ordered=True,
    )

    normalized = normalized.sort_values(
        ["date", "source"]
    ).reset_index(drop=True)

    normalized["source"] = normalized["source"].astype(str)

    return normalized


def build_combined_daily(
    articles: pd.DataFrame,
    source_daily: pd.DataFrame,
) -> pd.DataFrame:
    article_daily = (
        articles.groupby("date", as_index=False)
        .agg(
            news_sentiment_raw=(
                "news_sentiment_raw",
                "median",
            ),
            news_sentiment_mean=(
                "news_sentiment_raw",
                "mean",
            ),
            news_article_count=(
                "canonical_url",
                "size",
            ),
            news_source_count=(
                "source",
                "nunique",
            ),
            goldstein_median=(
                "goldstein_median",
                "median",
            ),
        )
    )

    normalized_daily = (
        source_daily.groupby("date", as_index=False)
        .agg(
            # Equal-weight available source families so Reuters does not
            # dominate merely because it has more historical articles.
            news_sentiment_z=(
                "source_sentiment_z",
                "mean",
            ),
            normalized_source_count=(
                "source_sentiment_z",
                "count",
            ),
        )
    )

    source_z = source_daily.pivot(
        index="date",
        columns="source",
        values="source_sentiment_z",
    ).rename(
        columns={
            source: f"{source}_sentiment_z"
            for source in SOURCE_ORDER
        }
    )

    source_counts = source_daily.pivot(
        index="date",
        columns="source",
        values="news_article_count",
    ).rename(
        columns={
            source: f"{source}_article_count"
            for source in SOURCE_ORDER
        }
    )

    combined = article_daily.merge(
        normalized_daily,
        on="date",
        how="left",
    )

    combined = combined.merge(
        source_z.reset_index(),
        on="date",
        how="left",
    )

    combined = combined.merge(
        source_counts.reset_index(),
        on="date",
        how="left",
    )

    calendar = pd.DataFrame(
        {
            "date": pd.date_range(
                combined["date"].min(),
                combined["date"].max(),
                freq="B",
            )
        }
    )

    daily = calendar.merge(
        combined,
        on="date",
        how="left",
    )

    count_columns = [
        "news_article_count",
        "news_source_count",
        "normalized_source_count",
        *[
            f"{source}_article_count"
            for source in SOURCE_ORDER
        ],
    ]

    for column in count_columns:
        if column not in daily.columns:
            daily[column] = 0

        daily[column] = (
            daily[column]
            .fillna(0)
            .astype(int)
        )

    daily["news_available"] = (
        daily["news_article_count"] > 0
    ).astype(int)

    last_news_date = daily["date"].where(
        daily["news_available"].eq(1)
    ).ffill()

    daily["days_since_news"] = (
        daily["date"] - last_news_date
    ).dt.days.astype("Int64")

    daily["sentiment_provider"] = "gdelt_proxy"

    ordered_columns = [
        "date",
        "news_sentiment_raw",
        "news_sentiment_mean",
        "news_sentiment_z",
        "news_article_count",
        "news_source_count",
        "normalized_source_count",
        "news_available",
        "days_since_news",
        "reuters_sentiment_z",
        "marketwatch_sentiment_z",
        "investing_sentiment_z",
        "reuters_article_count",
        "marketwatch_article_count",
        "investing_article_count",
        "goldstein_median",
        "sentiment_provider",
    ]

    for column in ordered_columns:
        if column not in daily.columns:
            daily[column] = pd.NA

    return daily[ordered_columns]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a causal daily GDELT sentiment proxy for "
            "historical backtesting."
        )
    )

    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
    )
    parser.add_argument(
        "--article-output",
        type=Path,
        default=DEFAULT_ARTICLE_OUTPUT,
    )
    parser.add_argument(
        "--source-daily-output",
        type=Path,
        default=DEFAULT_SOURCE_DAILY_OUTPUT,
    )
    parser.add_argument(
        "--daily-output",
        type=Path,
        default=DEFAULT_DAILY_OUTPUT,
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    raw = load_and_clean_raw(args.input)
    articles = deduplicate_articles(raw)
    source_daily = build_source_daily(articles)
    daily = build_combined_daily(articles, source_daily)

    for path in (
        args.article_output,
        args.source_daily_output,
        args.daily_output,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)

    articles.to_csv(args.article_output, index=False)
    source_daily.to_csv(args.source_daily_output, index=False)
    daily.to_csv(args.daily_output, index=False)

    print("GDELT historical proxy completed")
    print(f"Usable raw event rows: {len(raw):,}")
    print(f"Unique causal articles: {len(articles):,}")
    print(
        "Signal date range: "
        f"{daily['date'].min().date()} to "
        f"{daily['date'].max().date()}"
    )
    print(f"Business-day rows: {len(daily):,}")
    print(
        "Days with news: "
        f"{int(daily['news_available'].sum()):,}"
    )
    print(
        "Days with normalized sentiment: "
        f"{int(daily['news_sentiment_z'].notna().sum()):,}"
    )

    print("\nUnique articles by source:")
    print(
        articles["source"]
        .value_counts()
        .reindex(SOURCE_ORDER)
        .fillna(0)
        .astype(int)
        .to_string()
    )

    print("\nOutputs:")
    print(f"- {args.article_output}")
    print(f"- {args.source_daily_output}")
    print(f"- {args.daily_output}")


if __name__ == "__main__":
    main()