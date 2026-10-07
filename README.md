# AQLabs — Kalshi 15-Minute Crypto Signal

A bot that watches Kalshi's 15-minute Bitcoin and Solana "above or below"
markets and tells you what it thinks will happen — as a percentage — before
each market closes.

**Order execution is disabled by default.** The Trading tab provides explicit
opt-in automatic trading, saved dollar thresholds, and an activity journal.

## Dashboard trading

The **Trading** tab (or `/trading`) is written in plain language and has two
independent modes (it opens on Practice):

- **Bot Simulation trading** — simulates fills against the live order books and
  tracks a pretend account. No orders are ever sent. Practice results are
  estimates: they ignore queue position and your order's market impact,
  so real fills can be worse.
- **Bot Real Trading** — places real orders on your Kalshi account
  (demo or production, depending on `KALSHI_ENV`; the tab says which).

Each mode has independent saved settings. The strategy editor now has just
four controls: **Confidence level (%)**, **Amount per trade ($)**,
**Stop loss amount ($)**, and **Trades per 15-minute market**.
Saving installs a single momentum-scalping strategy
for that mode; it never enables trading or changes the other mode.

### Simple momentum scalping

The bot can buy either UP or DOWN, following observed movement rather than
choosing whichever side has the largest estimated settlement-value edge.
It compares both YES bid and ask against a quote at least 10 seconds old
(no older than 60 seconds), requiring both to move at least 1 cent in the
same direction and agree with the coin's trailing 60-second log-price slope.
An isolated high UP probability is not evidence of rising quotes. The bot
warms up again after a restart and rejects movement that reverses during
execution checks.

- **Confidence** (50-99%, initially 65%) is the moving side's market-implied
  settlement probability from the bid/ask midpoint. It is **not** a calibrated
  probability of profitable scalping or the settlement model's prediction.
- **Amount per trade** is a maximum purchase budget including reserved entry
  fees, up to $25. Whole-contract sizing can spend less or buy nothing.
- **Profit target** (default $0.03) and **Stop loss** (default $0.05) are net
  dollars after entry fees, spread and the exit fee. Entry fees and exit fees
  use the real quadratic taker fee at the actual price (about 1 cent each at
  85 cents or more, 2 cents nearer 50 cents), not a flat 2-cent worst case.
  A trade is only taken when buying and immediately selling back (spread plus
  both fees) leaves at least 2 cents of room before the stop loss; otherwise it
  is skipped with the reason logged. With a 1-cent spread that means a $0.05
  stop trades at about 83 cents and above, and cheaper entries need a wider
  stop. The stop is a net-loss sell trigger, not a guaranteed maximum loss;
  illiquid exits can lose more. A tight stop sells on small dips, so expect more
  small losses than with a wide stop; the profit target must be reached from
  roughly 5-6 cents above the quote, so check results in Practice first.
- **Stop loss** must be positive and smaller than the trade amount.
- **Trades per 15-minute market** sets 1-10 buy/sell cycles per individual
  ticker (default 3), not a total across coins. The cumulative spending cap
  is the smaller of $25 or the trade amount times this limit. Existing markets
  retain their original caps; changing this setting cannot raise them.
- **Start trading after** sets how many seconds into each 15-minute market the bot may begin entering (0-810, default 60). Enter 360 to start at the 6-minute mark. It is saved as the rule's `max_seconds_left` (900 minus this value); the bot stops entering when 90 seconds remain.
- The default profit target is **$0.03 net per position**, after paid entry
  costs and the exit fee at the quoted price. The bot checks that the actual sized order has
  enough price room for this target, and rejects entries whose spread and
  fees leave under 2 cents of room before the stop loss. This is a target,
  **not a promise of profit**.
- Automatic limits: 5-95 cent entries from your start time until 90 seconds are left, maximum
  3-cent spread, your configured cycle limit, 30 seconds after a fully closed
  profitable exit, cumulative market spending of `min($25, cycles x trade amount)`,
  and a market-loss trigger equal to the stop-loss amount. No re-entry after
  a loss or settlement. At most two open positions, open cost at most twice
  the trade amount, and a daily loss budget of three times the trade amount,
  including reserved open-position cost. A stricter server daily limit still
  applies. High confidence settings may leave no qualifying entry prices.
