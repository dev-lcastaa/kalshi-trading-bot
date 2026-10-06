# Phase 3 pre-registration: new information, tested in advance

Written and committed **before** any Phase 3 hypothesis was run. The commit history is the timestamp: if a
result in this repository contradicts a rule below, the rule wins and the result is reported as it came out.

## What we already know (so the tests are not fooled by it)

- The market price alone has no tradable edge (the market-only control placed 0 trades).
- The fair-value taker rule (frozen coefficients, `thr` 3c after fee, 4 to 14 minutes left) earned +0.95c per
  contract on the development splits (90% CI -1.2 to +2.9c) and has been negative since Oct 2.
- A test already run: replacing the index with Coinbase inside the z-score gave -0.5c. That is context for H1,
  not a result of this document.
- Maker (resting-order) execution of this signal fails (Phase 2a). All Phase 3 tests use **taker** execution.

## Common rules for every hypothesis

**Execution (frozen):** 1 contract, taker fill at the displayed ask (YES) or bid (NO) 2 seconds after the decision,
real fee `ceil(0.07 * P * (1 - P) * 100) / 100`, held to settlement unless the hypothesis says otherwise. Decisions
use only data available at the decision second. The rule is directional like the live bot: YES only when the
model probability is above 0.5, NO only when below.

**Stages.** Each hypothesis passes three gates in order, and a failure stops it.

| Stage | Data | Fitting allowed | Pass rule |
|---|---|---|---|
| 1. Screen | `val` + `test` (Sep 26 to Oct 5) | `train` (Sep 14 to 25) only | see per-hypothesis rules, plus the common screen below |
| 2. Validation | `fwd` (Oct 6 to 19) | frozen at the end of stage 1 (H4: first 7 days of `fwd`) | net mean > 0, day-clustered 90% CI lower bound > 0, at least 300 trades, positive in both halves of the period, placebo negative, still positive with a 5 s delay |
| 3. Confirmation | `holdout` (Oct 20 on) | frozen at the end of stage 2 | the stage 2 rule, evaluated once and logged |

**Common screen (stage 1):** pooled `val` + `test` net mean at least +0.5c per trade, at least 300 trades, mean above
zero in `val` and in `test` separately, and the placebo's pooled mean below zero.

Why this is loose at stage 1: ten days of data cannot confirm a 1c edge, so stage 1 only discards ideas that clearly
do nothing. Stages 2 and 3 are the evidence. With four hypotheses some stage 2 false positives are expected, so
stage 3 is the real guard.

**Reporting:** every run is appended to `registry.jsonl` in the event store (hypothesis, spec hash, data
fingerprint, code commit, splits, result). Any evaluation on `holdout` needs an explicit confirmation flag and is
logged as a look. Results that fail are kept and reported, not re-run with different settings.

**Stop rule:** if no hypothesis has passed stage 2 by **Oct 27 2026**, trading development stops and the collector
keeps running. If one passes stage 3, the next step is paper trading, not live money.

## H1: cross-exchange lead-lag

**Idea.** BRTI is built from several exchanges, and Kalshi's quote follows BRTI with a delay. A constituent exchange
that has already moved tells us where BRTI is heading.

**Spec.**
1. Basis `b(t) = ln(exchange price) - ln(BRTI)` on a 1 second grid, both sides no older than 5 s.
2. Measurement (per coin, fit on `train`): OLS of `ln(BRTI(t+10)/BRTI(t))` on `[b(t), ln(BRTI(t)/BRTI(t-5))]`.
   Report coefficients, day-clustered t statistics and out-of-sample R^2 against a zero forecast on `val` and `test`.
   Gate to the trading test: out-of-sample R^2 > 0 in `val` and in `test`, for both coins.
3. Trading test: adjusted level `L(t) = BRTI(t) * exp(beta_b * b(t))` with `beta_b` the `train` coefficient of `b`,
   used in place of BRTI in the z-score. Frozen fair-value coefficients; 3c after-fee rule, up to 5 buys 60 s apart.
4. Placebo: the basis from 3,600 seconds earlier.

