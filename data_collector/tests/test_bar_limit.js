require('dotenv').config();

const Client = require('../core/tv_client');

const client = new Client({
  token: process.env.TV_SESSION_ID,
  signature: process.env.TV_SESSION_SIGN,
});

const chart = new client.Session.Chart();

chart.onSymbolLoaded(() => {
  console.log('Loaded:', chart.infos.description);
});

chart.onUpdate(() => {
  const bars = chart.periods;

  if (!bars.length) return;

  console.log('Number of bars:', bars.length);
  console.log('Newest bar:', bars[0]);
  console.log('Oldest bar:', bars[bars.length - 1]);

  client.end();
});

chart.onError((...err) => {
  console.error('Chart error:', ...err);
});

chart.setMarket('COMEX:GC1!', {
  timeframe: 'D',
  range: 50000,
});