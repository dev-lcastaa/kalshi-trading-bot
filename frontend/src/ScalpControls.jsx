import React from "react";

const MARKET_SECONDS = 900;
const DEFAULT_START = 60;
const MIN_SECONDS_LEFT = 90;
// A few cents either way; the stop needs ~2 cents of room beyond the spread and fees to avoid instant stop-outs.
const DEFAULT_TAKE_PROFIT = 0.03;
const DEFAULT_STOP_LOSS = 0.05;

export const COINS = ["BTC", "SOL"];
const DEFAULT_MAX_PRICE = 95;

function coinForm(rule) {
  return {
    enabled: rule ? Boolean(rule.enabled) : true,
    confidence: rule?.side === "momentum" ? String(Math.round(Number(rule.min_confidence) * 100)) : "65",
    budget: Number(rule?.budget ?? 1).toFixed(2),
    take_profit: Number(rule?.side === "momentum" && rule.take_profit > 0 ? rule.take_profit : DEFAULT_TAKE_PROFIT).toFixed(2),
    stop_loss: Number(rule?.stop_loss > 0 ? rule.stop_loss : Math.min(DEFAULT_STOP_LOSS, Number(rule?.budget ?? 1) / 5)).toFixed(2),
    max_cycles: String(rule?.max_cycles ?? 3),
    start_after: String(MARKET_SECONDS - Number(rule?.max_seconds_left ?? MARKET_SECONDS - DEFAULT_START)),
    max_price: String(rule?.max_price != null ? Math.round(Number(rule.max_price) * 100) : DEFAULT_MAX_PRICE),
    require_llm_allow: Boolean(rule?.require_llm_allow),
  };
}

// One form per coin; a saved "ANY" rule (the older single-strategy setup) seeds every coin.
// A coin with no rule of its own starts off when other coins already have rules.
export function scalpForm(settings) {
  const rules = settings?.rules ?? [];
  const shared = rules.find((rule) => rule.coin === "ANY");
  return Object.fromEntries(COINS.map((coin) => {
    const rule = rules.find((r) => r.coin === coin) ?? shared;
    return [coin, rule || !rules.length ? coinForm(rule) : { ...coinForm(undefined), enabled: false }];
  }));
}

function coinRule(coin, form) {
  const budget = Number(form.budget);
  const cycles = Number(form.max_cycles ?? 3);
  const rule = {
    name: `${coin} scalp`, enabled: Boolean(form.enabled), coin, side: "momentum",
    min_price: "0.05", max_price: (Number(form.max_price ?? DEFAULT_MAX_PRICE) / 100).toFixed(2), min_confidence: (Number(form.confidence) / 100).toFixed(2),
    min_edge: null, min_seconds_left: MIN_SECONDS_LEFT, max_seconds_left: MARKET_SECONDS - Number(form.start_after ?? DEFAULT_START),
    budget: budget.toFixed(2), take_profit: Number(form.take_profit ?? 0.02).toFixed(2), stop_loss: Number(form.stop_loss).toFixed(2),
    max_entries: 1, reentry_gap_sec: 60, scalping: true, max_cycles: cycles, cycle_cooldown_sec: 30,
    market_spend_limit: Math.min(25, budget * cycles).toFixed(2),
    market_loss_limit: Number(form.stop_loss).toFixed(2),
  };
  return form.require_llm_allow ? { ...rule, require_llm_allow: true } : rule;
}

export function scalpSettings(form) {
  const budget = Math.max(...COINS.map((coin) => Number(form[coin].budget)));
  return {
    rules: COINS.map((coin) => coinRule(coin, form[coin])),
    risk: {
      daily_loss_limit: (budget * 3).toFixed(2), max_open_cost: (budget * 2).toFixed(2),
      max_open_positions: 2, min_edge: "0.00", uncertainty_buffer: "0.00", max_spread: "0.03",
      max_signal_age_ms: 5000, max_book_age_ms: 2000,
      require_reference_agreement: false, require_fair_value: false,
    },
  };
}