- **Locked-call gate.** The bot only trades after the model locks its
  direction for that market (at T-6:30, `KALSHI_DECISION_LEAD_SEC`) and keeps
  trading, if its other limits allow, until the market ends. An entry must go
  the same way as the locked UP/DOWN call, and at least 2 of the 4 safety checks
  (OLS momentum, short-term momentum, order-book imbalance, LLM risk review)
  must agree, the same checks shown on the model's pick. Before the lock, with
  fewer than 2 checks, or against the locked call, the market shows as waiting
  or no match and nothing is bought. Exits are unaffected. Each bet stores its
  `market_id` (the Kalshi market ticker) and an `entry_gate` snapshot of the
  locked call and checks; its orders and events carry the same `market_id`.
- Fresh-data, executable-liquidity, account-reconciliation, and IOC execution
  checks remain mandatory. Momentum entries do not pretend to have positive
  settlement-value edge and are excluded from settlement-model P/L comparisons.

Existing saved rules are **not silently migrated**. Turn the bot off, set the
four controls, and select **Save settings** to replace that mode's old rules
and risk settings. Existing positions keep their original exits and market
caps. Start in Practice; Real money still requires explicit confirmation.
Deploy/restart the updated backend and build/deploy the frontend to use this
strategy. There is no live-profit validation for these thresholds.

The settings and confirmation APIs accept `side: "momentum"` as well as
legacy sides. Validation failures display the affected field and reason in
the dashboard; a rejected save leaves the previous saved settings unchanged.

The trading page also shows:

- **Bot status** — a big ON/OFF card with the switch and a plain list of
  anything stopping the bot from betting. Turning Real money on requires a
  confirmation box.
- **Scoreboard** — bets running now, finished bets, total won/lost, win rate.
- **Bets happening now** — one card per open bet: coin, UP/DOWN, amount paid,
  cost per contract, payout if it wins, value if sold now, time left, the rule
  that triggered it, its exit plan, what the bot did, and why.
- **Finished bets** — responsive cards for closed bets only, newest first:
  Won/Lost/Broke even, prominent net profit or loss, amount paid and returned,
  completion time, and how it ended (market ended / cashed out early / sold to
  cut losses). Compact cards show the two newest bets by default; **Load more**
  reveals two more at a time and **Show fewer** returns to the latest two.
  Expand **Trade details** for contracts and the rule used. Switching money
  modes resets the list to the latest two; scoreboard totals include all bets.
- **Markets the bot is watching** — every live market with the bot's and the
  market's current UP/DOWN lean, centered direction bars, and a plain reason it
  is or isn't betting, using the same indicators as Live Picks. The percentage
  is the favored side's estimated probability (DOWN is one minus UP), not the
  saved final-pick confidence. Exactly 50% shows EVEN; missing/invalid odds
  show `--`. A lean is not an order: the entry side still follows the rule and
  available prices, and can differ from the favored direction.
  Compact cards use roughly half the previous height while keeping the
  countdown, probabilities, and full betting/skip reasons visible.
  Trading updates arrive over `/ws/trading` after each bot check or settings/
  control change, without repeated API polling or page refreshes. Countdowns
  tick locally every second. The connection indicator warns when updates stop
  and reconnects automatically; the bot's execution cadence is unchanged.
- **Scalp market movement** — the four controls, with automatic limits in a
  collapsed explanation rather than a separate rule/safeguard editor.
- **Bot diary** — a collapsible log of everything the bot did.

Save settings before turning the bot on. Settings are stored in the bot database,
not the browser; they survive refreshes and restarts and do not require a bot
restart to change.

### Legacy/API entry rules

The API continues to accept legacy rules for compatibility; the dashboard
no longer exposes their multi-field editor. Every few seconds the bot checks each live
market against your enabled rules (top to bottom, first match wins). A rule
matches when **all** of its conditions hold:

