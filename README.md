# AQLabs — Kalshi 15-Minute Crypto Signal

A bot that watches Kalshi's 15-minute Bitcoin and Solana "above or below"
markets and tells you what it thinks will happen — as a percentage — before
each market closes.

**It only watches. It never buys, sells, or places any order for you.**
Every trade decision is 100% yours.

## 📍 Where we are right now

We just finished setting the bot up and it's running 24/7, collecting real
results. **We are not fine-tuning the prediction model yet** — we're
letting it run first so we have enough real, settled markets to know what
actually needs improving, instead of guessing.

Check back on the dashboard's track record panel over the next few days.
Once we have 100+ settled markets per coin, we'll come back and tune the
model using real evidence instead of assumptions.

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
- ⚠️ **Right now we have very little real data** — the bot just started
  running. A handful of settled markets tells you almost nothing; don't
  trust the track record panel until it has at least 100-150 settled
  markets per coin, and treat it as a real signal only after 300+.
- ❌ The bot beating its own confidence threshold does **not** mean it
  beats the market. The track record panel is what tells you whether it
  actually has, once there's enough data to say so.

## Settings (`.env`)

| Variable | What it controls |
|---|---|
| `KALSHI_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH` | Your Kalshi API credentials |
| `KALSHI_ENV` | `demo` or `prod` — must match the account your key belongs to |
| `KALSHI_INDEX_IDS` | Which price feeds to track (default `BRTI,SOLUSD_RTI`) |
| `KALSHI_COIN_TICKS` | Which coins to find markets for (default `BTC,SOL`) |
| `KALSHI_POLL_INTERVAL_SEC` | How often the bot recalculates its guess |
| `KALSHI_EDGE_THRESHOLD` | How big a gap vs. the market before it's flagged |
| `KALSHI_PREDICTOR_VERSION` | `v1` (simple) or `v2` (default, more accurate model) |
| `KALSHI_CLOSED_GRACE_SEC` | How long a just-closed market stays on the Active tab |
| `KALSHI_DECISION_LEAD_SEC` | How early (in seconds) the final BET UP/DOWN call locks in |
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

