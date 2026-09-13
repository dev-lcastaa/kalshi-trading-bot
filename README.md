# AQLabs — Kalshi 15-Minute Crypto Signal

A bot that watches Kalshi's 15-minute Bitcoin and Solana "above or below"
markets and tells you what it thinks will happen — as a percentage — before
each market closes.

**It only watches. It never buys, sells, or places any order for you.**
Every trade decision is 100% yours.

## 📍 Where we are right now

The live model remains unchanged. With `v2` selected, new locked decisions
also record a shadow model with its order-book drift weight set to zero.
The shadow model never changes the live recommendation or places an order.
We are collecting prospective comparisons before deciding whether to
replace the model; no sample count guarantees accuracy or profitability.

### Shadow comparison

After deploying this version, each new decision records both models on
the same inputs and timestamp, including abstentions. The momentum weight,
settlement formula, decision timing, edge threshold, and confirmation rules
stay the same. Book imbalance remains part of the confirmation gate for
both models; only the challenger's probability-model tilt is removed.

The new `shadow_decisions` table is created automatically in SQLite or
Postgres. Existing decisions are never backfilled or overwritten. Restarting
does not replace snapshots. Shadow collection requires the `v2` live model;
selecting `v1` preserves legacy behavior without creating shadow snapshots.

Read-only endpoints (use your dashboard host and port):

- `/api/shadow-comparison`: matched live, shadow and market accuracy, Brier
  score, log loss, confidence diagnostics and actionable-call counts.
  Results are separated by coin and experiment configuration. Pending
  outcomes are counted but not scored. The default is the latest 10,000
  recorded pairs, not all historical decisions.
- `/api/shadow-decisions`: exact features, buffered index ticks, model
  settings, both recommendations and confirmation votes, quote sizes,
  bid/ask prices, quote timestamps and index-tick age, joined to outcomes.
  The default is the latest 200 pairs.
- Both accept `index_id=BRTI` or `index_id=SOLUSD_RTI` and `limit=1..10000`.

These are research endpoints, not a new dashboard panel. The existing
dashboard and its calibration numbers still describe the live model.
The stored NO ask is inferred as `1 - YES bid`. Quotes and their ages are
recorded as observed, not guaranteed fresh or executable; fees, fills,
latency and slippage are not simulated. Comparison scores are not net profit.
Changing model parameters, edge threshold or decision lead time starts a
separate configuration group. Algorithm changes require bumping the
experiment/model/confirmation version labels before collecting more data.

For the Docker deployment, update the source on the server, back up the
database, then run `docker compose up -d --build`. Keep the existing database
volume; do not run `docker compose down -v`. Inspect `/api/shadow-decisions`
after the next new market locks. Comparison scores appear after settlement.
Local edits alone do not update a bot already running on another machine.

## What does it actually do?

1. Watches the live Bitcoin/Solana price every few seconds.
2. Every 30 seconds, it estimates the odds the price will finish **above**
   or **below** the market's target price ("strike") when the 15-minute
   window closes.
3. About 6.5 minutes before each market closes, it locks in one final call:
   **BET UP**, **BET DOWN**, or **NO TRADE** — and never changes it after
   that, so you can judge it fairly.
4. Once the real result comes in from Kalshi, it checks itself: was the
   call right or wrong? That gets added to its permanent track record.

Everything above shows up live on a web dashboard you open in your browser.

## Getting it running

### Option A: Docker (recommended)

This is the easiest way to run it long-term — it also sets up its own
database automatically.

1. Copy `.env.example` to `.env`.
2. Fill in your Kalshi API key ID and the path to your private key file
   (see `.env.example` for the exact variable names).
   ⚠️ Kalshi has separate **demo** and **prod** keys — make sure
   `KALSHI_ENV` matches whichever account you generated the key on, or
   every request will fail.
3. Run:
   ```
   docker compose up --build
   ```
4. Open **http://localhost:8000** in your browser.

Your data is saved in a Docker volume, so stopping and restarting
(`docker compose down` / `docker compose up`) keeps everything. Only
`docker compose down -v` wipes it.

### Option B: Run it directly with Python (no Docker)

