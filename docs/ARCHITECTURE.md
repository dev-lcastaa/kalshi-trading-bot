# AQLabs architecture: an edge-proving machine with an automated executor

Status: Phase 1 in progress (see [Migration plan](#migration-plan)). This document replaces
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
| `market_ticks` | `market_ticker, ts_ms, price_dollars, yes_bid_dollars, yes_ask_dollars, yes_bid_size, yes_ask_size, volume, open_interest` |
| `index_ticks` | `index_id, ts_ms, value` |
| `external_ticks` | `source, symbol, index_id, ts_ms, received_at_ms, price, bid, ask, volume_24h` |
| `markets` | `ticker, index_id, strike, close_ts_ms, status, result, ...` (dimension table, replaced on each import) |
| `decisions` | the bot's recorded decisions (for audit only; never a training input) |

Planned additions for Phase 3: `orderbook_levels` (depth) and `trades` (full tape, not only $100+).

## Phase 1 status

| Item | State |
|---|---|
| Event store (Parquet + DuckDB), importer CLI, manifest fingerprints | Done. `python -m aqlabs.store.cli import/report`. 10.7M rows imported, 108 MB on disk. Row counts match the exports exactly. |
| Data-quality report (gaps, uptime, null/crossed/bound quotes) | Done. Reproduces the audit: about 13.8 hours of index gaps, Kraken BTC 0 ticks. |
| Shared fee module (`aqlabs.costs`) | Done. Tested against the live bot's fee function at every cent. |
| Replay engine moved to `aqlabs.research.replay`, standard suite | Done. `python -m aqlabs.research.suite`. **Parity gate passed: all 48 result lines (trades, means, totals, win rates, t-stats, CIs) identical to the original replay.** Runs in about 30 seconds. Holdout (Oct 6+) is excluded. |
| Kraken BTC feed bug | Fixed in `kalshi_bot/external_prices.py` (`XBT/USD` is rejected by Kraken's v2 API, `BTC/USD` works). **Needs a redeploy to take effect.** |
| Server storage | About 130 GB of the 233 GB disk is unallocated LVM space. Extending `/` needs `sudo` (owner action, see below). |
| Collector service (1b) | Not started. Spec below. |

### Server disk (owner action, needs sudo)

```bash
sudo vgs && sudo lvs                                     # confirm free space in ubuntu-vg
sudo lvextend -r -L +100G /dev/ubuntu-vg/ubuntu-lv        # grows the LV and the ext4 filesystem online
df -h /
```

### Collector spec (Phase 1b)

- Separate process and container from the bot, so trading load can never drop ticks.
- Sources: Kalshi quotes (top of book now; depth and the full trade tape added), the Kalshi index feed,
  Coinbase, Kraken (BTC/USD, SOL/USD), and at least one more exchange.
- Every tick stores the exchange timestamp and the local receive timestamp.
- Writes through `EventStore.append` in small batches (every few seconds), plus a heartbeat row per feed.
- Alerts when any feed is silent for more than 30 seconds. Target at least 99% uptime.
- Retention: raw Parquet kept indefinitely (about 5 MB per day compressed); nightly copy to a second disk.
- Gate: 2 weeks of gap-free collection, then the Phase 2 fill-model work uses it.

## Migration plan

| Phase | Work | Gate to continue |
|---|---|---|
| 0 | Turn off scalping, keep live paused, free disk, rotate the SSH key. | Done by the owner. |
| 1 | Event store, importer, data-quality checks, fee module, replay engine moved to `aqlabs/research`, Kraken BTC fix. Collector spec and build (1b). | Replay reproduces the existing results exactly. 2+ weeks of gap-free collection. |
| 2 | Maker/taker fill model in `costs/`, then re-run all strategies. | Net positive after realistic costs on development data. **If not, stop.** |
| 3 | New features and models, hypotheses pre-registered in the registry. | Beats the frozen baseline on validation, then the holdout once. |
| 4 | Strategy and executor on the shared interface. Paper-sim runs the replay code. | Paper P/L matches the replay prediction within its confidence interval for 2 to 3 weeks. |
| 5 | Live at $1 to $5 per trade with the risk gate and decay monitor. | Real fills match paper. Scale only slowly. |
| 6 | Remove legacy code (shadow models, isotonic calibrator, v1 to v3, scalper). | Tests green. |

### Evidence gates (every strategy)

- Net P/L per contract after fees, spread, a realistic delay (at least 5 to 10 seconds) and 1c slippage.
- Positive on validation and test, and the lower end of the day-clustered 90% CI above zero, with at least 300 to 500 trades.
- Positive in both coins and both halves, not driven by one day.
- Placebo tests (stale and shuffled signal) must lose.
- The holdout (data from Oct 6 2026 onward) is evaluated once per frozen candidate, and every look is logged.

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
