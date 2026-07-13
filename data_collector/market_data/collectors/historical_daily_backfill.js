require('dotenv').config();

const fs = require('fs');
const path = require('path');

const Client = require('../core/tv_client');
const symbols = require('../config/symbols');
const pool = require('../../database/db');


const TIMEFRAME = '1D';
const RANGE = 20000;
const TIMEOUT_MS = 120000;

const START_TIMESTAMP = Math.floor(
  new Date('2010-01-01T00:00:00Z').getTime() / 1000
);

const OUTPUT_DIR = path.join(
  __dirname,
  '..',
  'output'
);

const SUMMARY_FILE = path.join(
  OUTPUT_DIR,
  'historical_daily_backfill_summary.csv'
);


if (!fs.existsSync(OUTPUT_DIR)) {
  fs.mkdirSync(
    OUTPUT_DIR,
    {
      recursive: true,
    }
  );
}


const configuredSymbols = [
  ...symbols.futures.map((symbol) => ({
    symbol,
    asset_class: 'futures',
  })),

  ...symbols.fx.map((symbol) => ({
    symbol,
    asset_class: 'fx',
  })),

  ...(symbols.proxies || []).map((symbol) => ({
    symbol,
    asset_class: 'etf_proxy',
  })),
];


const requestedSymbols = process.argv.slice(2);

const configuredNames = new Set(
  configuredSymbols.map(
    (item) => item.symbol
  )
);

const unknownSymbols = requestedSymbols.filter(
  (symbol) => !configuredNames.has(symbol)
);

if (unknownSymbols.length > 0) {
  throw new Error(
    `Unknown requested symbols: ${unknownSymbols.join(', ')}`
  );
}


const allSymbols =
  requestedSymbols.length === 0
    ? configuredSymbols
    : configuredSymbols.filter(
        (item) =>
          requestedSymbols.includes(item.symbol)
      );

if (allSymbols.length === 0) {
  throw new Error(
    'No symbols selected for historical daily backfill.'
  );
}


function log(level, message) {
  console.log(
    `[${level}] ${new Date().toISOString()} ${message}`
  );
}


function csvEscape(value) {
  if (
    value === null
    || value === undefined
  ) {
    return '';
  }

  const str = String(value);

  if (
    str.includes(',')
    || str.includes('"')
    || str.includes('\n')
  ) {
    return `"${str.replace(/"/g, '""')}"`;
  }

  return str;
}


function writeSummary(rows) {
  const columns = [
    'symbol',
    'asset_class',
    'timeframe',
    'status',
    'bars',
    'earliest_datetime_utc',
    'latest_datetime_utc',
    'error',
  ];

  const lines = [
    columns.join(','),

    ...rows.map(
      (row) =>
        columns
          .map(
            (column) =>
              csvEscape(row[column])
          )
          .join(',')
    ),
  ];

  fs.writeFileSync(
    SUMMARY_FILE,
    `${lines.join('\n')}\n`
  );
}


function normalizeBars(bars) {
  const uniqueBars = new Map();

  for (const bar of bars || []) {
    const timestamp = Number(bar.time);

    if (
      !Number.isFinite(timestamp)
      || timestamp < START_TIMESTAMP
    ) {
      continue;
    }

    if (
      !Number.isFinite(Number(bar.open))
      || !Number.isFinite(Number(bar.high))
      || !Number.isFinite(Number(bar.low))
      || !Number.isFinite(Number(bar.close))
    ) {
      continue;
    }

    uniqueBars.set(
      timestamp,
      {
        ...bar,
        time: timestamp,
      }
    );
  }

  return Array.from(uniqueBars.values())
    .sort(
      (first, second) =>
        second.time - first.time
    );
}


