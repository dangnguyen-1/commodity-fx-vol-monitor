# The three original Reuters queries covered oil, gold, copper and gas, and
# the classified coverage showed exactly that shape: 249 rows for crude and
# 242 for Brent over seven days against 1 for silver and 1 for zinc. That is
# not a classifier limitation, it is that nothing else was ever collected.
# The queries below name the commodities the dashboard actually tracks.
RSS_FEEDS = {
    "reuters": [
    "https://news.google.com/rss/search?q=site%3Areuters.com%20commodities&hl=en-US&gl=US&ceid=US%3Aen",
    "https://news.google.com/rss/search?q=site%3Areuters.com%20oil%20OR%20gold%20OR%20copper%20OR%20natural%20gas&hl=en-US&gl=US&ceid=US%3Aen",
    "https://news.google.com/rss/search?q=site%3Areuters.com%20forex%20OR%20currency%20OR%20dollar&hl=en-US&gl=US&ceid=US%3Aen",
    # Precious and industrial metals beyond gold and copper.
    "https://news.google.com/rss/search?q=site%3Areuters.com%20silver%20OR%20platinum%20OR%20palladium%20OR%20%22iron%20ore%22%20OR%20aluminium%20OR%20nickel%20OR%20zinc&hl=en-US&gl=US&ceid=US%3Aen",
    # Grains, softs and livestock, none of which had a query at all.
    "https://news.google.com/rss/search?q=site%3Areuters.com%20wheat%20OR%20corn%20OR%20soybeans%20OR%20sugar%20OR%20coffee%20OR%20cotton%20OR%20cocoa%20OR%20cattle&hl=en-US&gl=US&ceid=US%3Aen",
    # Bulk fuels and battery materials.
    "https://news.google.com/rss/search?q=site%3Areuters.com%20coal%20OR%20uranium%20OR%20lithium%20OR%20LNG&hl=en-US&gl=US&ceid=US%3Aen",
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