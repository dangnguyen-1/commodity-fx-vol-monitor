// pm2 process definitions for the whole live pipeline. Start everything:
//   pm2 start ecosystem.config.js
// Persist across reboots/logout:
//   pm2 save && pm2 startup   (then run the command pm2 startup prints)
// Check on it:
//   pm2 status
//   pm2 logs <name>

// NOTE: market-sync and health-watchdog are deliberately NOT here. Its pm2 cron_restart silently
// stopped firing after running fine for a day, leaving the engine's SQLite
// copy three hours behind a perfectly healthy Postgres. The job itself takes
// 0.4 seconds and works when invoked directly, so this was pm2's scheduler
// rather than the script. It now runs from the user crontab, which has been
// reliable for the cost reminders.
//
// health-watchdog followed it for a stronger reason: a monitor scheduled by
// the same mechanism that just failed cannot report that it has stopped.

module.exports = {
  apps: [
    {
      // Real-time TradingView market data, a persistent WebSocket
      // connection, needs to just keep running.
      name: "tv-stream",
      script: "data_collector/market_data/collectors/stream_many.js",
      interpreter: "node",
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 20,
    },
    {
      // Collects raw articles (RSS feeds) into news_articles, its own
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
      // LLM classification of whatever news-stream has collected ,
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
      // Rebuilds the DERIVED:xxxUSD inverse FX rows from tv-stream's raw
      // USDxxx quotes (see generate_fx_inverses.py), was previously
      // only ever run once by hand and never scheduled, so every
      // currency using a DERIVED symbol (CAD, CHF, JPY, and now the
      // rest of the commodity-currency universe) would have silently
      // gone stale the same way the daily bars and news sync did.
      name: "fx-inverse-refresh",
      script: "scripts/refresh_fx_inverses.sh",
      interpreter: "bash",
      autorestart: false,
      cron_restart: "*/5 * * * *",
    },
    {
      // Rebuilds the pre-aggregated 1D bars market_data holds alongside
      // the raw 1-minute ticks (see historical_daily_backfill.js), the
      // dashboard's daily-close prices come from these, not the raw
      // feed, and they'd silently freeze without this running. Daily,
      // well after any tracked market's close.
      name: "daily-bars-refresh",
      script: "scripts/refresh_daily_bars.sh",
      interpreter: "bash",
      autorestart: false,
      cron_restart: "0 6 * * *",
    },
    {
      // UN Comtrade is monthly-cadence data, not real-time, this exits
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
      // Nightly pg_dump with rotation (see scripts/backup_database.sh).
      // The market data is not reproducible, TradingView only serves a
      // week or two of 1-minute history, so everything older than that
      // lives in this database and nowhere else. Runs at 03:30 UTC, well
      // clear of the 06:00 refresh jobs below.
      name: "db-backup",
      script: "scripts/backup_database.sh",
      interpreter: "bash",
      autorestart: false,
      cron_restart: "30 3 * * *",
    },
    {
      // The pipeline's read-only API, the dashboard's only way to reach
      // market_data/fundamental_trade_data/paper-trading state.
      name: "pipeline-api",
      script: "scripts/run_pipeline_api.sh",
      interpreter: "bash",
      autorestart: true,
      restart_delay: 3000,
    },
    {
      // The dashboard: market research and the Step 6 strategy monitor in
      // one app, split by a mode switch rather than by port. The monitor
      // was a separate Streamlit process until it was merged here.
      name: "dashboard",
      script: "dashboard/app.py",
      interpreter: ".venv/bin/python3",
      autorestart: true,
      restart_delay: 3000,
    },
  ],
};
