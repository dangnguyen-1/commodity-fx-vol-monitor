# Discovery run: what will be measured

Written **before** the run starts. That is the entire point of it.

Two months of live paper trading is the only evidence available: there is no
historical news data to backtest against, because news history begins when
the pipeline started running. So discovery and collection happen at the same
time, which is not ideal and is unavoidable.

The risk that creates is specific. If the questions are chosen after seeing
the data, something will always look significant, because across enough
cuts something always does. Deciding the questions now is what separates a
result from a coincidence.

---

## What this run is, and is not

**It is a discovery run.** The output is a hypothesis about whether the
strategy has an edge, and where.

**It is not a validation run.** Anything found here has been found on the
same data used to look, so it is in-sample by construction. Confirming it
needs a later period that was not used to find it. Realistically this is a
four-month arc, not two.

The distinction matters most in October, when a positive result will be
tempting to treat as a verdict.

---

## The spec is frozen for the duration

`validate_run_spec` compares the run's stored SHA-256 of the entire spec
file against the current one. **Any** edit to
`intraday_strategy_spec.yaml` -- a threshold, a comment, trailing
whitespace -- changes that hash, and the execution engine then refuses to
run with `"was created from a different strategy specification"`.

So the rule is not "avoid version bumps". It is:

> Do not edit the spec file during the run.

Code changes are fine and do not affect the hash. If the spec genuinely must
change (a real bug in a rule, not a preference), that ends the run and
starts a new one, and the clock restarts with it. Record why.

Run identity for this period:

```
run_id:  commodity_fx_intraday-live_paper-v0.3.2
spec:    v0.3.2, sha256 pinned at run creation
started: (fill in when the clock starts)
```

---

## Primary questions

Each is answered separately for **Confirmed** and **Divergence** mode. They
are different signals -- Confirmed requires corroborating news and
directional agreement, Divergence requires neither -- and pooling them would
let one hide inside the other.

### Q1. Does the signal have an edge, net of costs?

**Metric:** mean of `sign(divergence) x forward_return` per trade, in basis
points, minus the 4.0 bps round-trip cost budget.

**Method:** `strategy/research/divergence_edge.py --entry-delay-minutes 11`

The entry delay is not optional. Exchange futures arrive on a measured
10-11 minute delay, so a fill at the feature timestamp is not achievable and
measuring one flatters the strategy with a trade nobody could have made.

**Decision rule, fixed now:**

| net edge | reading |
|---|---|
| > +1.0 bps with t > 2 | worth a real validation run |
| between -1 and +1 bps | no edge established; do not promote |
| < -1.0 bps with t < -2 | evidence against; stop or reformulate |

### Q2. Does Confirmed mode differ from Divergence mode?

The open question. Divergence mode already has evidence against it from the
July replay: negative at every measurable horizon and threshold, worst in
the tail, t = -2.73 on the top 5% at a realistic fill. Confirmed mode has
never been tested at all, because that replay window contained zero news.

**Metric:** Q1's net edge, computed separately per mode, with the difference
and its standard error.

If Confirmed shows an edge and Divergence does not, the candidate worth
keeping is Confirmed-only, and Step 9's trade minimum has to be reconsidered
because news is sparse.

### Q3. Is the trade population dominated by relationships that do not
transmit?

Measured in the July replay: Crude Oil/CAD had the **highest** median
divergence in the book while transmitting at beta 0.054, because a
relationship with no expected term contributes raw FX noise as "divergence".
A global threshold selects hardest for exactly the relationships that work
least.

**Metric:** trade count and net edge per relationship, against its measured
`transmission_beta`.

**What would falsify the concern:** trades spread roughly evenly across
relationships, or the low-beta ones performing no worse.

---

## Secondary questions

Recorded so they are not discovered later and presented as findings.

- **S1.** Do exits behave as intended? Distribution of `exit_reason`, and net
  P&L by reason. `maximum_holding_time` had never once fired before it was
  fixed; the others have never been observed at scale.
- **S2.** Does session-close enforcement fire, and does it cost anything?
  Count of `session_close` exits and their P&L against other exits.
- **S3.** Which relationships accumulate enough complete features to measure
  a `transmission_beta` for the first time? Wheat, Cattle, Corn and Soybeans
  are collecting impulses now without trading.
- **S4.** How often does coverage block a relationship that would otherwise
  have traded?

---

## Holdout

**The final two weeks are not used for any fitting.**

Thresholds, universe decisions and beta estimates are derived from weeks 1-6
only. Week 7-8 data is then scored once, using parameters chosen without
seeing it.

This costs a quarter of the sample and is worth it: it is the only thing
that distinguishes a real result from one fitted to the period it was found
in. If the holdout disagrees with weeks 1-6, the finding does not
generalise, and that is the answer.

---

## The classifier is part of the setup

News classification stays on **gpt-5.5** for the whole run, at roughly $44 a
month.

Cheaper models were tested on real articles with the production prompt.
gpt-5.4-mini and gpt-5.4-nano cost $5 and $1.20 a month respectively, a 90
to 97% saving, but agreed with gpt-5.5 on only 73% of asset and direction
pairs, and mini returned twice as many impacts across the sample. For a
signal that fires on news agreement, flagging twice as much is a change to
the strategy rather than a saving on it.

More to the point: this run exists to find out whether Confirmed mode works.
Changing the classifier partway would make any result a statement about the
new classifier instead, with no way to tell the two apart. The saving is
worth revisiting once it is known whether the signal is worth running at
all.

## What will not be done

**Inverting the signal because the sign looks consistent.** The negative
edge has been consistent across horizons, which is tempting. Acting on it
would be fitting a direction to the only data available and is the classic
route to a strategy that backtests beautifully and loses money live.

**Tuning thresholds until the edge turns positive.** The thresholds derived
so far (entry ~1.70, exit ~0.30) come from percentiles of the observed
distribution, not from what makes returns look best. That distinction is the
whole discipline.

**Cutting the universe mid-run.** All 30 relationships stay active. Twelve
can trade, the rest collect the data needed to measure them at review. A
narrower book would shrink the sample precisely when it needs to grow.

---

## At the end

1. Run Q1-Q3 and S1-S4 on weeks 1-6
2. Fix thresholds, universe and betas from that, and write them down
3. Score weeks 7-8 once, without adjustment
4. Decide: promote to a real validation run, reformulate, or stop

Nothing in step 2 may be revised after seeing step 3.