| Rule field | Meaning |
|---|---|
| Coin | `ANY`, `BTC`, or `SOL` |
| Which way to bet | **Follow the bot's guess** (side with the larger estimated after-fee edge at current quotes; not necessarily the favored direction), **Always bet UP**, or **Always bet DOWN** |
| Lowest/Highest price to pay | Price of one contract of the side being bought, in cents (1–99¢) |
| How sure the bot must be | Model probability that the bought side wins (0–100%) |
| Minimum expected profit | Confidence minus price minus taker fee, in cents; blank turns the check off |
| Start/Stop betting at (seconds left) | Time window before market close (e.g. 390→330 = around T-6:30) |
| Most to spend per trade | Dollars per trade, at most **$25** (fees are reserved from it) |
| Cash out when up by / Cut losses when down by | Net-P/L exit thresholds in dollars; **0 = never (hold to the end)** |

### Repeated scalping (opt-in)

Legacy API rules can opt into repeated scalping in Practice or Real money.
The former multi-field scalping preset is no longer shown in the dashboard.
The simple momentum strategy uses the same persistent cycle and exit engine.
Saving never enables trading; real money still requires confirmation.

Scalping is one buy per cycle, with no scale-in. After the entire position is
sold at a **take-profit exit with positive realized net P/L**, the bot waits
the cooldown and may buy again in the same ticker. Every re-entry must pass
the current rule, fresh prediction/quality checks, account/order reconciliation,
and actual order-book checks. A partial or unknown exit never permits a new
cycle. Stop-loss, settlement, rejected/permanently skipped, and zero-fill
cycles end trading in that ticker; the bot does not chase losses.

| Scalping field | Meaning |
|---|---|
| Cycles per market | 1-10 separate buy/sell positions, including the first cycle |
| Cooldown after exit | 5-900 seconds after the full exit is reconciled |
| Total spending per market | Up to $25, cumulative entry costs including fees; profits do not replenish it |
| Market loss limit | Up to $25; blocks entries after realized losses and triggers a stop-loss exit when remaining loss allowance is breached |

Scalping requires positive take-profit and stop-loss amounts, a minimum
after-entry-fee edge of 1 cent for legacy settlement-value rules (momentum
requires `min_edge: null` instead), and an entry window between 60 and 900 seconds
left. The loss limit cannot exceed spending; the buy budget cannot exceed
spending; the per-position stop-loss cannot exceed the market-loss limit.
Each market retains its original caps: settings edits cannot raise its cycle,
spending, or loss limits or shorten its cooldown. Another rule/name cannot
bypass a completed or stopped scalping market. Caps and cooldowns survive
restarts, but new entries always restart paused.

Targets are **net dollars per position**, not price moves or per-contract
profits. Profit checks subtract paid entry costs and reserve exit fees, and
sell orders are price-limited IOC orders. Profit is not guaranteed: quotes
can move, orders can fill partially, and fees can differ from estimates.
Stop-loss and market-loss amounts are triggers, **not guaranteed maximum
losses**; a failed/illiquid exit can lose the full purchase cost. Unexited
positions remain monitored and can settle when the market closes.

Each cycle has a separate persistent journal, order IDs, entry prediction,
timestamps, and P/L. Running cards and trade details show cycle numbers; watch
cards show cumulative spending, closed net P/L, cooldowns and stop reasons.
The scoreboard includes all funded closed cycles even when the displayed
history is limited to the latest 100 closed positions.
Test in Practice before using real money. Settlement probability is not a
prediction of a price rise over the next few seconds; no preset is a proven
profitable scalping strategy.

### Starting a clean scoreboard

To wipe one mode's placed bets and diary (saved settings are kept), turn the
bot off, wait for running bets to finish, then run on the machine hosting the
database. With Docker:

```
docker compose exec bot python -m kalshi_bot.clear_bets --mode paper
docker compose cp bot:/app/data/. ./bets-backups   # copy the JSON backup out
```

Use `--mode live` for real-money (or Kalshi demo) bets. A JSON backup is written
first (`--backup PATH` to choose where) and the command refuses while bets are
still running unless `--force` is given. Without Docker, run
`python -m kalshi_bot.clear_bets --mode paper`.
### Practice-first safeguards and validation

