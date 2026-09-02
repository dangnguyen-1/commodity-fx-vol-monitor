# Commodity-FX Volatility Monitor

Tracks how commodity price moves relate to the currencies of the countries
that trade them. A copper move should matter to the Chilean peso, an oil
move to the Norwegian krone, and the opposite way round for an importer.

Live at **https://commodity-fx-vol-monitor.duckdns.org**

18 commodities against 20 currencies in 78 pairs, with daily price history
from January 2010. Pairs come from measured net exports in UN Comtrade
rather than from correlation, so a currency appears against a commodity
because its country genuinely leads in trading it.

## Layout

```
data_collector/   collectors: TradingView prices, UN Comtrade flows, news
api/              read-only FastAPI over Postgres, bound to localhost
dashboard/        Dash application, ten tabs
monitoring/       health checks and the usage report
scripts/          service entry points and operational tasks
ecosystem.config.js   pm2 process definitions
```

Data flows one way: collectors write to Postgres, the API reads it, the
dashboard calls the API server-side. The dashboard never touches the
database directly and the API is never exposed publicly.

## Data sources

| source | provides |
|---|---|
| TradingView | daily and one-minute bars for every instrument |
| UN Comtrade | monthly exports and imports, 39 reporters, 26 commodities |
| Reuters, Bloomberg, Investing.com | news headlines over RSS |
| OpenAI | classifies each headline into affected assets and direction |
| World Bank | GDP and governance indicators |

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # then fill in DATABASE_URL and any keys

.venv/bin/python3 -m uvicorn api.app:app --port 8000    # API
cd dashboard && python3 app.py                          # dashboard on :8050
```

The dashboard runs without a database or network for UI work:

```bash
DATA_SOURCE=mock python3 app.py
```

`DATA_SOURCE` selects `pipeline` (the API, default), `yahoo`, or `mock`.

## Deployment

A single VM runs everything under pm2, with Caddy terminating TLS in front
of the dashboard. `scripts/setup_cloud_vm.sh` provisions a host from
scratch: system packages, Postgres, the schema, the firewall and pm2.

```bash
pm2 start ecosystem.config.js     # collectors, API, dashboard
pm2 status
```

`monitoring/health_watchdog.py` runs every five minutes from cron and
alerts to a webhook when a collector stops producing rows. It checks
freshness in the database rather than whether a process is up, because the
failure worth catching is a collector that keeps running and quietly stops
writing.

## Notes

- `dashboard/DESIGN.md` documents the visual system: one amber accent
  reserved for chrome and alerts, semantic green and red for direction,
  blue for informational data, mono for every figure.
- News classification is capped at 1,000 API calls per UTC day.
- Futures prices arrive on a 10 to 11 minute delay under the current data
  entitlement. Spot FX is real time.
