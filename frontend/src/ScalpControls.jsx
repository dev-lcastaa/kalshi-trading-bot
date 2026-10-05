import React from "react";

const toLocalInput = (ms) => {
  if (!ms) return "";
  const date = new Date(Number(ms));
  if (Number.isNaN(date.getTime())) return "";
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
};

export function scalpForm(settings) {
  const rule = settings?.rules?.[0];
  return {
    confidence: rule?.side === "momentum" ? String(Math.round(Number(rule.min_confidence) * 100)) : "65",
    budget: Number(rule?.budget ?? 1).toFixed(2),
    stop_loss: Number(rule?.stop_loss > 0 ? rule.stop_loss : Math.min(0.20, Number(rule?.budget ?? 1) / 5)).toFixed(2),
    max_cycles: String(rule?.max_cycles ?? 3),
    start_at: toLocalInput(settings?.start_at_ms),
  };
}

export function scalpSettings(form) {
  const budget = Number(form.budget);
  const cycles = Number(form.max_cycles ?? 3);
  return {
    start_at_ms: form.start_at ? new Date(form.start_at).getTime() : null,
    rules: [{
      name: "Momentum scalp", enabled: true, coin: "ANY", side: "momentum",
      min_price: "0.05", max_price: "0.95", min_confidence: (Number(form.confidence) / 100).toFixed(2),
      min_edge: null, min_seconds_left: 90, max_seconds_left: 840,
      budget: budget.toFixed(2), take_profit: "0.02", stop_loss: Number(form.stop_loss).toFixed(2),
      max_entries: 1, reentry_gap_sec: 60, scalping: true, max_cycles: cycles, cycle_cooldown_sec: 30,
      market_spend_limit: Math.min(25, budget * cycles).toFixed(2),
      market_loss_limit: Number(form.stop_loss).toFixed(2),
    }],
    risk: {
      daily_loss_limit: (budget * 3).toFixed(2), max_open_cost: (budget * 2).toFixed(2),
      max_open_positions: 2, min_edge: "0.00", uncertainty_buffer: "0.00", max_spread: "0.03",
      max_signal_age_ms: 5000, max_book_age_ms: 2000,
      require_reference_agreement: false, require_fair_value: false,
    },
  };
}

export function validateScalp(form) {
  if (!/^\d+$/.test(form.confidence) || Number(form.confidence) < 50 || Number(form.confidence) > 99) {
    return "Confidence must be a whole percentage from 50 to 99.";
  }
  for (const key of ["budget", "stop_loss"]) {
    if (!/^\d+(\.\d{1,2})?$/.test(form[key])) return "Enter money in dollars with at most two decimals.";
  }
  if (Number(form.budget) <= 0 || Number(form.budget) > 25) return "Amount per trade must be more than $0 and no more than $25.";
  if (Number(form.stop_loss) <= 0 || Number(form.stop_loss) >= Number(form.budget)) return "Stop loss must be positive and less than the amount per trade.";
  const cycles = String(form.max_cycles ?? 3);
  if (!/^\d+$/.test(cycles) || Number(cycles) < 1 || Number(cycles) > 10) return "Trades per 15-minute market must be a whole number from 1 to 10.";
  if (form.start_at && Number.isNaN(new Date(form.start_at).getTime())) return "Choose a valid start date and time, or clear it to start right away.";
  return "";
}

export default function ScalpControls({ form, disabled, onChange }) {
  const fields = [
    ["confidence", "Confidence level (%)", "numeric"],
    ["budget", "Amount per trade ($)", "decimal"],
    ["stop_loss", "Stop loss amount ($)", "decimal"],
    ["max_cycles", "Trades per 15-minute market", "numeric"],
  ];
  return <fieldset className="trading-rule" disabled={disabled}>
    <legend>Scalping settings</legend>
    <div className="trading-fields">{fields.map(([key, label, inputMode]) => <label key={key}>
      {label}<input type="text" inputMode={inputMode} value={form[key]} onChange={(event) => onChange({ ...form, [key]: event.target.value })} />
    </label>)}
      <label>Start trading at (optional)<input type="datetime-local" value={form.start_at ?? ""} onChange={(event) => onChange({ ...form, start_at: event.target.value })} /></label>
    </div>
    <p className="trading-muted">Leave the start time empty to trade as soon as the bot is turned on. If you set one, turn the bot on beforehand and it waits until then (in your browser's time zone). A time that has already passed starts right away.</p>
    <p className="trading-muted">The bot follows UP or DOWN movement. Confidence is the market's win estimate, not a profit guarantee.</p>
    <p className="trading-muted">Amount includes buy fees. Profit target: $0.02 after fees. Stop loss triggers a sale, but losses can exceed it.</p>
    <details className="trading-decision"><summary>Automatic safety limits</summary>
      <p className="trading-muted">Your trade limit counts buy/sell cycles in each individual 15-minute market, not different coins. Spending is capped at the smaller of $25 or your trade amount times that limit. Wait 30 seconds after a profitable exit; no re-entry after a loss. At most 2 open trades; daily loss budget is 3 times your trade amount, including open trades. Entry prices: 5-95 cents with 90-840 seconds left and at most a 3-cent spread. Existing markets keep their original caps. Fees, liquidity, and these limits may prevent a trade.</p>
    </details>
  </fieldset>;
}
