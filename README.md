# Kalshi 15-Min Crypto Prediction Bot

Signal-only bot that monitors Kalshi's 15-minute BTC/SOL markets and their true
settlement price feeds (CF Benchmarks `BRTI` / `SOLUSD_RTI`), predicts P(yes),
and surfaces edges vs the market-implied probability on a local dashboard.

**This bot never places, amends, or cancels orders.** It is a monitoring and
prediction tool only — every trading decision is yours.

## Quick start

### Option A: Docker + PostgreSQL (recommended for anything long-running)

1. Copy `.env.example` to `.env` and fill in your Kalshi API key ID + private
   key path (path relative to the repo, e.g. `./.secrets/kalshi_private_key.pem`
   — the whole `.secrets/` folder is mounted read-only into the container).
   **Demo and production keys are separate** — make sure `KALSHI_ENV` matches
   which account the key was created on, or every request will fail with a `401`.
2. `docker compose up --build`
3. Open the dashboard at `http://localhost:8000`.

This starts a PostgreSQL container alongside the bot and points `DATABASE_URL`
at it automatically (overriding whatever's in `.env`) — no local Python or
Postgres install needed. Data persists in the `postgres-data` Docker volume
across restarts (`docker compose down` keeps it; `docker compose down -v`
wipes it).

### Option B: Local Python (SQLite, no Docker)

1. `python -m venv .venv && .venv\Scripts\activate` (Windows)
2. `pip install -e .[dev]`
3. Copy `.env.example` to `.env` as above. Leave `DATABASE_URL` as the default
   local file path — no Postgres needed for this path.
4. Run `python -m kalshi_bot.main` and leave the terminal open (closing it
   stops the bot).
5. Open the dashboard at `http://127.0.0.1:8000`.

## Using the dashboard

**Calibration line** (top of page) — rolling Brier score for the model vs.
the market vs. a naive 50% baseline, computed from every settled market so
far. See [How accurate is it?](#how-accurate-is-it) for how to read it.

**Active Markets tab** — one card per currently open 15-min BTC/SOL market:
- **ABOVE / BELOW** — the model's call on whether the settlement price will
  finish above or below the strike, with a confidence percentage.
- **Trade banner** — the one-shot, locked-in BET UP / BET DOWN / NO TRADE call
  made `KALSHI_DECISION_LEAD_SEC` before close, gated by a multi-signal
  confirmation check (see [How accurate is it?](#how-accurate-is-it)). This is
  distinct from the continuously-fluctuating ABOVE/BELOW indicator above it.
- **Sparkline chart** — the last 10 minutes of live index price, with a dashed
  line at the strike; colored green/red to match the current side of strike.
- **Our prediction / Market odds bars** — the model's estimated probability
  vs. the probability implied by the market's own bid/ask, with the market's
  value marked on the model's bar so you can see the gap at a glance.
- **Signal badge** — `Yes looks cheap` / `No looks cheap` when the gap exceeds
  `KALSHI_EDGE_THRESHOLD`, otherwise `Priced fairly`. This is informational
  only; nothing is ever submitted to Kalshi automatically.
- **Countdown** — time left until the market closes, turning amber under a
  minute.

Markets stay on this tab, dimmed, for `KALSHI_CLOSED_GRACE_SEC` seconds after
they close, then move to:

**Closed Markets tab** — full history of past markets with their last known
prediction before close, plus (once Kalshi reports a settlement result) a
"Settled ABOVE/BELOW — model was correct/wrong" line on each card.


## How the model works

Two predictors are available (`src/kalshi_bot/prediction/model.py`), selected
via `KALSHI_PREDICTOR_VERSION`:

- **v1 — `RandomWalkPredictor`**: treats the index price as a geometric
  random walk (GBM) and computes P(price > strike at close) from realized
  volatility, a simple 2-point momentum estimate, and time-to-expiry.
- **v2 — `SettlementAwarePredictor`** *(default)*: same foundation, plus three
  refinements:
  1. **Settlement-window awareness** — Kalshi actually settles these markets
     on the *average of the last 60 one-second ticks* before close, not the
     terminal price (confirmed in KXBTC15M/KXSOL15M's own product metadata).
     v2 models that average directly, which meaningfully lowers variance
     (and sharpens the call) in the final minute — the part of the contract
     that matters most.
  2. **OLS momentum** instead of a 2-point slope, so a single noisy tick
     can't flip the estimated drift.
  3. **Order-book imbalance** as a small secondary tilt (computed, but unused
     by v1).

  Set `KALSHI_PREDICTOR_VERSION=v1` to roll back instantly if you want the
  simpler baseline instead.

## How accurate is it?

**Short answer: check the calibration line at the top of the dashboard —
don't just trust the model.** Here's exactly what is and isn't being
measured:

- ✅ **Live calibration loop.** Every ~60s the bot checks Kalshi for the
  settlement result (`yes`/`no`) of any market that closed in the last 24h
  but hasn't been resolved yet (`_poll_pending_outcomes` in `main.py`), and
  records it. Once a market has a recorded result, it's included in the
  rolling `GET /api/calibration` stats: Brier score and log loss for the
  **locked-in decision** (see below) on that market, the market's own implied
  probability, and a naive 50% baseline — all computed from the exact same
  settled markets, so they're directly comparable. This is shown live at the
  top of the dashboard, and each Closed Markets card shows whether the model
  was right or wrong once its market settles.
- ✅ **Multi-signal confirmation gates the locked decision.** The BET UP/DOWN
  call isn't just the raw model output — before locking it in, independent
  momentum (short and long window) and order-book-imbalance signals are voted
  against the model's direction. If a majority disagree, the decision
  downgrades to `NO TRADE` instead of a shaky directional call (see
  `src/kalshi_bot/signals/confirmation.py`). The banner shows e.g.
  "2/3 checks agreed" for transparency.
- ✅ **Regression-guarded on synthetic data.** `tests/test_backtest.py` runs
  both predictor versions over identical synthetic 15-minute price paths and
  asserts v2's Brier score is no worse than v1's. This only guards against
  *regressions* when the model changes — it's separate from, and no
  substitute for, the live calibration numbers above.
- ✅ **The settlement mechanics are verified**, not assumed: the CF
  Benchmarks-average-of-last-60-ticks settlement rule was confirmed directly
  from Kalshi's own `KXBTC15M`/`KXSOL15M` series metadata, and the model's
  math for it (variance of a Brownian-motion time-average) is a standard,
  citable result (used in Asian option pricing), not a heuristic guess.
- ⚠️ **Sample size starts at zero.** The calibration panel reads "waiting for
  settled markets" until the bot has been running long enough for at least
  one 15-minute market to close and settle, and any early numbers will be
  noisy until dozens+ have accumulated (BTC + SOL together settle ~192
  markets/day, so a meaningful sample builds up within a day or so of
  continuous runtime). As a rule of thumb, don't draw conclusions before
  ~100-150 settled decisions, and treat it as a real signal only after
  ~300+ with the model consistently beating the market's own Brier score.
- ❌ **No claim of edge over the market.** The `Yes looks cheap` / `No looks
  cheap` badge just means the model disagrees with the market's own price by
  more than `KALSHI_EDGE_THRESHOLD` — the calibration panel tells you whether
  that disagreement has actually been paying off, but this is still not a
  backtested trading strategy.

Lower Brier score / log loss is better. If the model's Brier score is
consistently at or above the market's, the model isn't adding value over
just using the market's own price as your probability estimate.

## Configuration

All settings are read from `.env` (see `.env.example`):

| Variable | Purpose |
|---|---|
| `KALSHI_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH` | API credentials |
| `KALSHI_ENV` | `demo` or `prod` — must match where the key was generated |
| `KALSHI_INDEX_IDS` | CF Benchmarks index IDs to track (default `BRTI,SOLUSD_RTI`) |
| `KALSHI_COIN_TICKS` | Coin tags used to find the 15-min series (default `BTC,SOL`) |
| `KALSHI_POLL_INTERVAL_SEC` | How often to recompute features/predictions/signals |
| `KALSHI_EDGE_THRESHOLD` | Minimum \|model_p − market_p\| to flag a signal |
| `KALSHI_PREDICTOR_VERSION` | `v1` or `v2` (default) — see [How the model works](#how-the-model-works) |
| `KALSHI_CLOSED_GRACE_SEC` | How long (seconds) a closed market stays visible on the Active tab before moving to Closed Markets only (default `10`) |
| `KALSHI_DECISION_LEAD_SEC` | Seconds before close at which the one-shot BET UP/DOWN call locks in (default `390` = 6.5 min) |
| `DATABASE_URL` | A plain path uses SQLite (local dev); a `postgresql://` URL uses Postgres (set automatically by `docker-compose.yml`) |
| `KALSHI_DASHBOARD_HOST` / `KALSHI_DASHBOARD_PORT` | Dashboard bind address |

## Layout

- `src/kalshi_bot/auth.py` - RSA-PSS request signing for Kalshi API.
- `src/kalshi_bot/kalshi_client/` - REST + WebSocket clients and message models.
- `src/kalshi_bot/market_discovery.py` - finds live 15-minute BTC/SOL markets.
- `src/kalshi_bot/data/store.py` - tick/signal/market-lifecycle storage (SQLite or Postgres).
- `src/kalshi_bot/features/engine.py` - realized vol, momentum, time-to-expiry, etc.
- `src/kalshi_bot/prediction/model.py` - v1/v2 probability models.
- `src/kalshi_bot/signals/generator.py` - compares model vs market probability.
- `src/kalshi_bot/signals/confirmation.py` - multi-signal veto check for locked decisions.
- `src/kalshi_bot/backtest/runner.py` - offline calibration check (Brier/log loss).
- `src/kalshi_bot/dashboard/` - FastAPI live dashboard (Active/Closed tabs, sparklines).
- `src/kalshi_bot/main.py` - orchestrator entrypoint.
- `Dockerfile` / `docker-compose.yml` - containerized deployment with Postgres.

## Tests

`pytest` — includes model sanity checks, the v1/v2 regression guard, and
store/dashboard-query tests.