1. Create a virtual environment and install the bot:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -e .[dev]
   ```
2. Copy `.env.example` to `.env` and fill in your API key info (same as
   above). You can leave `DATABASE_URL` as-is — this option just uses a
   local file instead of a database server.
3. Run it:
   ```
   python -m kalshi_bot.main
   ```
   Keep this terminal window open — closing it stops the bot.
4. Open **http://127.0.0.1:8000** in your browser.

## Reading the dashboard

**Top of page — track record.** Shows how accurate the bot has been so
far, overall and separately for Bitcoin and Solana, compared to just
trusting the market's own price and to a random coin flip. Lower numbers =
more accurate. This starts empty and fills in as markets settle.

**Active Markets tab** — one card per market that's currently open:
- **ABOVE / BELOW** — the bot's current best guess, updated continuously.
- **Trade banner** — the one-shot **BET UP / BET DOWN / NO TRADE** call,
  locked in 6.5 minutes before close and never changed after that. Shows
  which independent checks agreed with the call, for transparency.
- **Price chart** — the last 10 minutes of price, with a dashed line
  showing the target price.
- **Prediction bars** — the bot's estimated odds vs. what the market's own
  price implies, side by side.
- **Countdown** — time left until the market closes.

Closed markets stay visible (dimmed) for a few seconds, then move to the
**Closed Markets tab**, where you can see whether each past call was right
or wrong once Kalshi reports the result.

### 🐋 Whale Tracker (who's putting in the big money?)

Each card has a **🐋 Whale Tracker** button. Click it to flip that card
from the prediction view to the live institutional order flow of the largest
recent fills on that specific market. Use the quick filter preset chips
(`$50+`, `$100+`, `$250+`, `$500+`, `$1,000+`) or type a custom minimum dollar
amount and press **Apply** to surface the latest trades meeting that threshold.
The threshold is remembered in your browser. Click **Prediction Signal** (⚡)
again to flip back to the signal view.

The filter can only search trades the bot collected. `KALSHI_WHALE_MIN_USD`
is the collection floor (default `$100`), so lowering the dashboard filter
below that value cannot recover smaller historical trades that were never saved.

**Important: this shows big trades, not big traders.** Kalshi's public
trade data doesn't reveal who made a trade — no names, no accounts. So this
can't tell you "who" is betting big, only that a trade of unusually large
size (by default, $100+) just happened, on which side, and at what price.
A large trade can be someone with strong conviction — or just as easily a
hedge, an exit, or someone market-making. Treat it as something worth
noticing, not something to blindly copy.

## How it makes its guess (in plain terms)

The bot doesn't just guess randomly — it uses:

- **How much the price has been moving around** (volatility) — more
  movement means less certainty either way.
- **Which direction the price has been trending** (momentum).
- **How Kalshi actually settles these markets** — not on the price at the
  exact final second, but on the *average* of the last 60 seconds. An
  average bounces around less than a single instant, so the bot accounts
  for that instead of assuming more uncertainty than there really is.
- **A small nudge from the order book** — if a lot more people are trying
  to buy "yes" than "no" (or vice versa), that's a weak extra clue.

There are two versions of this math (`v1` = simple, `v2` = accounts for
the settlement averaging above). `v2` is the default. You can switch back
to `v1` any time by changing `KALSHI_PREDICTOR_VERSION` in `.env` — nothing
else needs to change.

## How do we know if it's actually any good?

**Don't take our word for it — check the track record panel on the
dashboard.** That's the real answer, always. Here's exactly what's being
measured and what its limits are:

- ✅ Every time a market settles, the bot checks the real result against
  the call it locked in — not against whatever it happened to be saying
  most recently. That keeps the scoring honest.
- ✅ Before locking in a BET UP/DOWN call, the bot cross-checks it against
  a couple of independent signals (short-term and long-term momentum, and
  order-book pressure). If most of them disagree with its own call, it
  downgrades to NO TRADE instead of forcing a shaky pick.
- **Sample size alone does not establish a trading edge.** Evaluate frozen
  decision-time predictions on later, untouched markets across multiple days
  and market conditions. BTC and SOL in the same window are correlated, not
  independent trials. Check calibration and returns after execution costs.
- ❌ The bot beating its own confidence threshold does **not** mean it
  beats the market. The track record panel is what tells you whether it
  actually has, once there's enough data to say so.

### Signal quality review: September 13, 2026

Read-only review of `http://192.168.1.208:8000/api/closed?limit=10000`,
`/api/calibration`, `/api/shadow-comparison`, and `/api/shadow-decisions?limit=10000`.
The returned history contained 178 closed markets, including 176 with pre-close
locked decisions and reported YES/NO outcomes. Those decisions cover settlement
times from 00:00 through 21:45 UTC on September 13, 2026, with 88 markets per coin.