**Stage 1 data:** Coinbase only (the only exchange with full legacy coverage).
**H1b (stage 2 only):** consensus basis, the mean over Coinbase, Kraken and Bitstamp of the sources that are fresh,
with `beta_b` fit on the first 7 days of `fwd`, evaluated on the last 7.

## H2: a faster volatility estimate

**Idea.** The z-score divides by a flat 30 minute volatility. Volatility clusters, so recent minutes should weigh more.

**Spec.** Same 30 one-minute returns, weights `0.5^(k/8)` for a return `k` minutes old (half-life 8 minutes), weighted RMS
in place of the flat RMS. Nothing else changes. No time-of-day term (three weeks cannot support it).
1. Information test (independent of the market): Brier score and log loss of the pure model probability
   `Phi(z)` for every minute with 4 to 14 minutes left, new against old, with a paired day-clustered CI. Gate:
   the new estimate is better on `val` and on `test`.
2. Trading test: frozen fair-value coefficients with the new z; 3c after-fee rule, up to 5 buys 60 s apart.
3. Placebo: z shuffled across markets of the same coin.

## H3: the final-minute settlement average

**Idea.** A market settles on the average of the last 60 one-second index values. By 30 seconds before close half of
that average is already known, which narrows the outcome sharply. The live model switches off inside the last minute.

**Spec.** At second `t` with `r = close - t - 1` seconds still to come and `k = 60 - r` already observed:
`S ~ Normal(mean = (sum_observed + r * X(t)) / 60, variance = sigma_p^2 * r(r+1)(2r+1)/6 / 3600)`
with `sigma_p = X(t) * sigma_1min / sqrt(60)` (the baseline 30 minute volatility), `P(YES) = P(S >= strike)`, clipped to
[0.005, 0.995]. A decision needs all `k` observed seconds to be present. Decision window: 10 to 55 seconds before
close. Rule: buy when the model probability beats the executable price by at least **2c** after the fee (the
ceiling on the edge at extreme prices is only a few cents, so 3c would almost never fire), one entry per market, the
first qualifying second. Pure model probability, no blending with the market price.
Placebo: probabilities shuffled across markets of the same coin.

## H4: order-book depth and trade flow (needs the collector's data)

**Idea.** Imbalance in the book and one-sided aggressive trading predict the next move of the quote.

**Features** at second `t` for a market: depth imbalance over the top 5 levels
`(sum yes bids - sum no bids) / (sum of both)` from the latest depth snapshot (no older than 5 s) and signed taker
flow over the trailing 30 s `sign(F) * ln(1 + |F|)` where `F` is the sum of contracts with the taker on YES minus
the taker on NO.
**Target:** `mid(t+10) - mid(t)`.
**Model:** OLS of the target on the two features, pooled over both coins, fit on the first 7 days of `fwd` using
seconds with 4 to 14 minutes left and a valid quote.
**Trading test (stage 2, last 7 days of `fwd`):** enter in the direction of the prediction when it falls in the top
5% of `|prediction|` (cut-off from the fit days), taker, 2 s delay; exit 10 s later by crossing back; both fees paid;
at most one trade per 30 s per market. Placebo: features shuffled across markets of the same coin.
There is no stage 1 for H4 because the data does not exist before Oct 6.

## What this document does not allow

- Changing a parameter after seeing a result in the same stage.
- Adding a variant to a hypothesis that failed. A new idea is a new hypothesis, written here first.
- Looking at `holdout` outcomes for any reason other than a confirmation run.

---

# Addendum, 2026-10-06 evening: H5 and H6 (from the external spec)

