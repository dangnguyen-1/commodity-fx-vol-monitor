const Client = require('../core/tv_client');

const client = new Client({
  DEBUG: false,
});

const chart = new client.Session.Chart();

chart.onSymbolLoaded(() => {
  console.log('Loaded:', chart.infos.description);
});

chart.onUpdate(() => {
  if (!chart.periods[0]) return;

  const bar = chart.periods[0];

  console.log({
    symbol: chart.infos.pro_name,
    time: new Date(bar.time * 1000),
    open: bar.open,
    high: bar.high,
    low: bar.low,
    close: bar.close,
    volume: bar.volume,
  });
});

chart.onError((...err) => {
  console.error('Chart error:', ...err);
});

chart.setMarket('COMEX:GC1!', {
  timeframe: '60',
  range: 100,
});