| Decision-time metric | BTC | SOL | Combined |
|---|---:|---:|---:|
| Model Brier score (lower is better) | 0.1705 | 0.1880 | 0.1792 |
| Market Brier score, same decisions | 0.1461 | 0.1392 | 0.1427 |
| Trade calls won / trade calls issued | 32 / 47 | 31 / 47 | 63 / 94 |
| Average predicted probability of the recommended side | 83.4% | 89.7% | 86.5% |
| Hypothetical gross P&L at midpoint, one contract per call | -$0.1385 | -$2.5965 | -$2.7350 |

The combined trade win rate was 67.0%, well below the average predicted 86.5%.
Even calls with at least 90% model probability won only 46 of 55 times (83.6%).
Midpoint P&L assumes purchases at a non-guaranteed midpoint and settlement at
$1 or $0; it excludes fees, slippage, size constraints, and actual fills. Total
hypothetical midpoint cost was $65.735. These are not realized trading returns.

There were 58 settled paired shadow observations (29 per coin). Removing the
order-book drift improved Brier scores from 0.1954 to 0.1531 for BTC and from
0.1443 to 0.1336 for SOL, but both still trailed the same-sample market scores
(0.1331 and 0.1255). At recorded ask quotes, buying one contract for each issued
call and holding to settlement produced -$0.372 across 37 live calls versus
-$1.716 across 20 shadow calls, before costs. Better probability scores did not
produce better trade returns in this small sample. Do not promote the shadow
model on these results alone.

Four of the 60 total shadow snapshots had less than 240 seconds of index
history; two had less than 60 seconds. Short and full-window momentum can then
reuse the same observations. The checks also share inputs with the probability
model, so a vote count is not independent validation of its confidence.

**Current assessment: research/paper trading only, not a validated execution
signal.** The selection rule now requires the configured edge at the YES ask
or implied NO ask (`1 - YES bid`), not at the midpoint. Stored `edge` and
`market_p_yes` remain midpoint comparisons for calibration; displayed edge is
not net profit. Fees and slippage are not yet modeled. The new rule receives a
separate shadow experiment fingerprint and does not rewrite past decisions.
It would not have removed any of the 37 live calls in this paired sample, so
this correctness fix is not evidence of improved historical returns.

### Live decision data collection

Every locked decision now writes an always-on audit record to the
`decision_snapshots` table, even if the optional shadow model fails. The audit
stores the pre-decision index tick window, feature values, quote bid/ask and
sizes, quote/index ages, model and recommendation outputs, confirmation votes,
configuration fingerprint, and `quality_flags`. Shadow experiments remain in
`shadow_decisions` and are not required for the audit record to exist.

Current quality flags include `short_index_history`, `stale_index_tick`,
`stale_quote`, `missing_quote`, and `invalid_quote`. A clean audit record means
the inputs passed these collection checks; it does not prove the prediction was
correct or that a quote was executable for the desired size. The records are
decision-time snapshots and are joined to `markets.result` only after the
settlement poll records an outcome.

After restarting the bot and allowing audited decisions to settle, run the
forward-quality report with:

```powershell
python -m kalshi_bot.backtest.report --database data/kalshi_bot.db
```

Include conservative execution assumptions in dollars per contract when
available:

```powershell
python -m kalshi_bot.backtest.report `
  --database data/kalshi_bot.db `
  --fee-per-contract 0.01 `
  --slippage 0.01
