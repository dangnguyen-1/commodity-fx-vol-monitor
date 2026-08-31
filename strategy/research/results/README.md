# Research results — 31 August 2026

Output from the three analyses run against `v0.2.0`. Committed because the
tools are reproducible but re-running them costs about twenty minutes, and
these numbers are the input to decisions that are still open.

## Files

| file | produced by |
|---|---|
| `daily_screen_20260831.csv` | `daily_relationship_screen.py` — 16 years of daily bars, all 30 active relationships |
| `intraday_beta_20260831.txt` | `intraday_beta.py` — 8,340 feature snapshots replayed over 2026-07-13..17 |

## What they found

**The spec's `relationship_direction: ±1` is wrong for every relationship.**
Measured at the trading horizon, the transmission coefficient has a median of
**0.244** and a maximum of **0.664**. The `expected_fx_impulse` term has been
inflated by roughly 4× typically, and by 1.5× even for the strongest pair.

**Metals transmit better intraday than daily; oil/CAD barely transmits at
all.** This was the surprise, and it runs against the Epps effect, which
predicts the opposite:

| relationship | intraday β | intraday R² | daily β |
|---|---|---|---|
| Gold / AUD | 0.664 | 0.229 | 0.312 |
| Gold / EUR | 0.544 | 0.154 | 0.267 |
| Copper / AUD | 0.533 | 0.189 | 0.423 |
| Silver / EUR | 0.427 | 0.103 | 0.252 |
| Platinum / EUR | 0.369 | 0.099 | 0.250 |
| Gasoline / CAD | 0.127 | 0.011 | 0.308 |
| Brent / CAD | 0.065 | 0.0025 | 0.358 |
| Crude Oil / CAD | 0.054 | 0.0016 | 0.329 |

The CAD energy complex looked like four of the six best relationships on
daily evidence and is indistinguishable from noise at five minutes — R² of
0.0016 from 1,142 observations for Crude. Oil presumably takes hours to price
into CAD, which an intraday strategy cannot capture.

**Roughly 17 of 30 relationships show no daily relationship at all** — R²
under 0.001 with sign flips on half of all non-overlapping windows. Wheat/EUR
flips 28 times out of 55, which is a coin toss.

## Thresholds are badly miscalibrated after the v0.2.0 fix

From the same replay (`abs_divergence`: p50 0.727, p90 1.756, p95 2.104,
p99 2.886):

| gate | current | fires on |
|---|---|---|
| divergence entry | ≥ 1.00 | **34.7%** of evaluation points |
| confirmed entry | ≥ 0.75 | **48.6%** |
| convergence exit | < 0.25 | 18.4% |

An entry gate firing on a third to a half of all evaluations is not a gate.
Top-5% selectivity would need ≈ 2.1, top-1% ≈ 2.9. Left unchanged on purpose
— see `threshold_calibration_status` in the spec — because they must be
re-derived *after* the beta and universe decisions, not before, or the work
gets done twice.

## Caveats that matter

- The intraday sample is **five days in one regime** (13–17 July), covering
  14 of 30 relationships, with per-relationship n from 246 to 1,143. Enough to
  establish level, not stability. Whether beta needs to be *dynamic* still
  needs months of history, which is now accumulating.
- `commodity_impulse` is a 15/60/240-minute blend regressed on a 15-minute FX
  return, so it is not a clean horizon-matched coefficient. It *is* exactly
  the quantity the strategy compares, which is what makes it the right number
  for setting `relationship_direction` — but a 15m-on-15m measurement is
  worth running as a robustness check before committing.
- Coverage has not yet been profiled across a full day. The July window holds
  five complete trading days and can supply this; `coverage_profile.py` needs
  a date-range option to read it.

## One bug these caught

The registry stores each relationship's *source* quote, and seven trade its
inverse (`FX:USDCAD` stored, `DERIVED:CADUSD` traded). The daily screen
regressed against the stored symbol, so every CAD relationship came out with
the opposite sign. Only visible because the intraday figures come from
feature snapshots, which already use the traded symbol — the two disagreed by
exactly a sign on exactly the seven inverse relationships. Fixed in
`c230504`; these results are post-fix.


---

# Second session — 31 August 2026 (afternoon)

Output in `edge_analysis_20260831.txt`, produced under spec **v0.3.0** from a
replay of 13–17 July (9,541 complete snapshots, 5 relationships with
|beta| >= 0.35).

## The headline: the premise does not hold in this sample

`sign(divergence) * forward return`, before costs, against a 4 bps
round-trip budget:

| horizon | threshold | trades | mean bps | t | hit % |
|---|---|---|---|---|---|
| 15m | all | 1581 | −0.15 | −1.47 | 50.4 |
| 15m | top 5% | 80 | −0.52 | −1.02 | 50.0 |
| 60m | all | 391 | −0.86 | −2.10 | 44.8 |
| **60m** | **top 10%** | **40** | **−4.37** | **−3.06** | **30.0** |
| 240m | all | 95 | −1.53 | −0.96 | 43.2 |

Not one cell positive, and the tail is worse than the bulk — the top decile
at 60 minutes loses 4.4 bps gross with t = −3.06 and a 30% hit rate. That is
the exact population the strategy is built to trade.

Both escape routes are closed: it is not a horizon problem, and it is not a
tail problem.

## Supporting findings

**Divergence has no forward predictive power.** Pooled beta +0.011,
R² 0.0001, t = +0.41 on independent samples. Signs are inconsistent across
all five relationships, every |t| below 0.35.

**No return horizon predicts forward FX positively.** 60m is significantly
*negative* (t = −3.49), 15m is not significant. This closes the question of
re-weighting `market_impulse_weights` — reweighting cannot fix a set of
terms none of which point the right way.

**Contemporaneously, the 15m term carries everything.** R² 0.167–0.324
against 0.0000–0.015 for the 240m term the spec weights at 20%. Implied
weights 0.87/0.07/0.07 versus the spec's 0.50/0.30/0.20.

## Scope — read this before acting on any of the above

**This tests Divergence mode only.** The replay window contained **zero**
news articles, so `news_impulse` was 0 for all 9,541 snapshots and
`expected = beta × commodity_impulse` throughout. Not one Confirmed-mode
signal could have been generated.

**Confirmed mode remains completely untested** and cannot be tested
historically, because news history only begins when the pipeline started
running. It requires news to corroborate the commodity move, which may
select a different population. It can only ever be evaluated live.

Five days, one regime. The 240-minute panel has 95 observations and its tail
rows are too thin to read. Fixed-horizon holds were modelled; real trades
exit on volatility stops, convergence and reversals, which good exits could
improve on — though exits rarely turn a negative gross edge positive.

## What this justifies

Not freezing a version and starting the Step 9 clock. An eight-week run
committed now would most likely spend two months confirming a negative edge,
and rule changes are forbidden mid-period.

It does **not** justify inverting the signal because the negative sign looks
consistent. That would be fitting a direction to five days of data.

The responsible next step is more evidence: re-run `divergence_edge.py` once
the live pipeline has produced a larger sample, with news flowing so
Confirmed mode can finally be evaluated. Both depend on `feature_engine`
reaching healthy — no complete features, no new evidence.
