require('dotenv').config();

const fs = require('fs');
const path = require('path');

const Client = require('../core/tv_client');
const symbols = require('../config/symbols');
const pool = require('../../database/db');

const TIMEFRAME = '1';
const RANGE = 20000;
const TIMEOUT_MS = 30000;

const OUTPUT_DIR = path.join(__dirname, '..', 'output');
const SUMMARY_FILE = path.join(OUTPUT_DIR, 'historical_backfill_summary.csv');

if (!fs.existsSync(OUTPUT_DIR)) {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}

const allSymbols = [
  ...symbols.futures.map((symbol) => ({ symbol, asset_class: 'futures' })),
  ...symbols.fx.map((symbol) => ({ symbol, asset_class: 'fx' })),
];

function log(level, message) {
  console.log(`[${level}] ${new Date().toISOString()} ${message}`);
}

function csvEscape(value) {
  if (value === null || value === undefined) return '';
  const str = String(value);

  if (str.includes(',') || str.includes('"') || str.includes('\n')) {
    return `"${str.replace(/"/g, '""')}"`;
  }

  return str;
}

function writeSummary(rows) {
  const columns = [
    'symbol',
    'asset_class',
    'status',
    'bars',
    'earliest_datetime_utc',
    'latest_datetime_utc',
    'error',
  ];

  const lines = [
    columns.join(','),
    ...rows.map((row) => columns.map((col) => csvEscape(row[col])).join(',')),
  ];

  fs.writeFileSync(SUMMARY_FILE, `${lines.join('\n')}\n`);
}

async function upsertBars(symbol, assetClass, bars) {
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

  for (const bar of bars) {
    await pool.query(query, [
      symbol,
      assetClass,
      bar.time,
      new Date(bar.time * 1000).toISOString(),
      bar.open,
      bar.high,
      bar.low,
      bar.close,
      bar.volume,
      'tradingview',
      TIMEFRAME,
      new Date().toISOString(),
    ]);
  }
}

function fetchHistory(client, item) {
  return new Promise((resolve) => {
    const chart = new client.Session.Chart();
    let settled = false;

    const finish = (result) => {
      if (settled) return;
      settled = true;

      try {
        chart.delete();
      } catch (_) {}

      resolve(result);
    };

    const timer = setTimeout(() => {
      finish({
        ...item,
        status: 'timeout',
        bars: [],
        error: `No response within ${TIMEOUT_MS} ms`,
      });
    }, TIMEOUT_MS);

    chart.onUpdate(() => {
      const bars = chart.periods;

      if (!bars || bars.length === 0) return;

      clearTimeout(timer);

      finish({
        ...item,
        status: 'ok',
        bars,
        error: '',
      });
    });

    chart.onError((...err) => {
      clearTimeout(timer);

      finish({
        ...item,
        status: 'error',
        bars: [],
        error: err.map(String).join(' '),
      });
    });

    chart.setMarket(item.symbol, {
      timeframe: TIMEFRAME,
      range: RANGE,
    });
  });
}

async function main() {
  const client = new Client({
    token: process.env.TV_SESSION_ID,
    signature: process.env.TV_SESSION_SIGN,
  });

  const summary = [];

  log('START', `Backfilling ${allSymbols.length} symbols`);

  for (const [index, item] of allSymbols.entries()) {
    log('FETCH', `[${index + 1}/${allSymbols.length}] ${item.symbol}`);

    const result = await fetchHistory(client, item);

    if (result.status !== 'ok') {
      log('ERROR', `${item.symbol}: ${result.error}`);

      summary.push({
        symbol: item.symbol,
        asset_class: item.asset_class,
        status: result.status,
        bars: 0,
        earliest_datetime_utc: '',
        latest_datetime_utc: '',
        error: result.error,
      });

      writeSummary(summary);
      continue;
    }

    const newest = result.bars[0];
    const oldest = result.bars[result.bars.length - 1];

    await upsertBars(item.symbol, item.asset_class, result.bars);

    log(
      'SAVED',
      `${item.symbol} bars=${result.bars.length} earliest=${new Date(oldest.time * 1000).toISOString()}`
    );

    summary.push({
      symbol: item.symbol,
      asset_class: item.asset_class,
      status: 'ok',
      bars: result.bars.length,
      earliest_datetime_utc: new Date(oldest.time * 1000).toISOString(),
      latest_datetime_utc: new Date(newest.time * 1000).toISOString(),
      error: '',
    });

    writeSummary(summary);
  }

  client.end();
  await pool.end();

  log('DONE', `Summary saved to ${SUMMARY_FILE}`);
}

main().catch(async (err) => {
  console.error(err);
  await pool.end();
  process.exit(1);
});