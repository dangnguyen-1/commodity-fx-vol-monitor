require('dotenv').config();

const fs = require('fs');
const path = require('path');

const Client = require('../core/tv_client');
const symbols = require('../config/symbols');
const pool = require('../database/db');

const OUTPUT_DIR = path.join(__dirname, '..', 'output');
const OUTPUT_FILE = path.join(OUTPUT_DIR, 'market_data.jsonl');

if (!fs.existsSync(OUTPUT_DIR)) {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}

const client = new Client({
  token: process.env.TV_SESSION_ID,
  signature: process.env.TV_SESSION_SIGN,
});

const allSymbols = [
  ...symbols.futures.map((symbol) => ({ symbol, asset_class: 'futures' })),
  ...symbols.fx.map((symbol) => ({ symbol, asset_class: 'fx' })),
];

function writeBarToJsonl(row) {
  fs.appendFileSync(OUTPUT_FILE, `${JSON.stringify(row)}\n`);
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

client.onConnected(() => {
  console.log('Connected to TradingView websocket');
});

client.onError((...err) => {
  console.error('Client error:', ...err);
});

allSymbols.forEach(({ symbol, asset_class }) => {
  const chart = new client.Session.Chart();

  chart.onSymbolLoaded(() => {
    console.log(`Loaded ${asset_class}: ${symbol} - ${chart.infos.description}`);
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
      timeframe: '1',
      received_at_utc: new Date().toISOString(),
    };

    console.log(row);

    writeBarToJsonl(row);

    try {
      await insertBarToPostgres(row);
    } catch (err) {
      console.error(`Postgres insert error for ${symbol}:`, err.message);
    }
  });

  chart.onError((...err) => {
    console.error(`Chart error for ${symbol}:`, err);
  });

  chart.setMarket(symbol, {
    timeframe: '1',
    range: 100,
  });
});