Existing saved Practice and Real-money rules remain unchanged until explicitly
replaced with **Save settings**. No mode is turned on automatically. The old
conservative-practice preset and Entry safeguards editor have been removed;
the simple controls derive automatic limits described above. Optional API
limits remain supported independently for each mode. Dollar, position, and
spread limits of zero are disabled. The daily
budget is conservative: it subtracts net realized losses since midnight UTC
and reserves the entire remaining cost of open positions, assuming they can
all lose. The server's `KALSHI_DAILY_LOSS_LIMIT_USD`, when positive, is an
additional ceiling; mode settings cannot weaken it. These limits block new
entries and scale-ins, but never prevent reconciliation or existing exits.
They cover this bot's journal, not unrelated manual trades or other accounts.

For **every entry**, not just scalping, the bot now:

- Fetches both sides of the book together, caps quantity to best-price depth,
  and retains a 2-cent-per-contract fee reserve to cover fragmented fills.
- Rejects book requests/checks older than the configured limit (2 seconds by
  default), crossed books, future/stale predictions, and degraded inputs.
- Recomputes the active model using current index data and the executable
  bid/ask midpoint after the asynchronous account/book checks. It rechecks
  price, side, timing, confidence, buffered edge, and risk limits immediately
  before submitting a price-limited IOC order.
- Optionally requires fresh Coinbase **and** Kraken prices to agree with the
  settlement index's direction relative to the strike. Both event and receive
  timestamps must be within 5 seconds. This is off by default: exchange basis
  differences can reject otherwise valid trades, and neither feed settles
  Kalshi contracts.

The dashboard omits the former Execution costs panel to keep the page compact.
The trading API still summarizes reconciled orders, buy fees, and entry cost
above the checked midpoint (spread/slippage plus fees).
Each order's requested/filled quantity, actual costs, checked quotes/depth,
prediction, and timestamps are retained in its persistent position journal.
For measured hold-to-settlement bets, it also compares predicted net return
with realized net return; early exits are excluded to keep the comparison
meaningful.
Old positions without these measurements are not reconstructed. Practice
still ignores queue position and market impact; it is not proof of live fills.

Run a read-only, market-level validation locally:

```powershell
python -m kalshi_bot.backtest.walk_forward --database data\kalshi_bot.db
```

This command takes one as-of decision per settled market at T-6:30, compares
fixed fair-value forecasts with market odds, and trains market recalibration
and logistic challengers only on outcomes recorded before each test fold.
It reports Brier/log loss, per-coin results, fees, net returns, and drawdown
with 0/2/10-second delayed quotes, 1-cent extra execution cost, and a 2-cent
uncertainty buffer. It requires recorded size for simulated trades and never
looks forward to fill missing quotes. A minimum 300 prior markets is needed
to fit challengers; smaller samples do not silently count as fitted models.
Use `--help` for research parameters.

The frozen model was previously fitted: replay is **not an untouched holdout**.
The runner never writes to the database, changes coefficients, promotes a
model, places orders, or enables either bot. Validate over multiple future
market periods and both coins, including losing runs, before considering
real-money changes. Maker execution and Kelly sizing remain disabled/not
implemented: their fills and probability calibration need separate evidence.
No accuracy or profitability is guaranteed.

On a match the bot re-checks the rule against the **real order book** (not the
quote), sizes the order to the budget and top-of-book size, and submits a
price-limited immediate-or-cancel buy. It holds **at most one open position per
coin** (a BTC and a SOL position can run concurrently). The "Markets the bot is watching" table
shows every live market, the side/price a rule would buy, and why other markets
don't match; it also previews matches while trading is paused.

The existing first-run default rule (`Coin price edge`) buys the side with the
larger after-fee edge when that edge is at least 3 cents, at prices 0.03-0.97
with 4-14 minutes left. It allows up to five buys, 60 seconds apart, and holds
to settlement. The new conservative Practice preset is a separate opt-in
experiment, not a replacement for saved/default rules.
**Rules that ignore edge (blank Min edge, Always YES/NO)
trade more often but can lose money steadily after fees — test them in Paper
mode first.** Older saved settings (budget/take profit/stop loss only) migrate
automatically to the existing first-run default rule.

