RSS_FEEDS = {
    "reuters": [
        "https://feeds.reuters.com/reuters/topNews",
        "https://feeds.reuters.com/reuters/businessNews",
        "https://feeds.reuters.com/reuters/technologyNews",
    ],

    "bloomberg": [
        "https://feeds.bloomberg.com/markets/news.rss",
        "https://feeds.bloomberg.com/technology/news.rss",
    ],

    "investing": [
        "https://www.investing.com/rss/news.rss",
        "https://www.investing.com/rss/news_285.rss",
        "https://www.investing.com/rss/news_14.rss",
    ],
}

SCRAPE_TARGETS = {
    "reuters": {
        "url": "https://www.reuters.com",
        "selectors": [
            "a[data-testid='Heading']",
            "h3 a",
            "article a",
        ],
    },

    "investing": {
        "url": "https://www.investing.com/news/latest-news",
        "selectors": [
            "article a.title",
            "a.title",
            "div.textDiv a",
        ],
    },
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]