function validateCoin(form) {
  if (!/^\d+$/.test(form.confidence) || Number(form.confidence) < 50 || Number(form.confidence) > 99) {
    return "Confidence must be a whole percentage from 50 to 99.";
  }
  for (const key of ["budget", "stop_loss", ...(form.take_profit === undefined ? [] : ["take_profit"])]) {
    if (!/^\d+(\.\d{1,2})?$/.test(form[key])) return "Enter money in dollars with at most two decimals.";
  }
  if (form.take_profit !== undefined && (Number(form.take_profit) <= 0 || Number(form.take_profit) >= Number(form.budget))) return "Profit target must be positive and less than the amount per trade.";
  if (Number(form.budget) <= 0 || Number(form.budget) > 25) return "Amount per trade must be more than $0 and no more than $25.";
  if (Number(form.stop_loss) <= 0 || Number(form.stop_loss) >= Number(form.budget)) return "Stop loss must be positive and less than the amount per trade.";
  const cycles = String(form.max_cycles ?? 3);
  if (!/^\d+$/.test(cycles) || Number(cycles) < 1 || Number(cycles) > 10) return "Trades per 15-minute market must be a whole number from 1 to 10.";
  const startAfter = String(form.start_after ?? DEFAULT_START);
  if (!/^\d+$/.test(startAfter) || Number(startAfter) > MARKET_SECONDS - MIN_SECONDS_LEFT) return `Start time must be whole seconds into the market, from 0 to ${MARKET_SECONDS - MIN_SECONDS_LEFT} (360 = the 6 minute mark).`;
  const maxPrice = String(form.max_price ?? DEFAULT_MAX_PRICE);
  if (!/^\d+$/.test(maxPrice) || Number(maxPrice) < 50 || Number(maxPrice) > 99) return "Highest entry price must be whole cents from 50 to 99.";
  return "";
}

export function validateScalp(form) {
  if (!COINS.some((coin) => form[coin].enabled)) return "Turn on at least one coin.";
  for (const coin of COINS) {
    const problem = validateCoin(form[coin]);
    if (problem) return `${coin}: ${problem}`;
  }
  return "";
}

const FIELDS = [
  ["confidence", "Confidence level (%)", "numeric"],
  ["budget", "Amount per trade ($)", "decimal"],
  ["take_profit", "Profit target ($)", "decimal"],
  ["stop_loss", "Stop loss amount ($)", "decimal"],
  ["max_cycles", "Trades per 15-minute market", "numeric"],
  ["start_after", "Start trading after (seconds into market)", "numeric"],
  ["max_price", "Highest entry price (cents)", "numeric"],
];

function CoinControls({ coin, form, onChange }) {
  return <fieldset className="trading-rule">
    <legend><label><input type="checkbox" aria-label={`Trade ${coin}`} checked={form.enabled} onChange={(event) => onChange({ ...form, enabled: event.target.checked })} /> {coin} scalping {form.enabled ? "on" : "off"}</label></legend>
    <div className="trading-fields">{FIELDS.map(([key, label, inputMode]) => <label key={key}>
      {label}<input type="text" inputMode={inputMode} aria-label={`${coin} ${label}`} value={form[key]} disabled={!form.enabled} onChange={(event) => onChange({ ...form, [key]: event.target.value })} />
    </label>)}</div>
    <label><input type="checkbox" aria-label={`${coin} LLM must approve`} checked={form.require_llm_allow} disabled={!form.enabled} onChange={(event) => onChange({ ...form, require_llm_allow: event.target.checked })} /> Only trade when the 6:30 LLM risk review says ALLOW</label>
  </fieldset>;
}

export default function ScalpControls({ form, disabled, onChange }) {
  return <fieldset className="trading-rule" disabled={disabled}>
    <legend>Scalping settings</legend>
    {COINS.map((coin) => <CoinControls key={coin} coin={coin} form={form[coin]} onChange={(next) => onChange({ ...form, [coin]: next })} />)}
    <p className="trading-muted">Each coin has its own settings. The bot follows UP or DOWN movement. Confidence is the market's win estimate, not a profit guarantee.</p>
    <p className="trading-muted">Amount includes buy fees. Profit target and stop loss are net dollars after fees. The bot skips trades whose spread and fees leave under 2 cents of room before the stop loss. A stop loss triggers a sale, but losses can exceed it. The LLM option skips trades the risk review objected to or could not check.</p>
    <details className="trading-decision"><summary>Automatic safety limits</summary>
      <p className="trading-muted">Your trade limit counts buy/sell cycles in each individual 15-minute market. Spending is capped at the smaller of $25 or your trade amount times that limit. Wait 30 seconds after a profitable exit; no re-entry after a loss. At most 2 open trades across all coins; daily loss budget is 3 times the largest coin trade amount, including open trades. Entry prices: from 5 cents up to each coin's highest entry price, starting at your chosen second of the market and until 90 seconds are left, and at most a 3-cent spread. Existing markets keep their original caps. Fees, liquidity, and these limits may prevent a trade.</p>
    </details>
  </fieldset>;
}