Environment values seed the first-run default rule only:

```dotenv
KALSHI_TRADE_BUDGET_USD=1.00
KALSHI_TAKE_PROFIT_USD=0
KALSHI_STOP_LOSS_USD=0
```

Dollar amounts use at most two decimals; the loss limit must be below the
budget. Amounts are **per trade**, not percentages or daily/session totals.
Repeated trades can cumulatively spend or lose much more than one budget.
No accuracy or profitability is guaranteed.

To set up:

1. Start with `KALSHI_ENV=demo` and demo API credentials. Paper trading works
   immediately; its switch enables without a confirmation dialog.
2. For live trading, set `KALSHI_ORDER_EXECUTION_ENABLED=true` and
   restart/redeploy the bot. Enabling **Live trading** in the dashboard always
   shows a confirmation with your exact saved settings and a risk acknowledgment.
3. Production uses `KALSHI_ENV=prod` and production credentials; the switch is
   labelled **Live trading — real money**. Switching credentials while journaled
   positions are open blocks their management; use a separate database for a
   different account, or resolve the original positions first. API-key rotation
   also requires resolving ownership.

The dashboard controls have **no login or token**. Anyone who can reach the
dashboard can change settings and enable live trading, so run it only on a
private, trusted network (or put authenticated HTTPS/VPN in front). Never
expose the port to the internet or an untrusted LAN.

The bot always restarts **paused** in both journeys, while retaining saved
settings and position journals. Off takes effect immediately for new entries,
but cannot recall an already submitted order. Existing positions continue to be
monitored for exits. Pause before editing settings. Positions retain their
entry-time thresholds.
If the server authorization flag is false, **all** live order execution,
including exits, stops. Do not disable that flag or stop the process while
relying on exits. Compose already reads these variables from `.env`.

When enabled, the policy triggers an exit at net profit **at or above** the
profit target, or net loss **at or above** the loss limit; a threshold of 0 is
off. Entry sizing reserves fees. With a stop loss set, entries are skipped when
the quoted spread and fee reserve already reach the loss limit; with any exit
set, entries need initial sell liquidity. Net P/L uses confirmed fill
costs/fees and available sell liquidity, not the underlying crypto price.
Entries use the fresh live market feed (reads older than 10 seconds or with
degraded inputs are never traded); historical or Test Lab decisions never
trigger trades. One open bot position per coin, one entry per market.
The worker refuses to mix an entry with existing holdings/resting orders in that
market. Avoid manual trading in bot-managed markets; a holdings mismatch pauses
automation rather than risking unrelated holdings. Only ordinary $1 binary
contracts with supported general fee schedules are eligible.

Orders are price-limited and immediate-or-cancel; exits are reduce-only. Partial
fills are tracked. Orders are journaled before submission, and uncertain outcomes
block new entries instead of being blindly retried. A lost response with no
confirmed fills may need manual reconciliation in Kalshi; do not delete the
journal to bypass this block. Database worker ownership prevents overlapping
workers from submitting duplicate orders. Use one bot process per database.

**A stop-loss cannot cap losses or guarantee a cash-out price.** Liquidity, price
gaps, fees, outages, rejected orders, and market closure can prevent an exit or
cause larger losses. Stop-losses attempt available partial liquidity; unavailable
liquidity is recorded rather than displayed as a guaranteed executable P/L.
The worker must stay running to monitor thresholds. If a position remains through
market close, it is tracked until final settlement. Tests use mocked Kalshi
responses; authenticated demo/production execution has not been verified here.

## 📍 Where we are right now

### Coin-price fair value + scale-in (v0.9.0, decision model `fair-value-v1`)

The market-recal model only looked at the Kalshi price, so it could never know
more than the market. Research on 5,694 settled markets (30 days of 1-minute
Kalshi quotes plus Coinbase prices) found the real, repeatable edge: **Kalshi
prices react slowly to coin moves**. When BTC/SOL moves away from the strike,
the YES price takes seconds to a minute to catch up.

