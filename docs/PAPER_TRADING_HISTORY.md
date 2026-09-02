# The intraday paper-trading system, and why it was removed

Written on 2 September 2026, when `paper_trading/` and `strategy/` were
deleted from the working tree.

**The code is not lost.** Everything described here exists at commit
`5394d6f` and every commit before it. To read it:

```
git show 5394d6f:paper_trading/execution/run_paper_execution.py
git checkout 5394d6f -- paper_trading/ strategy/   # to restore it entirely
```

That was 92 Python files and about 40,500 lines: 14,793 in `paper_trading/`
and 25,716 in `strategy/`.

**The engine's data is archived too.** Its SQLite database, holding 5 runs
and 15,510 feature snapshots, is at
`backups/paper_trading_final_20260902.db.gz` on the VM (7.1 MB compressed,
WAL checkpointed so the file is self-contained). That is the measurement
data behind every number below. `scripts/pull_backups.sh` copies it down
with the rest.

---

## What it was

An intraday strategy trading FX against commodity moves. The thesis: a
commodity move should transmit to its exporter's currency, and when the
currency has not yet moved as much as the commodity implies, that gap
("divergence") predicts the currency catching up.

The machinery, roughly in order of the data flowing through it:

- **Feature engine.** Multi-horizon commodity returns (15m, 60m, 240m),
  volatility-normalised with a `sqrt(horizon/60)` scaling, combined with a
  measured `transmission_beta` per relationship into an expected FX impulse,
  compared against the observed one to produce a divergence score.
- **Signal engine.** Two modes. Confirmed required corroborating classified
  news and directional agreement; Divergence required neither.
- **Execution engine.** Position lifecycle, stop and target, maximum holding
  time, thin-liquidity blackout, and session-close enforcement against
  venue calendars derived empirically from bar coverage.
- **Orchestrator.** Features and signals on a 5-minute cadence, execution on
  1 minute, with execution deliberately still running when the upstream
  stages failed.
- **Spec pinning.** The whole strategy lived in one YAML file whose SHA-256
  was stored on the run. Any edit, including whitespace, ended the run
  rather than silently changing the rules mid-flight.
- **Monitoring.** Heartbeat staleness per stage, data staleness, unresolved
  alerts, disk, and OpenAI credit, delivered to a Discord webhook.

Universe: 30 relationships across 27 commodities.

---

## What was measured, and what it said

This is the part worth keeping. The system worked; the strategy did not.

**The divergence signal had negative edge at every horizon and threshold
tested.** Worst in the tail, which is the opposite of what a real signal
does: the 60-minute top decile came in at -4.37 bps, t = -3.06. Imposing a
realistic 11-minute fill delay left it at t = -2.73. There was never a
threshold at which it turned positive.

**Between 90% and 99% of the measured commodity-to-FX transmission was the
dollar factor.** Stripping the common cross-sectional dollar move left
silver retaining 1.3% of its beta, copper 0.6%, gold 2.2%. The betas the
strategy traded on were mostly measuring the dollar, not the commodity. Gold
"transmitted" to EURUSD at 0.544 because gold is priced in USD, not because
Europe exports gold.

**The one exception pointed at the exit.** Oil to NZDCAD retained 57%
ex-dollar, and daily data agreed (oil to CAD held 92-97%). That is a
*cross*, not a USD pair, which is what eventually reframed the whole
project.

**The universe was the root cause.** All 30 relationships traded through
just five instruments: EURUSD, GBPUSD, AUDUSD, USDCAD, BRLUSD. Every one a
USD pair, chosen because those are what you can trade intraday. So the
strategy was structurally a lagged dollar view, expressed against
instruments that price the dollar instantly. The effective sample size was
far below 30.

**The data could never have supported a backtest.** One-minute FX history
amounted to about 10 days for the majors and 4 for the crosses, against 180
days of commodity minutes. There is no news history to backtest Confirmed
mode against at all, because news collection began when the pipeline did.
Waiting longer would not have fixed this on any useful horizon.

---

## Bugs worth remembering

These were found by running it, and several are the reason the engineering
half was worth doing even though the strategy failed. Each is the same
shape: state that looked correct because a key or a deadline omitted part of
its own input.

- **`maximum_holding_time` had never once fired.** The deadline was computed
  from the current bar rather than from `opened_at`, so "now plus the limit"
  moved forward with every cycle and never arrived. Found in production on a
  position held 450 minutes against a 240-minute cap.
- **Session-close enforcement had the same defect.** Anchoring the deadline
  to "now" meant that once the market had closed, the next close was
  tomorrow's, so an overdue position stopped looking overdue and quietly
  survived the night.
- **The orchestrator wrote `status = "unhealthy"`**, which the schema's
  CHECK constraint rejected. The heartbeat was therefore lost precisely when
  the system was in trouble.
- **The signals stage crashed on unmeasured relationships**, calling
  `finite_number()` on a NULL expected impulse. It hid for a day because it
  needed a complete snapshot for a relationship with no beta, then failed
  every cycle for five hours once coverage improved.
- **pm2 silently stopped firing a cron timer.** `market-sync` ran fine for a
  day, then stopped, leaving the engine's SQLite copy three hours behind a
  perfectly healthy Postgres. The process stayed listed and the daemon
  stayed green. Both that job and the watchdog moved to the system crontab.
- **A staleness alert blamed the wrong component.** It always pointed at the
  TradingView session, so when the *sync* stopped it sent someone to check
  credentials that were working. Alerts now compare upstream against
  downstream and name the side that actually stopped.

---

## Why it was removed rather than fixed

The strategy was not failing because of a bug. It was failing because the
premise, as implemented, was mostly a dollar trade on a 10 to 11 minute
delayed feed, and because the instruments that made it tradeable intraday
were the ones that destroyed the signal.

Fixing that means changing the frequency and the universe, which is not a
fix, it is a different strategy. Meanwhile the research side had 16.7 years
of daily history across 28 commodities and 40 FX pairs sitting unused,
including every cross the revised thesis wants and the commodity-exporter
currencies the intraday version could never touch.

So the project became a research project on daily data, and the
paper-trading engine was removed rather than left rotting in the tree. It
will be rebuilt when there is something worth trading.

---

## What was kept

- `api/` — the read-only API, reduced from 17 routes to 4. `/news/latest`
  was rewritten to read Postgres directly rather than the engine's SQLite.
- `monitoring/health_watchdog.py` — rewritten against Postgres. The
  heartbeat and alert checks went with the engine; the data-freshness,
  OpenAI and disk checks stayed.
- Everything under `data_collector/` and `dashboard/`.

## What went with it

`market-sync` (the SQLite bar sync), `sync_news.sh` (news copied into
SQLite), the `strategy-orchestrator` process, the paper-trading SQLite
database, the strategy spec and its measurement plan, the nine paper-trading
tests, and the eleven research scripts under `strategy/research/`.

Several of those research scripts produced the findings above and are worth
reading before rebuilding anything, particularly `cross_transmission.py`,
which is what established the dollar-factor result, and `divergence_edge.py`,
which measured the negative edge with a realistic fill.
