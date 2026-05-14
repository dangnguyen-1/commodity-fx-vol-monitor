const { genSessionID } = require('./utils');

module.exports = (client) => class ChartSession {
  #chartSessionID = genSessionID('cs');
  #client = client;
  #periods = {};
  #infos = {};
  #seriesCreated = false;
  #currentSeries = 0;

  get periods() {
    return Object.values(this.#periods).sort((a, b) => b.time - a.time);
  }

  get infos() {
    return this.#infos;
  }

  #callbacks = {
    symbolLoaded: [],
    update: [],
    error: [],
  };

  #handleEvent(ev, ...data) {
    this.#callbacks[ev].forEach((cb) => cb(...data));
  }

  #handleError(...msgs) {
    if (this.#callbacks.error.length === 0) console.error(...msgs);
    else this.#handleEvent('error', ...msgs);
  }

  constructor() {
    this.#client.sessions[this.#chartSessionID] = {
      type: 'chart',
      onData: (packet) => {
        if (packet.type === 'symbol_resolved') {
          this.#infos = {
            series_id: packet.data[1],
            ...packet.data[2],
          };

          this.#handleEvent('symbolLoaded');
          return;
        }

        if (['timescale_update', 'du'].includes(packet.type)) {
          const changes = [];

          Object.keys(packet.data[1]).forEach((key) => {
            changes.push(key);

            if (key === '$prices') {
              const prices = packet.data[1].$prices;
              if (!prices || !prices.s) return;

              prices.s.forEach((p) => {
                this.#periods[p.v[0]] = {
                  time: p.v[0],
                  open: p.v[1],
                  high: p.v[2],
                  low: p.v[3],
                  close: p.v[4],
                  volume: p.v[5] ? Math.round(p.v[5] * 100) / 100 : 0,
                };
              });
            }
          });

          this.#handleEvent('update', changes);
          return;
        }

        if (packet.type === 'symbol_error') {
          this.#handleError(`(${packet.data[1]}) Symbol error:`, packet.data[2]);
          return;
        }

        if (packet.type === 'series_error') {
          this.#handleError('Series error:', packet.data[3]);
        }
      },
    };

    this.#client.send('chart_create_session', [this.#chartSessionID]);
  }

  setSeries(timeframe = '60', range = 100, reference = null) {
    if (!this.#currentSeries) {
      this.#handleError('Please set the market before setting series');
      return;
    }

    const calcRange = !reference ? range : ['bar_count', reference, range];

    this.#periods = {};

    this.#client.send(`${this.#seriesCreated ? 'modify' : 'create'}_series`, [
      this.#chartSessionID,
      '$prices',
      's1',
      `ser_${this.#currentSeries}`,
      timeframe,
      this.#seriesCreated ? '' : calcRange,
    ]);

    this.#seriesCreated = true;
  }

  setMarket(symbol, options = {}) {
    this.#periods = {};

    const symbolInit = {
      symbol,
      adjustment: options.adjustment || 'splits',
    };

    if (options.session) symbolInit.session = options.session;
    if (options.currency) symbolInit['currency-id'] = options.currency;
    if (options.backadjustment) symbolInit.backadjustment = 'default';

    this.#currentSeries += 1;

    this.#client.send('resolve_symbol', [
      this.#chartSessionID,
      `ser_${this.#currentSeries}`,
      `=${JSON.stringify(symbolInit)}`,
    ]);

    this.setSeries(options.timeframe || '60', options.range || 100, options.to);
  }

  setTimezone(timezone) {
    this.#periods = {};
    this.#client.send('switch_timezone', [this.#chartSessionID, timezone]);
  }

  fetchMore(number = 1) {
    this.#client.send('request_more_data', [this.#chartSessionID, '$prices', number]);
  }

  onSymbolLoaded(cb) {
    this.#callbacks.symbolLoaded.push(cb);
  }

  onUpdate(cb) {
    this.#callbacks.update.push(cb);
  }

  onError(cb) {
    this.#callbacks.error.push(cb);
  }

  delete() {
    this.#client.send('chart_delete_session', [this.#chartSessionID]);
    delete this.#client.sessions[this.#chartSessionID];
  }
};