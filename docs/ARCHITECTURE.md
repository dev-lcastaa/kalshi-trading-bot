# AQLabs architecture: an edge-proving machine with an automated executor

Status: Phase 1 built and verified; collecting for the 2-week gate (see [Migration plan](#migration-plan)). This document replaces
"predict up or down" as the app's goal.

## Why this redesign

A replay of 3 weeks of tick data (4,078 markets, Sep 14 to Oct 5 2026, real fees, taker fills at
the displayed ask or bid) showed:

| Finding | Evidence |
|---|---|
| The market price is already about as accurate as our models | Model Brier 0.1447 vs market 0.1348 on 3,950 clean decisions. A market-only control model never clears the fee. |
| The fair-value signal exists but is too small | Frozen repo coefficients: +0.95c per contract net (90% CI -1.2c to +2.9c), gross +2.6c vs 1.7c average fees. Placebos (60 s stale coin price, shuffled z) lose 2 to 2.5c. |
| The edge faded | By week: +3.5c, +1.5c, +1.9c, then -2.7c (Oct 2 to 6). Test split -3.05c. |
| The README's early edge did not replicate | The README tick replay (+11c) covers about Sep 12 to 13, which overlaps the 30-day fit window. Replaying that window gives +6.2c (t = 1.5). |
| Momentum scalping loses to costs | -2.8c to -3.9c per trade in every split (t from -12 to -55). A 88% win rate with a 2c target still loses. |
| Data collection had holes | About 14 hours of index gaps in 3 weeks, Kraken BTC never produced a tick (wrong symbol), disk 93% full. |

Conclusion: we do not have a trading edge that survives fees. The goal of the app is now to
**find out cheaply and honestly whether one exists, and to trade it automatically only if it does.**
Accuracy of direction is not a goal. Expected net value per contract is.

## Principles

1. **The target is expected net value per contract.** A model outputs a calibrated probability per
   side. The strategy compares it to the executable price after fees, spread and fill odds.
2. **One code path.** Replay, paper and live run the same strategy and execution code against
   different "exchanges" (historical, simulated, real).
3. **Costs are first-class.** Fees, spread, fill odds and latency live in one module (`costs/`).
   Every report splits P/L into gross signal and cost.
4. **Nothing is promoted without a frozen model, a locked holdout and a gate.**
5. **Raw data is immutable.** Features and labels are derived from it and can always be rebuilt.

## Target system

```
 Exchanges/Feeds --> [1 Collector] --> [2 Event Store (immutable)]
                                          |
             +----------------------------+-------------------------+
             v                            v                         v
     [3 Research Lab]            [4 Strategy Engine]         [7 Monitoring]
  replay, features, train       pure functions:              data quality, edge
  evaluate, registry            state -> order intent        decay, P/L split
             | promotes frozen          |                            ^
             +---> model artifacts -----+                            |
                                        v                            |
                                 [5 Risk Gate] --> [6 Executor] -----+
                                limits, kill switch   live | paper-sim | replay-sim
```

| # | Component | Responsibility |
|---|---|---|
| 1 | Collector | Separate process. Kalshi quotes, depth, trades; the index; Coinbase, Kraken and more. Stores exchange and receive timestamps. Heartbeats and gap alerts. Cannot be blocked by trading. |
| 2 | Event store | Append-only Parquet (partitioned by table and UTC date), queried with DuckDB. Manifest with row counts and content fingerprints. |
| 3 | Research lab | Replay engine, feature pipeline, walk-forward training, experiment registry, holdout lock, standard reports. |
| 4 | Strategy engine | Pure functions `(state, features, position) -> order intent or abstain`. Models are frozen, versioned artifacts. No unattended refitting. |
| 5 | Risk gate | Position, exposure and daily loss limits, kill switches, automatic halt when live results diverge from the replay prediction. |
| 6 | Executor | One order state machine, idempotent submission, reconciliation, maker and taker policies. Same interface for live, paper-sim and replay-sim. |
| 7 | Monitoring | Feed health, rolling net P/L vs predicted, fill quality, P/L split into signal and cost. |

## How the design answers each finding

| Finding | Design response |
|---|---|
| Market beats our models | Models predict only the residual against the market. No standalone outcome predictor. |
| Scalping loses to costs | Removed. Any scalp must pass replay with the shared fee and fill model first. |
| Gross signal +2.6c, costs 3 to 4c | Cost is the first design variable: maker orders, price-bucket selection (fees are quadratic), explicit fill model. |
| The edge faded | Edge-decay monitor, forward holdout, preset kill criteria. |
| Early README edge did not replicate | Frozen splits, pre-registered hypotheses, experiment registry. |
| Shadow and logistic models went nowhere | Retired in Phase 6. One challenger path with the same ledger and gates as the champion. |
| Feed gaps, Kraken BTC outage | Dedicated collector, gap alerts, multiple independent price sources. |
| Paper, live and research logic diverged | One strategy and executor interface. |

## Repository layout (target)

```
src/aqlabs/
  store/        event schema, Parquet store, importer, data-quality checks     (Phase 1)
  costs/        fee schedule, fill and slippage models (single source of truth) (Phase 1, 2)
  research/     replay engine, walk-forward, registry, reports, holdout lock    (Phase 1, 3)
  collector/    feeds, normalization, heartbeat, writers                        (Phase 1b)
  features/     settlement-average, volatility, z-score, lead-lag               (Phase 3)
  models/       frozen artifacts, loaders, calibration                          (Phase 3)
  strategies/   pure-function strategies                                        (Phase 4)
  execution/    order state machine, live/paper/replay exchanges                (Phase 4)
  risk/         limits, kill switches, decay monitor                            (Phase 5)
src/kalshi_bot/  legacy bot: stays untouched until the new path is proven
```

`aqlabs` lives beside `kalshi_bot` (strangler pattern). Nothing in the running bot changes until a
phase gate passes.

## Event store schema (Phase 1)

Layout: `<root>/<table>/date=YYYY-MM-DD/part-*.parquet`. Dates are UTC and derived from `ts_ms`
(`close_ts_ms` for `markets`). Writers add new part files atomically and never rewrite old ones.

| Table | Key columns |
|---|---|
| `market_ticks` | `market_ticker, ts_ms, price_dollars, yes_bid/ask_dollars, yes_bid/ask_size, volume, open_interest, received_at_ms` |
| `index_ticks` | `index_id, ts_ms, value, exchange_ts_ms, received_at_ms, avg_60s` (`avg_60s` is Kalshi's own running 60 s average, the quantity markets settle on) |
| `external_ticks` | `source, symbol, index_id, ts_ms, received_at_ms, price, bid, ask, volume_24h` (Coinbase, Kraken, Bitstamp) |
| `trades` | `trade_id, market_ticker, ts_ms, received_at_ms, yes/no_price_dollars, count, taker_side, is_block_trade` (every public fill) |
| `orderbook_depth` | `market_ticker, ts_ms, seq, yes_bids, no_bids, yes_total, no_total` (1 Hz, top 10 levels as JSON, rebuilt from the delta stream) |
| `market_meta` | `ticker, index_id, strike, close_ts_ms, observed_ms, status, result, expiration_value` (append-only; a row on discovery and another when the result is known) |
| `markets` | legacy dimension table from the Postgres export, replaced on each import |
| `heartbeats` | `feed, ts_ms, events, silent_ms, note` (about once a minute per feed, plus sequence-gap events) |

The columns marked with `received_at_ms`, `exchange_ts_ms` and `avg_60s` are new. Legacy data reads back
as NULL for them. Replay reads labels from `markets` and `market_meta` together.

## Phase 1 status: built and verified, waiting on deploy and the 2-week collection window

| Item | State |
|---|---|
| Event store (Parquet + DuckDB), importer CLI, manifest fingerprints | Done. `python -m aqlabs.store.cli import/report`. 10.7M legacy rows imported, 108 MB. `--since-ms` fills a gap without duplicates. |
| Data-quality report (gaps, uptime, null/crossed/bound quotes) | Done. Reproduces the audit: about 13.8 hours of index gaps, Kraken BTC 0 ticks. |
| Shared fee module (`aqlabs.costs`) | Done. Tested against the live bot's fee function at every cent. |
| Replay engine in `aqlabs.research`, standard suite | Done. `python -m aqlabs.research.suite`. **Parity gate passed twice: all 48 result lines identical to the original replay, also after the schema changes.** About 30 seconds per run. The holdout (Oct 6+) is always excluded. |
| Kraken BTC feed bug | Fixed (`XBT/USD` is rejected by Kraken's v2 API, `BTC/USD` works). Verified live. Ships with the next deploy. |
| **Collector service** | **Done and tested live.** `aqlabs.collector`, own image (`Dockerfile.collector`) and compose service `collector` with the named volume `aqlabs-eventstore`. |
| Server storage | Extended by the owner: 197 GB total, 102 GB free. |

### What the collector records

- Kalshi: quotes, the **full trade tape**, **1 Hz order-book depth** (book rebuilt from snapshot and delta
  messages; at quote arrival its best bid matches Kalshi's own quote 92% of the time, the misses are
  sub-cent timing noise), the CF Benchmarks index with its 60 s average, market metadata and settlement results.
- Coinbase, Kraken (BTC/USD, SOL/USD) and Bitstamp, at most 4 ticks per symbol per second
  (`COLLECTOR_EXTERNAL_MIN_INTERVAL_MS`, 0 keeps everything).
- Both the exchange timestamp and our receive timestamp for every tick. The server clock is NTP-synchronized.
- It opens its own Kalshi WebSocket connection with the bot's read-only data credentials; it never places orders.

### Reliability design

- Rotating the market set opens the new Kalshi connection and confirms it before closing the old one.
  Duplicates are removed by per-stream dedupe, and each connection owns its own book.
- Sequence gaps on the order-book channel invalidate the book and trigger a fresh connection.
- Exchange sockets reconnect with backoff, and an idle socket (no messages for 60 s) is torn down and rebuilt.
- Writes are buffered and flushed every 30 s in a worker thread. A failed write keeps the rows and retries.
  If the buffer ever fills, rows are dropped and counted, never blocking a feed. A crash loses at most 30 s.
- Per-feed silence limits raise one alert per outage and one on recovery (log, plus
  `COLLECTOR_ALERT_WEBHOOK` if set). `collector_status.json` is refreshed every 10 s and drives the Docker healthcheck.
- Finished UTC days are compacted into one file per table per day (row counts verified before old files are deleted).
- Low-disk warning below `COLLECTOR_MIN_FREE_GB` (default 10).

### Operating it

Deploys: the Jenkins pipeline rebuilds and restarts only `bot` and `frontend`, and leaves the collector (and
Postgres) running, because restarting the collector leaves an ~80 s gap in every feed and the data gate counts
gaps. Tick the **DEPLOY_COLLECTOR** build parameter only when the collector's own code changed. The collector
image bundles `src/`, so the report command run *inside* the container uses the code from its last deploy.

```bash
docker compose up -d --build collector                                  # start or update by hand
docker exec aqlabs-kalshi-trading-bot-collector-1 python -m aqlabs.store.cli report --root /data/eventstore
docker logs --tail 50 aqlabs-kalshi-trading-bot-collector-1             # alerts show as ERROR lines
# copy the data to a research machine
docker run --rm -v aqlabs-eventstore:/d -v "$PWD":/out alpine tar czf /out/eventstore.tgz -C /d .
```

The report judges each feed against its own gap limit (`FEED_GAP_LIMIT_SEC` in `aqlabs/store/quality.py`, the same
numbers the collector alerts on): 15 s for the index, 30 s Coinbase, 60 s / 120 s Kraken BTC / SOL, and 120 s / 600 s
Bitstamp BTC / SOL, which only ticks when someone trades. `--max-gap-sec N` applies one limit to every feed.
A planned restart shows up as one gap in every feed at the same time; that is not a feed outage.

Expected volume: about 170 MB per day before compaction (measured: 4.4 MB for the first 38 minutes; the trade
tape is the largest table), so roughly 60 GB a year; the 99 GB free is enough for the 2-week gate and months beyond it.
The legacy Postgres tables stop being the research source once the collector has run; backfill the gap
between the last legacy export and the collector's first row with `import --since-ms <largest max_ms in
MANIFEST.json>` from a fresh Postgres export.

### The Phase 1 gate

2 weeks of collection with no feed down for more than its limit and no dropped rows, checked with
`python -m aqlabs.store.cli report` (uptime per feed) and the `heartbeats` table. Planned restarts are
excluded from the count but should be rare (see "Operating it"). Until then Phase 2
(the fill model) can be developed on the 3 weeks of legacy data plus the new trade tape and depth as they accumulate.
## Phase 2a result: maker execution of the fair-value signal fails the kill check

Run with `python -m aqlabs.research.maker_suite` on the 3 weeks of legacy data (4,078 markets, Sep 14 to Oct 5,
holdout excluded). The headline spec was fixed before any result was seen: frozen fair-value signal, 3c edge over the
limit price with no fee, a limit order that joins the touch and rests 60 s, 2 s order delay, held to settlement,
maker fee 0 (the series metadata for KXBTC15M / KXSOL15M lists `fee_type: quadratic`, `fee_multiplier: 1`, i.e. no
maker fee; unverified against a real fill). Fills are decided from the recorded trade and quote stream under three
rules that bound queue position (`aqlabs/costs/fills.py`): optimistic (first in the queue), queue (wait behind the
displayed size) and pessimistic (only a trade strictly through our price counts).

| Per decision, cents | Optimistic | Queue | Pessimistic |
|---|---|---|---|
| Fill rate | 84% | 78% | 73% |
| Train + validation | -0.24 | | |
| Test | -1.16 | -1.63 | -2.25 |
| All days | -0.40 | -1.03 | -1.45 (90% CI -2.5 to -0.4) |

For comparison, crossing the spread on the same decisions is -1.46c, and the best taker spec (A) is +0.95c.

- **Pre-declared kill check failed:** even the optimistic bound is negative on development data
  (-0.24c). A second pre-declared variant, pulling the order when the edge over the limit falls below 1c, changed
  nothing (-0.21c).
- **Adverse selection is the cause.** Decisions that fill would have earned -3.3c to -4.7c had we crossed the
  spread, and decisions that never fill would have earned +5c to +8c. When the signal is right the quote runs away
  from a resting order; the fills come from price reversals. Cancelling on the signal does not remove it.
- Robust to every sensitivity (threshold 1 to 5c, rest 30 to 120 s, 5 s delay, maker fee 25% and 100% of the taker
  formula). The 5c threshold is the only positive cell, and only under the optimistic rule (+0.26c). The placebos
  (stale or shuffled signal) are negative.
- **Scope of the conclusion:** this kills *signal-driven, join-the-touch, hold-to-settlement* maker execution of
  this signal. It does not test two-sided market making, quoting deeper than the touch, or other signals. The
  collector's trade tape and depth can test those, but nothing here suggests they would work, and market making
  adds inventory risk.
- A $1 resting order on the live market would confirm whether the maker fee is really zero, but with this result
  that check is no longer urgent.

Implication for the roadmap: the fee is not the removable cost it first looked like, because the orders that
avoid the fee are the ones that get adversely selected. The remaining plan is Phase 3 (new information) with the
taker execution already measured.

## Migration plan

| Phase | Work | Gate to continue |
|---|---|---|
| 0 | Turn off scalping, keep live paused, free disk, rotate the SSH key. | Done by the owner. |
| 1 | Event store, importer, data-quality checks, fee module, replay engine moved to `aqlabs/research`, Kraken BTC fix, collector service. **Built and verified.** | Replay reproduces the existing results exactly (passed). 2+ weeks of gap-free collection (starts when the collector is deployed). |
| 2 | Maker/taker fill model in `costs/`, then re-run all strategies. **2a done on legacy data: maker execution of the fair-value signal fails the kill check (see above).** | Net positive after realistic costs on development data. **If not, stop.** |
| 3 | New features and models, hypotheses pre-registered in the registry. | Beats the frozen baseline on validation, then the holdout once. |
| 4 | Strategy and executor on the shared interface. Paper-sim runs the replay code. | Paper P/L matches the replay prediction within its confidence interval for 2 to 3 weeks. |
| 5 | Live at $1 to $5 per trade with the risk gate and decay monitor. | Real fills match paper. Scale only slowly. |
| 6 | Remove legacy code (shadow models, isotonic calibrator, v1 to v3, scalper). | Tests green. |

### Evidence gates (every strategy)

- Net P/L per contract after fees, spread, a realistic delay (at least 5 to 10 seconds) and 1c slippage.
- Positive on validation and test, and the lower end of the day-clustered 90% CI above zero, with at least 300 to 500 trades.
- Positive in both coins and both halves, not driven by one day.
- Placebo tests (stale and shuffled signal) must lose.
- The holdout (data from Oct 20 2026 onward) is evaluated once per frozen candidate, and every look is logged.

### Data splits

| Split | Dates (UTC) | Status |
|---|---|---|
| `train` | Sep 14 to 25 | Development |
| `val` | Sep 26 to Oct 1 | Development |
| `test` | Oct 2 to 5 | **Development.** Phases 1 and 2 looked at it repeatedly, so it is not a clean test any more. |
| `fwd` | Oct 6 to 19 | Development, with the collector's trade tape, depth and published 60 s average. Used by Phase 3; not by the Phase 1/2 suites. |
| `holdout` | Oct 20 onward | Untouched. One evaluation per frozen candidate, every look logged. |

The holdout started at Oct 6 until the Phase 3 decision, when it was moved to Oct 20. No outcome from Oct 6 or later
had been scored at that point, so no information leaked across the change.

### Kill criteria (decided up front)

- Phase 2 fails: stop. Public data and our latency cannot beat costs in this market.
- Phase 3 fails on the holdout: stop adding variants.
- Phase 4 paper falls short of replay predictions: fix the assumptions, not the strategy.
- Phase 5 loses more than the preset budget, or the edge-decay monitor fires: halt automatically.

## Success measures

| Area | Today | Target |
|---|---|---|
| Time to test a strategy idea | Days of live waiting | Under 10 minutes on 3+ weeks of data |
| Feed uptime | About 14 hours of gaps in 3 weeks | At least 99%, alert within 1 minute |
| Replay, paper and live parity | Three code paths | One path |
| Edge visibility | Accuracy and Brier score | Net P/L per contract with CIs, split into signal and cost |

## Honest expectation

The most likely good outcome is a small, selective edge of one to two cents per contract that is
fragile and needs monitoring. This architecture cannot guarantee profit. It makes finding, proving
and killing a strategy cheap, and it limits the cost of being wrong.
