require('dotenv').config();

const fs = require('fs');
const path = require('path');

const Client = require('../core/tv_client');
const symbols = require('../config/symbols');
const pool = require('../database/db');

const OUTPUT_DIR = path.join(__dirname, '..', 'output');
const OUTPUT_FILE = path.join(OUTPUT_DIR, 'market_data.jsonl');

const TIMEFRAME = '1';
const INITIAL_RANGE = 100;
const RECONNECT_DELAY_MS = 5000;

if (!fs.existsSync(OUTPUT_DIR)) {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}

const allSymbols = [
  ...symbols.futures.map((symbol) => ({ symbol, asset_class: 'futures' })),
  ...symbols.fx.map((symbol) => ({ symbol, asset_class: 'fx' })),
];

const lastRows = new Map();

function log(level, message) {
  console.log(`[${level}] ${new Date().toISOString()} ${message}`);
}

function writeBarToJsonl(row) {
  fs.appendFileSync(OUTPUT_FILE, `${JSON.stringify(row)}\n`);
}

function rowChanged(row) {
  const key = `${row.symbol}|${row.timeframe}|${row.timestamp}`;
  const previous = lastRows.get(key);

  if (!previous) {
    lastRows.set(key, row);
    return true;
  }

  const changed =
    previous.open !== row.open ||
    previous.high !== row.high ||
    previous.low !== row.low ||
    previous.close !== row.close ||
    previous.volume !== row.volume;

  if (changed) {
    lastRows.set(key, row);
  }

  return changed;
}

async function insertBarToPostgres(row) {
  const query = `
    INSERT INTO market_data (
      symbol,
      asset_class,
      timestamp,
      datetime_utc,
      open,
      high,
      low,
      close,
      volume,
      provider,
      timeframe,
      received_at_utc
    )
    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
    ON CONFLICT (symbol, timeframe, timestamp)
    DO UPDATE SET
      open = EXCLUDED.open,
      high = EXCLUDED.high,
      low = EXCLUDED.low,
      close = EXCLUDED.close,
      volume = EXCLUDED.volume,
      received_at_utc = EXCLUDED.received_at_utc
  `;

  const values = [
    row.symbol,
    row.asset_class,
    row.timestamp,
    row.datetime_utc,
    row.open,
    row.high,
    row.low,
    row.close,
    row.volume,
    row.provider,
    row.timeframe,
    row.received_at_utc,
  ];

  await pool.query(query, values);
}

function startCollector() {
  const client = new Client({
    token: process.env.TV_SESSION_ID,
    signature: process.env.TV_SESSION_SIGN,
  });

  client.onConnected(() => {
    log('CONNECTED', 'TradingView websocket connected');
  });

  client.onDisconnected(() => {
    log('DISCONNECTED', `Websocket disconnected. Reconnecting in ${RECONNECT_DELAY_MS / 1000}s`);

    setTimeout(() => {
      startCollector();
    }, RECONNECT_DELAY_MS);
  });

  client.onError((...err) => {
    log('CLIENT_ERROR', err.map(String).join(' '));
  });

  allSymbols.forEach(({ symbol, asset_class }) => {
    const chart = new client.Session.Chart();

    chart.onSymbolLoaded(() => {
      log('LOADED', `${asset_class} ${symbol} - ${chart.infos.description}`);
    });

    chart.onUpdate(async () => {
      if (!chart.periods[0]) return;

      const bar = chart.periods[0];

      const row = {
        symbol,
        asset_class,
        timestamp: bar.time,
        datetime_utc: new Date(bar.time * 1000).toISOString(),
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
        volume: bar.volume,
        provider: 'tradingview',
        timeframe: TIMEFRAME,
        received_at_utc: new Date().toISOString(),
      };

      writeBarToJsonl(row);

      if (!rowChanged(row)) {
        log('SKIP', `${symbol} ${row.datetime_utc} unchanged`);
        return;
      }

      try {
        await insertBarToPostgres(row);
        log('UPSERT', `${symbol} ${row.datetime_utc} close=${row.close} volume=${row.volume}`);
      } catch (err) {
        log('DB_ERROR', `${symbol}: ${err.message}`);
      }
    });

    chart.onError((...err) => {
      log('CHART_ERROR', `${symbol}: ${err.map(String).join(' ')}`);
    });

    chart.setMarket(symbol, {
      timeframe: TIMEFRAME,
      range: INITIAL_RANGE,
    });
  });
}

process.on('SIGINT', async () => {
  log('SHUTDOWN', 'Stopping collector');
  await pool.end();
  process.exit(0);
});

startCollector();