import React from "react";

export function scalpForm(settings) {
  const rule = settings?.rules?.[0];
  return {
    confidence: rule?.side === "momentum" ? String(Math.round(Number(rule.min_confidence) * 100)) : "65",
    budget: Number(rule?.budget ?? 1).toFixed(2),
    stop_loss: Number(rule?.stop_loss > 0 ? rule.stop_loss : Math.min(0.20, Number(rule?.budget ?? 1) / 5)).toFixed(2),
  };
}

export function scalpSettings(form) {
  const budget = Number(form.budget);
  return {
    rules: [{
      name: "Momentum scalp", enabled: true, coin: "ANY", side: "momentum",
      min_price: "0.05", max_price: "0.95", min_confidence: (Number(form.confidence) / 100).toFixed(2),
      min_edge: null, min_seconds_left: 90, max_seconds_left: 840,
      budget: budget.toFixed(2), take_profit: "0.02", stop_loss: Number(form.stop_loss).toFixed(2),
      max_entries: 1, reentry_gap_sec: 60, scalping: true, max_cycles: 3, cycle_cooldown_sec: 30,
      market_spend_limit: Math.min(25, budget * 3).toFixed(2),
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
  return "";
}

export default function ScalpControls({ form, disabled, onChange }) {
  const fields = [
    ["confidence", "Confidence level (%)", "numeric"],
    ["budget", "Amount per trade ($)", "decimal"],
    ["stop_loss", "Stop loss amount ($)", "decimal"],
  ];
  return <fieldset className="trading-rule" disabled={disabled}>
    <legend>Scalping settings</legend>
    <div className="trading-fields">{fields.map(([key, label, inputMode]) => <label key={key}>
      {label}<input type="text" inputMode={inputMode} value={form[key]} onChange={(event) => onChange({ ...form, [key]: event.target.value })} />
    </label>)}</div>
    <p className="trading-muted">Confidence is the moving side's market-implied chance of winning at settlement, not a probability of scalp profit. The bot buys UP or DOWN only when recent quotes and short-term coin movement agree.</p>
    <p className="trading-muted">Amount includes reserved buy fees. Automatic cash-out target: $0.02 net per trade after costs and reserved sell fees. Stop loss is a sell trigger, not a guaranteed maximum loss; an illiquid position can lose its full cost.</p>
    <details className="trading-decision"><summary>Automatic safety limits</summary>
      <p className="trading-muted">At most 3 cycles per market, total spending capped at the smaller of $25 or 3 times your trade amount, 30 seconds between profitable cycles, no re-entry after a loss, at most 2 open trades, and a daily loss budget of 3 times your trade amount (including money reserved for open trades). Entry prices: 5-95 cents with 90-840 seconds left and at most a 3-cent spread. High confidence thresholds may leave no qualifying prices. Fees and spreads can prevent a trade. No profit is guaranteed.</p>
    </details>
  </fieldset>;
}