`prediction/fair_value.py` measures how far the index is from the strike in
"expected remaining moves" (z = distance / (price x 1-minute volatility x
sqrt(minutes left))), turns that into a probability, and blends it with the
market price:

```
logit P = c0 + c1*L(mid) + c2*L(Phi(z)) + c3*L(Phi(z))*is_btc
            + c4*L(mid)*m/15 + c5*L(Phi(z))*m/15      # m = minutes left
```

The coin's weight grows with time left, because that's where the market
under-reacts the most. Evidence (all after taker fees, buying at the ask):

| Test | Bets | Avg profit / contract | Win rate |
|---|---|---|---|
| Walk-forward by day, 1-min data, edge >= 2c, 4-14 min left, one buy per minute | 2,090 | +4.3c (t = 5.2) | -- |
| Same, but with a 1-minute-stale coin price | -- | ~0 (edge disappears) | -- |
| Frozen coefficients, 179 later markets replayed tick by tick, edge >= 3c, up to 5 buys 60s apart, 2s delay | 190 | +11.3c (t = 4.7) | 86% |
| Same with a 10s execution delay | ~190 | +10.8c | -- |

Things that **lost money** in the same tests and are deliberately not used:
buying on price alone, scalping (take-profit / sell-at-fair-value; wins often
but spread + two fees eat it), and the old momentum confirmation.

How the live bot uses it:
- A 31-minute index buffer (re-seeded from the database on restart) feeds the
  fair value every 2 seconds; with too little history or under 1 minute left
  it falls back to `market-recal`.