async function upsertBars(
  symbol,
  assetClass,
  bars
) {
  const client = await pool.connect();

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
    VALUES (
      $1,
      $2,
      $3,
      $4,
      $5,
      $6,
      $7,
      $8,
      $9,
      $10,
      $11,
      $12
    )
    ON CONFLICT (
      symbol,
      timeframe,
      timestamp
    )
    DO UPDATE SET
      asset_class = EXCLUDED.asset_class,
      datetime_utc = EXCLUDED.datetime_utc,
      open = EXCLUDED.open,
      high = EXCLUDED.high,
      low = EXCLUDED.low,
      close = EXCLUDED.close,
      volume = EXCLUDED.volume,
      provider = EXCLUDED.provider,
      received_at_utc = EXCLUDED.received_at_utc;
  `;

  const receivedAt = new Date().toISOString();

  try {
    await client.query('BEGIN');

    for (const bar of bars) {
      await client.query(
        query,
        [
          symbol,
          assetClass,
          bar.time,
          new Date(
            bar.time * 1000
          ).toISOString(),
          Number(bar.open),
          Number(bar.high),
          Number(bar.low),
          Number(bar.close),
          bar.volume === null
            || bar.volume === undefined
            ? null
            : Number(bar.volume),
          'tradingview',
          TIMEFRAME,
          receivedAt,
        ]
      );
    }

    await client.query('COMMIT');

  } catch (error) {
    await client.query('ROLLBACK');
    throw error;

  } finally {
    client.release();
  }
}


function fetchHistory(client, item) {
  return new Promise((resolve) => {
    const chart =
      new client.Session.Chart();

    let settled = false;

    const finish = (result) => {
      if (settled) {
        return;
      }

      settled = true;

      try {
        chart.delete();
      } catch (_) {
        // Nothing to clean up.
      }

      resolve(result);
    };


    const timer = setTimeout(
      () => {
        finish({
          ...item,
          status: 'timeout',
          bars: [],
          error:
            `No response within ${TIMEOUT_MS} ms`,
        });
      },
      TIMEOUT_MS
    );


    chart.onUpdate(() => {
      const bars = chart.periods;

      if (
        !bars
        || bars.length === 0
      ) {
        return;
      }

      clearTimeout(timer);

      finish({
        ...item,
        status: 'ok',
        bars,
        error: '',
      });
    });


    chart.onError((...errors) => {
      clearTimeout(timer);

      finish({
        ...item,
        status: 'error',
        bars: [],
        error: errors
          .map(String)
          .join(' '),
      });
    });


    chart.setMarket(
      item.symbol,
      {
        timeframe: TIMEFRAME,
        range: RANGE,
      }
    );
  });
}


async function main() {
  const client = new Client({
    token: process.env.TV_SESSION_ID,
    signature: process.env.TV_SESSION_SIGN,
  });

  const summary = [];

  log(
    'START',
    `Daily backfilling ${allSymbols.length} symbols: `
      + allSymbols
        .map((item) => item.symbol)
        .join(', ')
  );


  try {
    for (
      const [index, item]
      of allSymbols.entries()
    ) {
      log(
        'FETCH',
        `[${index + 1}/${allSymbols.length}] `
          + item.symbol
      );

      const result = await fetchHistory(
        client,
        item
      );


      if (result.status !== 'ok') {
        log(
          'ERROR',
          `${item.symbol}: ${result.error}`
        );

        summary.push({
          symbol: item.symbol,
          asset_class: item.asset_class,
          timeframe: TIMEFRAME,
          status: result.status,
          bars: 0,
          earliest_datetime_utc: '',
          latest_datetime_utc: '',
          error: result.error,
        });

        writeSummary(summary);
        continue;
      }


      const filteredBars =
        normalizeBars(result.bars);


      if (filteredBars.length === 0) {
        const error =
          'No valid daily bars found from 2010 onward';

        log(
          'ERROR',
          `${item.symbol}: ${error}`
        );

        summary.push({
          symbol: item.symbol,
          asset_class: item.asset_class,
          timeframe: TIMEFRAME,
          status: 'no_data',
          bars: 0,
          earliest_datetime_utc: '',
          latest_datetime_utc: '',
          error,
        });

        writeSummary(summary);
        continue;
      }


      const newest = filteredBars[0];

      const oldest =
        filteredBars[
          filteredBars.length - 1
        ];


      try {
        await upsertBars(
          item.symbol,
          item.asset_class,
          filteredBars
        );

      } catch (error) {
        log(
          'ERROR',
          `${item.symbol}: database insert failed: `
            + error.message
        );

        summary.push({
          symbol: item.symbol,
          asset_class: item.asset_class,
          timeframe: TIMEFRAME,
          status: 'database_error',
          bars: 0,
          earliest_datetime_utc: '',
          latest_datetime_utc: '',
          error: error.message,
        });

        writeSummary(summary);
        continue;
      }


      const earliestDatetime =
        new Date(
          oldest.time * 1000
        ).toISOString();

      const latestDatetime =
        new Date(
          newest.time * 1000
        ).toISOString();


      log(
        'SAVED',
        `${item.symbol} `
          + `timeframe=${TIMEFRAME} `
          + `bars=${filteredBars.length} `
          + `earliest=${earliestDatetime} `
          + `latest=${latestDatetime}`
      );


      summary.push({
        symbol: item.symbol,
        asset_class: item.asset_class,
        timeframe: TIMEFRAME,
        status: 'ok',
        bars: filteredBars.length,
        earliest_datetime_utc:
          earliestDatetime,
        latest_datetime_utc:
          latestDatetime,
        error: '',
      });

      writeSummary(summary);
    }

  } finally {
    try {
      client.end();
    } catch (_) {
      // Ignore shutdown errors.
    }

    await pool.end();
  }


  log(
    'DONE',
    `Summary saved to ${SUMMARY_FILE}`
  );
}


main().catch((error) => {
  console.error(error);
  process.exit(1);
});