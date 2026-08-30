// pm2 process definitions for the whole live pipeline. Start everything:
//   pm2 start ecosystem.config.js
// Persist across reboots/logout:
//   pm2 save && pm2 startup   (then run the command pm2 startup prints)
// Check on it:
//   pm2 status
//   pm2 logs <name>

module.exports = {
  apps: [
    {
      // Real-time TradingView market data — a persistent WebSocket
      // connection, needs to just keep running.
      name: "tv-stream",
      script: "data_collector/market_data/collectors/stream_many.js",
      interpreter: "node",
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 20,
    },
    {
      // Collects raw articles (RSS feeds) into news_articles — its own
      // while-loop with a sleep, same "just keep running" shape as the
      // TV stream. Classification is a separate process (below).
      name: "news-stream",
      script: "scripts/run_news_stream.sh",
      interpreter: "bash",
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 20,
    },
    {
      // LLM classification of whatever news-stream has collected —
      // produces the news_classifications/news_classification_assets
      // rows the dashboard's /news/latest actually reads.
      name: "news-sentiment-stream",
      script: "scripts/run_news_sentiment_stream.sh",
      interpreter: "bash",
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 20,
    },
    {
      // Bridges news-stream/news-sentiment-stream's Postgres output into
      // the SQLite database the API's /news/latest endpoint actually
      // reads from — see paper_trading/news_data/sync_news.py. Without
      // this running, the classified articles pile up in Postgres but
      // the dashboard never sees anything past whenever this last ran.
      // Incremental/checkpointed, so a 5-minute cadence is cheap.
      name: "news-sync",
      script: "scripts/sync_news.sh",
      interpreter: "bash",
      autorestart: false,
      cron_restart: "*/5 * * * *",
    },
    {
      // UN Comtrade is monthly-cadence data, not real-time — this exits
      // after each run (autorestart off) and pm2's cron_restart re-fires
      // it daily to catch newly-published months. The backfill script
      // checkpoints completed periods, so a daily re-run only fetches
      // what's actually new, not the full 2010-present history each time.
      name: "comtrade-refresh",
      script: "scripts/refresh_comtrade.sh",
      interpreter: "bash",
      autorestart: false,
      cron_restart: "0 6 * * *",
    },
    {
      // The pipeline's read-only API — the dashboard's only way to reach
      // market_data/fundamental_trade_data/paper-trading state.
      name: "pipeline-api",
      script: "scripts/run_pipeline_api.sh",
      interpreter: "bash",
      autorestart: true,
      restart_delay: 3000,
    },
    {
      // The Dash dashboard itself.
      name: "dashboard",
      script: "dashboard/app.py",
      interpreter: ".venv/bin/python3",
      autorestart: true,
      restart_delay: 3000,
    },
  ],
};
