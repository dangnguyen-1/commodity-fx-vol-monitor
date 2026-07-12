RSS_FEEDS = {
    "reuters": [
    "https://news.google.com/rss/search?q=site%3Areuters.com%20commodities&hl=en-US&gl=US&ceid=US%3Aen",
    "https://news.google.com/rss/search?q=site%3Areuters.com%20oil%20OR%20gold%20OR%20copper%20OR%20natural%20gas&hl=en-US&gl=US&ceid=US%3Aen",
    "https://news.google.com/rss/search?q=site%3Areuters.com%20forex%20OR%20currency%20OR%20dollar&hl=en-US&gl=US&ceid=US%3Aen",
    ],

    "marketwatch": [
        "https://feeds.marketwatch.com/marketwatch/topstories/",
        "https://feeds.marketwatch.com/marketwatch/marketpulse/",
    ],

    "investing": [
        "https://www.investing.com/rss/news.rss",
        "https://www.investing.com/rss/news_285.rss",
        "https://www.investing.com/rss/news_14.rss",
    ],
}

SCRAPE_TARGETS = {
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