```

The report excludes flagged snapshots from clean model/market scoring while
reporting their counts separately. It outputs Brier score, log loss, accuracy,
ten probability calibration buckets, trade coverage, abstention rate, wins,
ask-price P&L, and maximum drawdown overall and per coin. P&L is a
one-contract hypothetical based on recorded asks, not a fill simulator.

The table is created automatically for existing SQLite/Postgres databases when
the bot starts. Restart the running bot process after updating the code so the
schema and audit path are active. Past decisions cannot be reconstructed into
this table unless their raw inputs were already retained elsewhere.

Before any model promotion:

1. Add warm-up, timestamp freshness, valid quote, and available size gates;
  abstain when decision inputs are incomplete or stale.
2. Evaluate against executable asks with the applicable fee schedule and
  conservative slippage, not just directional win rate or midpoint edge.
3. Fit probability calibration and challenger parameters only on earlier
  settled data; freeze them before each chronological validation period.
4. Compare the current model, no-book challenger, and market-anchored baseline
  on identical future windows. Report probability scores, trade coverage,
  net returns, drawdowns, and uncertainty clustered by settlement window/day.
5. Require sustained out-of-sample and forward paper-trading evidence across
  multiple regimes. No probability model guarantees accurate bets.

### Experimental v3 probability model

`RegularizedSettlementPredictor` is now the shadow challenger for the default
v2 model. It does not replace live predictions or rewrite recorded decisions.
New snapshots use a `regularized-settlement-v3-...` experiment ID; old no-book
experiments remain separate. Restart/redeploy the bot to collect the new
shadow forecasts. `KALSHI_PREDICTOR_VERSION` still supports only v1 and v2;
do not set it to v3.

The candidate changes the forecast calculation itself:

- Projects momentum to the average time of the future settlement interval,
  rather than to its endpoint (including partial settlement windows).
- Removes the unvalidated order-book drift term.
- Adds drift-estimation uncertainty based on observed history duration. The
  continuous Brownian-path OLS approximation uses slope variance
  `6 * sigma^2 / (5 * history_seconds)`, scaled by the momentum weight squared.
- Returns a neutral 0.5 with insufficient history (less than 240 seconds or
  120 ticks), invalid inputs, zero observed volatility, or incomplete expired
  settlement data. Neutral forecasts cannot issue directional recommendations.

These are research assumptions, not fitted or calibrated probabilities. The
history thresholds are conservative engineering choices, not proven optimal
values. Arithmetic Brownian dynamics, dense observations, and the existing
settlement tick-count convention remain approximations. Gaps/duplicates and
feed timing still require ingestion-level safeguards; raw tick count alone
does not prove full settlement coverage. None of these guards apply to the
unchanged live v2 model yet.

Reproduce the retrospective comparison using the saved snapshot endpoint:

```powershell
python -m kalshi_bot.backtest.runner "http://192.168.1.208:8000/api/shadow-decisions?limit=10000"
```

The same command accepts a local JSON export instead of a URL. It performs no
fitting, scores once per market per experiment, groups by experiment and coin,
and splits chronology at a common settlement window across coins. Pending,
post-close, duplicate, and future-input rows are excluded. Invalid JSON/schema
errors fail the command rather than silently inventing inputs. Saved features
are trusted as decision-time inputs; timestamp checks cannot establish their
provenance. Results are retrospective, not an untouched holdout, and quote
returns exclude fees, slippage, latency, and available-size constraints.

On the September 13 replay (62 rows returned, 60 settled and evaluated across
30 paired windows), with parameters fixed before the replay:

| Metric | Recorded live v2 | Candidate v3 | Market odds |
|---|---:|---:|---:|
| Combined Brier score | 0.1647 | 0.1526 | 0.1312 |
| Later-half Brier score (30 markets) | 0.1970 | 0.1802 | 0.1642 |
| Calls won / calls issued | 27 / 38 | 10 / 16 | Not traded |
| Hypothetical gross P&L at asks | +$0.228 | -$1.702 | Not traded |

The two formerly pending outcomes had settled by this replay, so this sample
differs from the earlier 58-row audit. V3 improved combined Brier and log loss
relative to live v2, but not directional accuracy or trade returns; its SOL
full-sample Brier also slightly worsened. The older no-book shadow model scored
0.1411 combined Brier, better than v3. **Do not promote v3 based on these
results.** Collect forward shadow outcomes and evaluate a frozen candidate on
new data before considering any live switch.

### Historical research backfill

The public production API backfill is separate from the running bot and needs
no trading credentials. It never inserts historical predictions into live
decisions, signals, or calibration. The default output is the local SQLite
file `data/research_backfill.db`, regardless of `DATABASE_URL`.

```powershell
python -m kalshi_bot.backtest.backfill --start 2026-08-14T00:00:00Z --end 2026-09-13T00:00:00Z
```

The range means market close times **after start, through end inclusive**. Both
timestamps must have a timezone. The importer checks `/historical/cutoff`,
paginates market lists, batches recent candles by six-hour blocks, and uses
historical candle endpoints for archived markets. Existing REST rate-limit
backoff applies. Rerunning refreshes metadata, skips complete candle histories,
and retries sparse/empty/failed histories. `--max-markets` defaults to 10000;
reaching the bound aborts rather than silently reporting full coverage.

An existing live/non-research database is rejected. Research runs record the
source, requested interval, archive cutoff and completion summary. Raw market
and candle JSON are retained beside normalized prices (dollars, not cents).
No individual trade backfill or underlying index history is included yet.

Verified September 13 import:

| Dataset item | Count |
|---|---:|
| Requested completed UTC days | 30 |
| BTC markets returned | 2,847 |
| SOL markets returned | 2,847 |
| One-minute candles | 85,410 |
| Returned markets with all 15 candle intervals | 5,694 |
| Unverifiable markets quarantined from training | 2 |
| Eligible 6-, 3-, and 1-minute examples | 17,076 |
| Final failed candle downloads | 0 |

There are 33 absent 15-minute slots per coin versus a continuous schedule,
in matching gaps on August 20/27 and September 3/10. The API returned no markets
for those slots in this run; their cause has not been independently confirmed.
Do not synthesize missing markets or label them as losses.

The stored candles had no missing, crossed, or out-of-range bid/ask values.
After recognizing two valid thousands-separated settlement strings, all 5,692
verifiable labels agreed with the reported settlement value being at least
the goal price. Two August 14 markets lack the goal/structured rule metadata
and are quarantined. BTC metadata specifies rounding to 2 decimals and SOL to
4 decimals, with `strike_type=greater_or_equal`; the current predictor's
equality and rounding conventions need a separate correction before exact
settlement replay can be claimed.

Tables/views:

- `research_markets`: reported outcomes, times, raw rules and metadata.
- `research_candles`: minute bid/ask/trade closes, volume/open interest, raw JSON.
- `research_market_quality`: explicit reasons a market cannot be verified.
- `research_examples`: nearest fully ended candle at 360/180/60 seconds before
  close, less than one minute old, with valid bid/ask. Never uses a candle
  ending after the simulated decision. Candle publication/receipt latency is
  not available historically and must be modeled separately.
- `research_training_examples`: the same examples excluding quarantined markets.
- `research_runs`: import provenance and completion records.

The three horizons share an outcome; they are not 17,076 independent markets.
BTC/SOL windows are correlated too. Candles cannot establish executable size,
quote freshness within a minute, or actual fills. This dataset cannot recreate
the old bot's one-second index features or its historical confidence values.
Do not use final settlement values, final market quotes, full-market volume,
or post-decision candles as predictor inputs.

#### Concrete next model-development steps

1. Freeze splits by market close time, keeping both coins and all horizons
  together: training `(Aug 14 00:00, Sep 3 00:00]`, validation
  `(Sep 3 00:00, Sep 8 00:00]`, test `(Sep 8 00:00, Sep 13 00:00]`, all UTC.
  Respect actual settlement availability at each fit boundary. These are
  reserved development splits, not evidence of future performance; the
  collection has already undergone a whole-sample quality audit.
2. Build separate 6-/3-/1-minute baseline reports from
  `research_training_examples`: Brier, log loss, reliability bins, and
  quoted spread by coin. Predicting later is naturally easier; do not
  present this as model improvement at the original 6:30 decision horizon.
3. Train a regularized market-anchored logistic correction using only earlier
  completed candle odds, odds changes, spread, and trailing volume/open
  interest changes. Use market log-odds as the starting prediction. Fit on
  training dates only; select regularization and calibration on validation.
  Do not feed contract-price changes into the underlying-price model as
  though they were BTC/SOL index changes.
4. Evaluate once on the reserved test dates, including executable-price
  proxies, applicable fees and conservative latency/slippage. Compare with
  the unadjusted market on identical examples, report coverage/drawdown and
  uncertainty by settlement window/day, and avoid repeated test-set tuning.
5. Fix the live data-quality/settlement guards and deploy the new candidate in
  shadow mode with the same candle features/horizons. Collect fresh forward
  results before any promotion. Neither this backfill nor v3's unit tests
  establish a profitable trading edge.

The first fixed 6-minute challenger was tested before deployment using market
log-odds, five-minute market-probability movement, and quoted spread. It was
fit only on the training dates with regularization, then evaluated on the
reserved September 8-13 test dates. Its Brier score was `0.144352` versus
`0.144099` for raw market odds, and its log loss was `0.443056` versus
`0.442483`. It was correctly rejected and was not wired into live decisions.
This is useful evidence: contract-price movement and spread alone did not
improve the market baseline in this period. The next challenger needs genuinely
new information, such as verified underlying-index history, or must focus on
calibration/abstention and execution costs rather than more market transforms.

The 30-day database is local research output and is ignored by Git. Back it up
or transfer it separately if training on the LAN server; this command does not
modify or deploy anything to that server.

## Settings (`.env`)

| Variable | What it controls |
|---|---|
| `KALSHI_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH` | Your Kalshi API credentials |
| `KALSHI_ENV` | `demo` or `prod` — must match the account your key belongs to |
| `KALSHI_INDEX_IDS` | Which price feeds to track (default `BRTI,SOLUSD_RTI`) |
| `KALSHI_COIN_TICKS` | Which coins to find markets for (default `BTC,SOL`) |
| `KALSHI_POLL_INTERVAL_SEC` | How often the bot recalculates its guess |
| `KALSHI_EDGE_THRESHOLD` | Minimum model probability minus purchase price, before fees/slippage |
| `KALSHI_FEE_MULTIPLIER` | Kalshi taker-fee multiplier used in `ceil(M * 0.07 * P * (1-P) * 100) / 100` (default `1`) |
| `KALSHI_SLIPPAGE_PER_CONTRACT` | Conservative slippage in dollars deducted from executable edge (default `0`) |
| `KALSHI_PREDICTOR_VERSION` | `v1` (simple) or `v2` (default, settlement-aware; accuracy must be validated) |
| `KALSHI_MIN_INDEX_HISTORY_SEC` | Minimum index history span before a signal is allowed (default `240`) |
| `KALSHI_MIN_INDEX_HISTORY_TICKS` | Minimum index observations before a signal is allowed (default `120`) |
| `KALSHI_MAX_INPUT_AGE_MS` | Maximum quote/index age before the bot abstains (default `5000`) |
| `KALSHI_MIN_QUOTE_SIZE` | Minimum YES bid and ask size required for a signal (default `1`) |
| `KALSHI_CLOSED_GRACE_SEC` | How long a just-closed market stays on the Active tab |
| `KALSHI_DECISION_LEAD_SEC` | How early (in seconds) the final BET UP/DOWN call locks in |
| `KALSHI_WHALE_MIN_USD` | Minimum dollar size of a trade to count as a "big bet" |
| `KALSHI_WHALE_POLL_INTERVAL_SEC` | How often to check for new big bets |
| `DATABASE_URL` | A file path = SQLite; a `postgresql://` link = Postgres (Docker sets this automatically) |
| `KALSHI_DASHBOARD_HOST` / `KALSHI_DASHBOARD_PORT` | Where the dashboard is served |

## Project layout (for developers)

- `src/kalshi_bot/auth.py` — signs requests to Kalshi's API.
- `src/kalshi_bot/kalshi_client/` — talks to Kalshi over REST and WebSocket.
- `src/kalshi_bot/market_discovery.py` — finds the currently open 15-minute markets.
- `src/kalshi_bot/data/store.py` — saves everything (SQLite or Postgres).
- `src/kalshi_bot/features/engine.py` — turns raw prices into volatility/momentum numbers.
- `src/kalshi_bot/prediction/model.py` — the actual v1/v2 prediction math.
- `src/kalshi_bot/signals/generator.py` — compares the model's guess to the market's price.
- `src/kalshi_bot/signals/confirmation.py` — the cross-check before locking in a trade call.
- `src/kalshi_bot/backtest/runner.py` — tests the model against made-up historical data.
- `src/kalshi_bot/dashboard/` — the web dashboard you look at.
- `src/kalshi_bot/main.py` — starts everything and ties it together.
- `Dockerfile` / `docker-compose.yml` — one-command setup with Postgres included.

## Running the tests

```
pytest
```