- The default rule *Coin price edge* buys whichever side is at least 3c cheap
  after fees with 4-14 minutes left, and **adds to the position up to 5 times
  (60s apart) while the edge is still there**, then holds to settlement.
  `max_entries` / `reentry_gap_sec` are per-rule ("Buys per market" / "Wait
  between buys" in the Trading tab). Total exposure per market is at most
  budget x buys.
- With side "Follow the bot's guess" the bot now buys the side with the larger
  after-fee edge, not just the side above 50%.
- `KALSHI_DAILY_LOSS_LIMIT_USD` pauses new bets for the day after a set loss.

**Caveats:** the tick-level check covers only ~2 days, fills were simulated at
the top of the book, and Coinbase stood in for BRTI in the 30-day fit. This is
a speed edge: if Kalshi market makers get faster it will shrink. Run it in
Practice mode for a few hundred bets before using real money.

### Market-anchored recalibration (v0.8.0, decision model `market-recal-v1`)

After 300+ settled markets, a walk-forward review (fit on the past, test on
the next unseen markets, ~2,500 test markets) showed the blended model's
Brier score was **worse than the market price alone**, which is why the old
pipeline said NO_EDGE ~88% of the time. The only variant that beat the market
after taker fees was a Platt recalibration of the market price itself:

```
P(yes) = sigmoid(a + b * logit(market mid))   # fitted a ≈ 0.03, b ≈ 1.11
```

`b > 1` captures the favorite–longshot bias in these markets (favorites win a
bit more often than their price implies). In walk-forward testing, following
it whenever after-fee edge ≥ 0 made +$17.36 over 833 one-contract trades
(about +2¢/contract, 86% win rate), positive in both halves of the history and
on both BTC and SOL (bootstrap P(profit > 0) ≈ 0.97). Adding the model's own
features, per-coin fits, or isotonic curves all did worse, and the old
confirmation gate reduced profit, so it is now off by default.

The live bot refits this recalibrator from settled decision snapshots every
calibration cycle once `KALSHI_MARKET_RECAL_MIN_SAMPLES` (default 300) clean
samples exist; until then the previous calibrated model is used. Decision
snapshots record `decision_model`, the coefficients, and the edge threshold so
results stay auditable. **The edge is thin**: it can disappear with fee or
market changes, so watch Paper results before committing real money.

The previous blended model is still computed and recorded as the
pre-calibration probability. With `v2` selected, new locked decisions
also record a shadow model with its order-book drift weight set to zero.
The shadow model never changes the live recommendation or places an order.
No sample count guarantees accuracy or profitability.

### Shadow comparison

After deploying this version, each new decision records both models on
the same inputs and timestamp, including abstentions. The momentum weight,
settlement formula, decision timing, edge threshold, and confirmation rules
stay the same. Book imbalance remains part of the confirmation gate for
both models; only the challenger's probability-model tilt is removed.

### External crypto prices

The bot now connects to Coinbase's public Advanced Trade WebSocket and Kraken's
public v2 WebSocket for BTC and SOL. It stores source ticks in `external_ticks` and adds a
fresh aggregate to each locked `decision_snapshots` record:

- median reference price
- independent source count, sample count, and source names
- maximum receive age
- cross-source price dispersion
- bid/ask and 24-hour volume for the raw tick

This is **shadow-only**. External prices do not currently alter v2 probabilities
or live recommendations. Coinbase is not CF Benchmarks RTI, so using it as a
direct replacement would introduce basis risk. The collector reconnects after
feed failures and records receive time separately from the exchange event time.
Treat `source_count < 2`, stale age, or high dispersion as unavailable external
features. The next step is adding at least one independent exchange and testing
an external-feature challenger on chronological holdouts before allowing it to
influence a signal.

External WebSocket collectors do not write directly to the bot database. They
place at most one tick per symbol per second into a bounded in-memory queue;
a worker thread commits queued ticks in batches. This prevents a burst from an
external exchange from blocking the dashboard or Kalshi processing. If the
queue fills, excess external shadow samples are dropped and logged rather than
delaying the trading-signal path. Persisted feed status therefore reflects the
latest successfully stored data, not a guarantee that every exchange update was
retained.

### Shadow Lab

The React dashboard's **Shadow Lab** tab (also available directly at `/shadow`)
receives continuously refreshed
candidate forecasts from `/api/shadow-active`, computed on the same clean live
inputs as the primary model. Its card layout mirrors the live dashboard but
shows the regularized shadow model's probability, candidate recommendation,
market probability, edge, confirmation count, and current time remaining.
The page's development warning is permanent: Shadow Lab forecasts must not be
used to place trades. Locked `shadow_decisions` remain the separate historical
record used for calibration and comparison after settlement.
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
3. Run the API and bot:
   ```
   python -m kalshi_bot.main
   ```
   Keep this terminal window open — closing it stops the bot.
4. In a second terminal, run the React development server:
  ```
  cd frontend
  npm ci
  npm run dev
  ```
5. Open **http://127.0.0.1:5173** in your browser. Vite proxies API and
  WebSocket traffic to the Python service on port 8000.

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

Each card has a **🐋 Whale Tracker** button. On desktop it opens within the
market card; on mobile it opens as a bottom sheet containing the largest recent
fills for that market. Use the quick filter preset chips
(`$50+`, `$100+`, `$250+`, `$500+`, `$1,000+`) or type a custom minimum dollar
amount and press **Apply** to surface the latest trades meeting that threshold.
The threshold is remembered in your browser. Use **Back to signal**, the close
button, or Escape to return to the market decision.

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
| `KALSHI_EDGE_THRESHOLD` | Minimum model probability minus purchase price, before fees/slippage (legacy model) |
| `KALSHI_DECISION_MODEL` | `fair-value` (default: coin price vs strike blended with the market price), `market-recal`, or `legacy` |
| `KALSHI_DAILY_LOSS_LIMIT_USD` | Pause new bets for the rest of the UTC day after losing this much that day (default `0` = off) |
| `KALSHI_MARKET_RECAL_MIN_SAMPLES` | Clean settled decisions required before the recalibrator is used (default `300`) |
| `KALSHI_MARKET_RECAL_WINDOW` | Most recent settled decisions used to fit it (default `5000`) |
| `KALSHI_MARKET_RECAL_EDGE_THRESHOLD` | Edge threshold used with the recalibrator (default `0`; fees are already deducted) |
| `KALSHI_CONFIRMATION_GATE` | `true` re-enables the momentum/book confirmation gate on locked decisions (default `false`) |
| `KALSHI_TRADE_BUDGET_USD` / `KALSHI_TAKE_PROFIT_USD` / `KALSHI_STOP_LOSS_USD` | First-run default rule only (budget ≤ $25; 0 = exit off) |
| `KALSHI_FEE_MULTIPLIER` | Kalshi taker-fee multiplier used in `ceil(M * 0.07 * P * (1-P) * 100) / 100` (default `1`) |
| `KALSHI_SLIPPAGE_PER_CONTRACT` | Conservative slippage in dollars deducted from executable edge (default `0`) |
| `KALSHI_PREDICTOR_VERSION` | `v1` (simple) or `v2` (default, settlement-aware; accuracy must be validated) |
| `KALSHI_MIN_INDEX_HISTORY_SEC` | Minimum index history span before a signal is allowed (default `240`) |
| `KALSHI_MIN_INDEX_HISTORY_TICKS` | Minimum index observations before a signal is allowed (default `120`) |
| `KALSHI_MAX_INPUT_AGE_MS` | Maximum quote/index age before the bot abstains (default `5000`) |
| `KALSHI_MIN_QUOTE_SIZE` | Minimum YES bid and ask size required for a signal (default `1`) |
| `KALSHI_CLOSED_GRACE_SEC` | How long a just-closed market stays on the Active tab |
| `KALSHI_DECISION_LEAD_SEC` | How early (in seconds) the final BET UP/DOWN call locks in |
| `KALSHI_LLM_BASE_URL` | Local llama.cpp URL for the live risk review (default `http://192.168.1.229:8080`) |
| `KALSHI_LLM_MODEL` | Optional llama.cpp model ID; blank discovers the first `/v1/models` entry |
| `KALSHI_LLM_TIMEOUT_SEC` | Maximum seconds to wait for one local risk review |
| `KALSHI_WHALE_MIN_USD` | Minimum dollar size of a trade to count as a "big bet" |
| `KALSHI_WHALE_POLL_INTERVAL_SEC` | How often to check for new big bets |
| `DATABASE_URL` | A file path = SQLite; a `postgresql://` link = Postgres (Docker sets this automatically) |
| `KALSHI_DASHBOARD_HOST` / `KALSHI_DASHBOARD_PORT` | Where the dashboard is served |
| `DISCORD_TRADE_WEBHOOK_URL` | Discord webhook for the **trades** channel: a message each time a real-money buy fills (unset = off) |
| `DISCORD_SETTLEMENT_WEBHOOK_URL` | Discord webhook for the **results** channel: win/loss and P&L when a position settles or exits (unset = off) |
| `DISCORD_NOTIFY_PAPER` | Also announce paper trades in the same channels, titled `PAPER` (default `true`; set `false` once live trading is on) |

### Discord notifications

Live and paper trades are both announced; paper messages are titled `PAPER`. In production the two webhook
URLs are Jenkins **Secret text** credentials with the IDs `discord-trade-webhook-url` and
`discord-settlement-webhook-url`; the Deploy stage passes them to the bot container, so they never
touch the repo or the image. For local runs, put them in `.env`. Sending is fire-and-forget with a
short timeout, so a Discord outage can never block or pause trading.

## Project layout (for developers)

- `src/kalshi_bot/auth.py` — signs requests to Kalshi's API.
- `src/kalshi_bot/kalshi_client/` — talks to Kalshi over REST and WebSocket.
- `src/kalshi_bot/market_discovery.py` — finds the currently open 15-minute markets.
- `src/kalshi_bot/data/store.py` — saves everything (SQLite or Postgres).
- `src/kalshi_bot/features/engine.py` — turns raw prices into volatility/momentum numbers.
- `src/kalshi_bot/prediction/model.py` — the actual v1/v2 prediction math.
- `src/kalshi_bot/prediction/market_recal.py` — market-anchored Platt recalibration used for live decisions.
- `src/kalshi_bot/trading.py` / `auto_trader.py` — entry rules, sizing/exits, and the auto-trading worker.
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
