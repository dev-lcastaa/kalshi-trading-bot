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
