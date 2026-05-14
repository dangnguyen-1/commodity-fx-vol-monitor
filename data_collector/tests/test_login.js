require('dotenv').config();

const Client = require('../core/tv_client');

const client = new Client({
  token: process.env.TV_SESSION_ID,
  signature: process.env.TV_SESSION_SIGN,
});

client.onConnected(() => {
  console.log('Connected');
});

client.onLogged((data) => {
  console.log('Logged in');
  console.log(data);
});

const chart = new client.Session.Chart();

chart.onSymbolLoaded(() => {
  console.log('Loaded:', chart.infos.description);
});

chart.onUpdate(() => {
  if (!chart.periods[0]) return;

  console.log(chart.periods[0]);
});

chart.onError((...err) => {
  console.error(err);
});

chart.setMarket('COMEX:GC1!', {
  timeframe: '60',
});