Written and committed **before** H5 or H6 was implemented or run. The source is `KALSHI_15M_TRADING_BOT_SPEC.md`
(another agent's handoff spec) and its answers to our questions, which conceded that its 8% edge threshold, its
momentum and acceleration signals, and the independence of its confirmations are all unvalidated. The rules above
(common rules, stages, registry, stop rule) apply unchanged unless stated here.

## Settling the cost semantics first

At 20 to 80c prices a round trip costs a median **5c** (1c spread, about 2c fee on each side). Therefore
`net = gross - 5c`, and a *net* stop of -5c is a gross move of about zero: it would fire at entry. The spec's
"2 to 5c net target with 3 to 5c net stop" is therefore not a coherent configuration at these prices. The coherent
form, which the spec's author also recommended, is gross price moves with costs charged in the P/L:

| | Setting (frozen) |
|---|---|
| Take profit | the executable exit price is at least **+0.08** above the entry price (about +3c net) |
| Stop | the executable exit price is at least **0.08** below the entry price |

## H5: the spec's trading shell around the frozen model

**Model.** The frozen fair-value probability (distance to strike, volatility, time left, anchored to the market
price). It is undefined inside the last 60 seconds, so the decision window is 60 to 840 seconds before close.

**A candidate is rejected unless all of these hold** (the spec's validator, with numbers fixed here):

| Rule | Value |
|---|---|
| Edge | model probability of the side minus its executable price (YES: ask, NO: 1 - bid) at least **0.08**, the better of the two sides |
| Entry price | 0.20 to 0.80 |
| Spread | at most 0.03 |
| Liquidity | at least 1 contract at the touch |
| Data freshness | Kalshi quote no older than **1000 ms**; index within the existing 5 s validity |
| Chop | fewer than **3** crossings of the strike by the index in the last 60 seconds |
| Position | none open for this coin |
| Cooldown | 20 s since the previous exit |
| Consecutive losses | fewer than 3 in a row (resets at 00:00 UTC) |
| Daily loss | the coin's net P/L today above -0.25 (resets at 00:00 UTC) |

**Execution.** Decision at second `t`; the order arrives at `t + 2 s` and is **re-validated** with the data of that
second (edge, price band, spread, liquidity, freshness); if it fails, the entry is cancelled. Fills at the ask (YES) or
`1 - bid` (NO), real taker fee. One contract.

**Exits**, checked every second after the fill against the price that could be sold at right now, in this order:
take profit (+0.08), stop (-0.08), **invalidation** (the model probability of our side is below the executable exit
price; only while the model is defined, i.e. 60 s or more before close). A triggered exit executes 2 s later at the
bid, with the real fee, and may fill worse than the trigger. A position still open at close settles at its outcome.

**Universe.** Both coins (the shell is coin-agnostic and one coin cannot reach the sample sizes below); each coin
has its own position, cooldown and loss counters, and BTC alone (the spec's stated scope) is reported next to it.

**Stage 1 screen** (the 300-trade rule cannot apply: a selective strategy trades about 10 times a day, so the rules
are sized to it). All must hold: at least 100 trades on all development days, at least 40 trades pooled in `val` and
`test`, pooled `val` + `test` net mean at least **+0.5c**, `val` and `test` each above zero with at least 15 trades,
and the placebo (model probabilities shuffled across markets of the same coin) negative. Also reported, not gated:
profit factor, average win and loss, maximum drawdown, exit-reason counts, and the rejection-reason histogram.

**Honest limit, stated in advance.** At this frequency 300 trades take about 5 weeks. Stage 2 on the 14-day
`fwd` window cannot reach its trade-count rule, so a stage 1 pass would not be confirmable by the Oct 27 stop date; the
owner would decide then whether to extend it. No extension is pre-committed here.

## H6: do momentum, acceleration and the order book add anything? (the spec's nested test, collapsed)

The spec proposed nested models A (distance + volatility + time), B (+ momentum), C (+ acceleration), D (+ book). To
limit the number of tests, H6 tests **D against A** in one step; only a pass would justify the B and C ablations,
which would be registered separately.

**Model D.** A ridge logistic regression (L2 = 1, no tuning) fit on `train` only, at the minute marks inside the
decision window, of the outcome on:
`x0 = logit(p_A)`; `x1 = ln(V_t / V_{t-60}) / sigma_1min` (momentum); `x2 = (ln(V_t / V_{t-30}) - ln(V_{t-30} / V_{t-60})) / sigma_1min`
(acceleration); `x3 = (bid size - ask size) / (bid size + ask size)` at the touch (book imbalance).
Features need a valid index at `t`, `t - 30`, `t - 60`.

**Test.** The H5 shell with `p_D` in place of `p_A`, the same screen, and the same placebo. Also reported, not gated:
held-out log loss of `p_D` against `p_A` on `val` + `test`.

**Independence.** Reported, not assumed: the fitted coefficients and the change in held-out log loss when each of
`x1`, `x2`, `x3` is removed (refit on `train`).

---

# Addendum 2, 2026-10-06 late: H7 and H8 (principled new directions)

Written and committed **before** H7 or H8 was implemented or run, after H1 to H3, H5 and H6 all failed. Neither is a
repair of a failed idea: both come from structure we have measured. The rules above (common rules, stages, registry,
holdout, stop rule) apply unchanged unless stated here. To limit false positives, **at most two candidates may enter
the holdout look**, chosen by the stage rules and nothing else.

## H7: favorite-longshot bias at extreme prices

**Idea.** The fee is quadratic, so near 90c it is about 1c per side against about 2c near 50c. Early research
(v0.8.0) found that favorites win slightly more often than their price implies (Platt slope 1.11). If that bias
exceeds the small cost at extreme prices, buying the favorite and holding to settlement has a positive expectation.
This is a calibration effect of the price, not new information, so it does not repeat H1 to H6.

**Spec (frozen).**
1. Recalibrated probability `P(yes) = sigmoid(a + b * logit(mid))`, a ridge-free logistic fit of the outcome on the
   market mid at the minute marks (60 to 840 s before close), both coins pooled, **fit on `train` only**.
2. Candidate side: the side whose executable price (YES: the ask, NO: 1 - bid) is **at least 0.85** and below 1.00.
3. Enter when `P(side) - price - fee(price) >= 0` (a non-negative after-fee edge, the v0.8.0 rule), at least 1
   contract at the touch, decision window 60 to 840 s before close, **one entry per market** (the first qualifying
   second), order arrives 2 s later and fills at the displayed price, real fee, held to settlement.
4. Placebo: recalibrated probabilities shuffled across markets of the same coin.

**Stage 1 screen:** the common screen (pooled `val` + `test` mean at least +0.5c, at least 300 trades, `val` and
`test` each above zero, placebo negative). Stage 2 on `fwd` and stage 3 on `holdout` as in the common rules.
Reported, not gated: a calibration table (price bin against realised win rate) above 0.85, and the same rule without
the 0.85 restriction (the original v0.8.0 form).

## H8: thinner markets are less efficient

**Idea.** The BTC series trades about 2.2M contracts a day, the others 8k to 100k. If liquidity drives efficiency, the
slow-quote edge that the fair-value rule looks for should be larger in thin markets. SOL (about 40 times thinner than
BTC) looked better than BTC in an earlier replay, but that was found after the fact and is not evidence. H8 tests the
idea on markets that have never been analysed.

**Universe (frozen).** The seven series ETH, XRP, DOGE, BNB, ZEC, NEAR and HYPE (`KXETH15M` and so on; all quadratic
fee, multiplier 1, settled on CF Benchmarks with an index named `<COIN>USD_RTI`). ADA, BCH and TON are excluded
(not discoverable by the bot's tag-based discovery, or no index stream). **No development data exists for these
markets**, so there is no stage 1; the first data seen is the validation window.

**Windows.** `D0` is the first full UTC day after alt markets first appear in the event store (recorded in the
registry at the first run). **Stage 2** is `D0` to `D0 + 13`; **stage 3** is `D0 + 14` to `D0 + 27`, evaluated once.

**Test (frozen).** Exactly rule A: the frozen fair-value coefficients (no refit, no per-coin change), 3c after-fee
edge, 4 to 14 minutes left, up to 5 buys 60 s apart on the same side, 2 s delay, held to settlement, real fees. The
alts pooled. Pass rule: the stage 2 rule of the common rules (mean above 0, day-clustered 90% CI lower bound above 0,
at least 300 trades, positive in both halves, shuffled-z placebo negative, still positive with a 5 s delay), and the
same again on stage 3.

**Reported, not gated:** each coin's net mean; the same rule on BTC and SOL over the same days; the rank correlation
across coins between net mean and traded volume per market.
