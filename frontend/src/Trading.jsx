import React from "react";
import { AlertTriangle, Check, ChevronDown, Power, RefreshCw, Save, X } from "lucide-react";
import { friendlyCheckName } from "./utils";
import DirectionBars from "./DirectionBars";
import ScalpControls, { scalpForm, scalpSettings, validateScalp } from "./ScalpControls";

const MODES = [["paper", "Bot Simulation trading"], ["live", "Bot Real Trading"]];
export const MAX_BUDGET = 25;
export const MAX_RULES = 10;
const UI_KEYS = ["name", "enabled", "coin", "side", "min_price", "max_price", "min_confidence", "min_edge", "min_seconds_left", "max_seconds_left", "budget", "take_profit", "stop_loss", "max_entries", "reentry_gap_sec", "scalping", "max_cycles", "cycle_cooldown_sec", "market_spend_limit", "market_loss_limit"];
const RISK_DEFAULTS = { daily_loss_limit: "0.00", max_open_cost: "0.00", max_open_positions: "0", min_edge: "0", uncertainty_buffer: "0", max_spread: "0", max_signal_age_ms: "10000", max_book_age_ms: "2000", require_reference_agreement: false, require_fair_value: false };
const WHOLE = /^\d+$/;
const SIGNED_WHOLE = /^-?\d+$/;
const MONEY = /^\d+(\.\d{1,2})?$/;

const rulesOf = (settings) => Array.isArray(settings?.rules) ? settings.rules : [];
const formKey = (form) => JSON.stringify([rulesOf(form).map((rule) => UI_KEYS.map((key) => rule?.[key] == null ? "" : String(rule[key]))), { ...RISK_DEFAULTS, ...form?.risk }]);
const num = (value) => Number(value);
const centsOf = (dollars) => String(Math.round(num(dollars) * 100));
const timestamp = (value) => value == null ? "--" : new Date(value).toLocaleString();
const money = (value) => value == null || value === "" || Number.isNaN(num(value)) ? "--" : `$${Math.abs(num(value)).toFixed(2)}`;
const signedMoney = (value) => value == null || Number.isNaN(num(value)) ? "--" : `${num(value) > 0 ? "+" : num(value) < 0 ? "-" : ""}${money(value)}`;
const clock = (secs) => { const value = Math.max(0, Math.round(num(secs) || 0)); return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`; };
const direction = (side) => side === "no" ? "DOWN" : "UP";
const coinOf = (ticker = "") => /^KX([A-Z]+?)15M/.exec(ticker)?.[1] ?? ticker.split("-")[0];
const modeName = (mode) => mode === "paper" ? "Practice bot" : "Real-money bot";
export function tradingErrorMessage(detail, status) {
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const messages = detail.filter((item) => typeof item?.msg === "string").map((item) => {
      const path = Array.isArray(item.loc) ? item.loc.filter((part) => part !== "body").join(".") : "";
      return path ? `${path}: ${item.msg}` : item.msg;
    });
    if (messages.length) return messages.join("; ");
  }
  return `Request failed (${status})`;
}
const modeLabel = (mode, environment) => mode === "paper" ? "Practice mode — pretend money, nothing real is spent"
  : environment === "prod" ? "Real-money mode — uses your real Kalshi money" : "Real-money mode — Kalshi demo account (still not real money)";

export function toUi(settings) {
  const risk = settings?.risk;
  return { ...(risk ? { risk: Object.fromEntries(Object.keys(RISK_DEFAULTS).map((key) => [
    key, ["min_edge", "uncertainty_buffer", "max_spread"].includes(key) ? centsOf(risk[key] ?? 0)
      : key.startsWith("require_") ? Boolean(risk[key])
      : ["daily_loss_limit", "max_open_cost"].includes(key) ? num(risk[key] ?? 0).toFixed(2)
      : String(risk[key] ?? RISK_DEFAULTS[key]),
  ])) } : {}), rules: rulesOf(settings).map((rule) => ({
    name: String(rule.name ?? ""), enabled: Boolean(rule.enabled), coin: rule.coin ?? "ANY", side: rule.side ?? "model",
    min_price: centsOf(rule.min_price), max_price: centsOf(rule.max_price), min_confidence: centsOf(rule.min_confidence),
    min_edge: rule.min_edge == null || rule.min_edge === "" ? "" : centsOf(rule.min_edge),
    min_seconds_left: String(rule.min_seconds_left), max_seconds_left: String(rule.max_seconds_left),
    budget: num(rule.budget).toFixed(2), take_profit: num(rule.take_profit).toFixed(2), stop_loss: num(rule.stop_loss).toFixed(2),
    max_entries: String(rule.max_entries ?? 1), reentry_gap_sec: String(rule.reentry_gap_sec ?? 60),
    scalping: Boolean(rule.scalping), max_cycles: String(rule.max_cycles ?? 3),
    cycle_cooldown_sec: String(rule.cycle_cooldown_sec ?? 30),
    market_spend_limit: num(rule.market_spend_limit ?? 3).toFixed(2),
    market_loss_limit: num(rule.market_loss_limit ?? 0.5).toFixed(2),
  })) };
}

export function fromUi(form) {
  const dollars = (cents) => (num(cents) / 100).toFixed(2);
  return { ...(form.risk ? { risk: Object.fromEntries(Object.entries(form.risk).map(([key, value]) => [
    key, ["min_edge", "uncertainty_buffer", "max_spread"].includes(key) ? dollars(value)
      : key.startsWith("require_") ? Boolean(value)
      : ["daily_loss_limit", "max_open_cost"].includes(key) ? num(value).toFixed(2) : num(value),
  ])) } : {}), rules: rulesOf(form).map((rule) => ({
    name: String(rule.name).trim(), enabled: Boolean(rule.enabled), coin: rule.coin, side: rule.side,
    min_price: dollars(rule.min_price), max_price: dollars(rule.max_price), min_confidence: dollars(rule.min_confidence),
    min_edge: String(rule.min_edge ?? "").trim() === "" ? null : dollars(rule.min_edge),
    min_seconds_left: num(rule.min_seconds_left), max_seconds_left: num(rule.max_seconds_left),
    budget: num(rule.budget).toFixed(2), take_profit: num(rule.take_profit).toFixed(2), stop_loss: num(rule.stop_loss).toFixed(2),
    max_entries: num(rule.max_entries), reentry_gap_sec: num(rule.reentry_gap_sec),
    ...(rule.scalping ? { scalping: true, max_cycles: num(rule.max_cycles),
      cycle_cooldown_sec: num(rule.cycle_cooldown_sec),
      market_spend_limit: num(rule.market_spend_limit).toFixed(2),
      market_loss_limit: num(rule.market_loss_limit).toFixed(2) } : {}),
  })) };
}

export function validateRule(rule) {
  const text = (key) => String(rule[key] ?? "").trim();
  if (!text("name") || text("name").length > 40) return "Give the rule a name (up to 40 letters).";
  for (const key of ["min_price", "max_price"]) if (!WHOLE.test(text(key)) || num(text(key)) < 1 || num(text(key)) > 99) return "Prices must be whole cents from 1 to 99.";
  if (num(rule.min_price) > num(rule.max_price)) return "The lowest price can't be higher than the highest price.";
  if (!WHOLE.test(text("min_confidence")) || num(text("min_confidence")) > 100) return "\"How sure\" must be a whole number from 0 to 100.";
  if (text("min_edge") !== "" && (!SIGNED_WHOLE.test(text("min_edge")) || Math.abs(num(text("min_edge"))) > 100)) return "Minimum expected profit must be blank or whole cents from -100 to 100.";
  for (const key of ["min_seconds_left", "max_seconds_left"]) if (!WHOLE.test(text(key)) || num(text(key)) > 3600) return "Seconds left must be whole numbers from 0 to 3600.";
  if (num(rule.min_seconds_left) > num(rule.max_seconds_left)) return "\"Start betting\" must be the bigger number of seconds (it comes first).";
  for (const key of ["budget", "take_profit", "stop_loss"]) if (!MONEY.test(text(key))) return "Write money like 1.50 (dollars and cents). Use 0 for \"never\".";
  if (num(rule.budget) <= 0 || num(rule.budget) > MAX_BUDGET) return `Most to spend must be more than $0 and no more than $${MAX_BUDGET}.`;
  if (num(rule.stop_loss) >= num(rule.budget)) return "\"Cut losses\" must be less than the most you spend.";
  if (!WHOLE.test(text("max_entries")) || num(text("max_entries")) < 1 || num(text("max_entries")) > 10) return "\"Buys per market\" must be a whole number from 1 to 10.";
  if (!WHOLE.test(text("reentry_gap_sec")) || num(text("reentry_gap_sec")) > 900) return "\"Wait between buys\" must be whole seconds from 0 to 900.";
  if (rule.scalping) {
    if (num(rule.max_entries) !== 1) return "Scalping uses one buy per cycle, not scale-in buys.";
    if (num(rule.take_profit) <= 0 || num(rule.stop_loss) <= 0) return "Scalping needs positive cash-out and cut-loss amounts.";
    if (rule.side !== "momentum" && (text("min_edge") === "" || num(rule.min_edge) < 1)) return "Scalping needs at least 1¢ expected profit after entry fees.";
    if (num(rule.min_seconds_left) < 60 || num(rule.max_seconds_left) > 900) return "Scalping entries must be between 60 and 900 seconds left.";
    if (!WHOLE.test(text("max_cycles")) || num(rule.max_cycles) < 1 || num(rule.max_cycles) > 10) return "Cycles per market must be from 1 to 10.";
    if (!WHOLE.test(text("cycle_cooldown_sec")) || num(rule.cycle_cooldown_sec) < 5 || num(rule.cycle_cooldown_sec) > 900) return "Cycle cooldown must be from 5 to 900 seconds.";
    for (const key of ["market_spend_limit", "market_loss_limit"]) if (!MONEY.test(text(key)) || num(rule[key]) <= 0 || num(rule[key]) > MAX_BUDGET) return `Market spending and loss limits must be positive dollars up to $${MAX_BUDGET}.`;
    if (num(rule.market_loss_limit) > num(rule.market_spend_limit)) return "Market loss limit cannot exceed its spending limit.";
    if (num(rule.budget) > num(rule.market_spend_limit)) return "Per-buy budget cannot exceed the market spending limit.";
    if (num(rule.stop_loss) > num(rule.market_loss_limit)) return "Cut losses cannot exceed the market loss limit.";
  }
  return "";
}

export function validateSettings(form) {
  const rules = rulesOf(form);
  if (!rules.length) return "Add at least one rule.";
  if (rules.length > MAX_RULES) return `You can have up to ${MAX_RULES} rules.`;
  for (const [index, rule] of rules.entries()) { const problem = validateRule(rule); if (problem) return `Rule ${index + 1}: ${problem}`; }
  const risk = form.risk;
  if (risk) {
    for (const key of ["daily_loss_limit", "max_open_cost"]) if (!MONEY.test(String(risk[key]))) return "Risk limits must be non-negative dollars with at most two decimals.";
    for (const key of ["min_edge", "uncertainty_buffer", "max_spread"]) if (!WHOLE.test(String(risk[key])) || num(risk[key]) > 100) return "Risk edge, buffer, and spread must be whole cents from 0 to 100.";
    if (!WHOLE.test(String(risk.max_open_positions)) || num(risk.max_open_positions) > 100) return "Maximum simultaneous positions must be from 0 to 100.";
    for (const key of ["max_signal_age_ms", "max_book_age_ms"]) if (!WHOLE.test(String(risk[key])) || num(risk[key]) < 100 || num(risk[key]) > 10000) return "Freshness limits must be from 100 to 10000 milliseconds.";
  }
  return "";
}

function exitPlan(policy) {
  const up = num(policy?.take_profit) > 0, down = num(policy?.stop_loss) > 0;
  if (!up && !down) return "Hold until the market ends";
  return [up && `cash out when up ${money(policy.take_profit)}`, down && `sell if down ${money(policy.stop_loss)}`].filter(Boolean).join(", ").replace(/^./, (c) => c.toUpperCase());
}

function repeats(rule) {
  if (rule.scalping) return ` per cycle, up to ${rule.max_cycles} cycles in one market, waiting ${rule.cycle_cooldown_sec}s after each fully closed profitable exit. Never re-enter after a loss or settlement. Total spending including entry fees is capped at ${money(rule.market_spend_limit)} per market; stop new entries at ${money(rule.market_loss_limit)} in losses (exits can lose more)`;
  const times = num(rule.max_entries) || 1;
  if (times <= 1) return " once per market";
  return ` each time, up to ${times} times per market (at least ${num(rule.reentry_gap_sec) || 0}s apart, only while the deal is still good) — so up to ${money(num(rule.budget) * times)} in one market`;
}

function ruleSummary(rule) {
  if (rule.side === "momentum") return `Follow rising UP or DOWN quotes with matching short-term coin movement and at least ${rule.min_confidence}% market confidence. Spend up to ${money(rule.budget)}${repeats(rule)}. ${exitPlan(rule)}.`;
  const coin = rule.coin === "ANY" ? "any coin" : rule.coin;
  const side = rule.side === "model" ? "whichever way the bot guesses" : direction(rule.side);
  const edge = String(rule.min_edge ?? "") === "" ? "" : ` and expects at least ${rule.min_edge}¢ profit`;
  return `Bet ${side} on ${coin} when ${clock(rule.max_seconds_left)} to ${clock(rule.min_seconds_left)} is left, if it costs ${rule.min_price}¢–${rule.max_price}¢ and the bot is at least ${rule.min_confidence}% sure${edge}. Spend up to ${money(rule.budget)}${repeats(rule)}. ${exitPlan(rule)}.`;
}

function RuleList({ settings }) {
  const rules = rulesOf(toUi(settings));
  if (!rules.length) return <p className="trading-muted">No rules saved</p>;
  return <ol className="trading-rule-list">{rules.map((rule, index) => <li key={index}>
    <strong>{rule.name}</strong> {!rule.enabled && <span className="trading-muted">(turned off)</span>}
    <p className="trading-muted">{ruleSummary(rule)}</p>
  </li>)}</ol>;
}

const BLOCKERS = [
  [/not authorized/i, "This server isn't allowed to place orders (trading is switched off in its settings)."],
  [/credentials/i, "The bot doesn't know which Kalshi account to use (login keys are missing)."],
  [/another trading worker|not started/i, "The bot is still starting up, or another copy of it is already running."],
  [/not healthy/i, "The bot's trading engine isn't running properly."],
  [/reconciliation/i, "Waiting to hear back about an earlier order before placing new ones."],
];
const friendlyBlocker = (text) => BLOCKERS.find(([pattern]) => pattern.test(text))?.[1] ?? text;

function friendlyReason(reason) {
  let match;
  if ((match = /^coin is not (\w+)/.exec(reason))) return `only bets on ${match[1]}`;
  if ((match = /^(\d+)s left is outside (\d+)-(\d+)s/.exec(reason))) return `${clock(match[1])} left — waits for ${clock(match[3])} to ${clock(match[2])}`;
  if ((match = /^(YES|NO) costs ([\d.]+), outside ([\d.]+)-([\d.]+)/.exec(reason))) return `${direction(match[1].toLowerCase())} costs ${centsOf(match[2])}¢, your range is ${centsOf(match[3])}¢–${centsOf(match[4])}¢`;
  if ((match = /^model gives (YES|NO) ([\d.]+) < ([\d.]+)/.exec(reason))) return `bot is ${centsOf(match[2])}% sure, you want ${centsOf(match[3])}%`;
  if ((match = /^market implies (YES|NO) ([\d.]+) < ([\d.]+)/.exec(reason))) return `${direction(match[1].toLowerCase())} market confidence is ${(num(match[2]) * 100).toFixed(1)}%, you want ${centsOf(match[3])}%`;
  if ((match = /^edge (-?[\d.]+) after fees < (-?[\d.]+)/.exec(reason))) return `expected profit ${(num(match[1]) * 100).toFixed(1)}¢, you want ${centsOf(match[2])}¢`;
  return reason;
}

export function friendlyStatus(status = "") {
  if (status.startsWith("Matches '")) {
    const name = /^Matches '(.*?)'/.exec(status)?.[1];
    return status.includes("(waiting") ? `Matches "${name}" — waiting, already betting on this coin` : `Betting now — matches "${name}"`;
  }
  if (status.startsWith("No match - ")) return `Not yet: ${status.slice(11).split("; ").map((part) => { const at = part.indexOf(": "); return at < 0 ? part : `${part.slice(0, at)}: ${friendlyReason(part.slice(at + 2))}`; }).join("; ")}`;
  if (status.startsWith("Degraded inputs")) return "Skipping — some price data is missing";
  if (status === "Live read is stale") return "Skipping — price data is out of date";
  if (status === "No live price") return "Skipping — no price yet";
  if (status.startsWith("Bot position")) return "The bot already has a bet here";
  if (status === "No enabled rules") return "None of your rules are turned on";
  return status;
}

function WatchCards({ watch, now, updatedAt, stale, enabled }) {
  if (!watch?.length) return <p className="trading-muted">No markets open right now</p>;
  return <div className="trading-watch">{watch.map((row) => {
    const closeAt = row.close_ts_ms ?? updatedAt + num(row.seconds_left) * 1000;
    const left = Math.max(0, (closeAt - now) / 1000);
    const matched = Boolean(row.rule);
    const state = stale ? "Out of date" : left <= 0 ? "Market ended" : matched ? enabled ? "Rule matched" : "Preview match" : "Watching";
    const note = friendlyStatus(row.status).replace(/\b\d+:\d{2} left — /g, "").replace(/^Betting now — /, enabled ? "Betting now — " : "Preview only — ");
    return <article key={row.ticker} className={`watch-card${matched ? " match" : ""}`} aria-label={`Watching ${row.ticker}`}>
      <header><div className="watch-identity"><span className={`watch-coin ${coinOf(row.ticker).toLowerCase()}`} aria-hidden="true">{coinOf(row.ticker).slice(0, 1)}</span><div><strong>{coinOf(row.ticker)}</strong><small>15-minute market</small></div></div>
        <span className={`watch-state${stale ? " stale" : ""}`}>{state}</span>
        <div className={`watch-countdown${left <= 240 ? " closing" : ""}`}><span>Time left</span><strong>{clock(left)}</strong></div></header>
      <DirectionBars modelProbability={row.model_p_yes} marketProbability={row.market_p_yes} />
      <footer><span className={`trading-bet ${row.side === "no" ? "down" : row.side ? "up" : ""}`}>{row.side ? `${row.strategy === "momentum" && !matched ? "Watching " : ""}${direction(row.side)} at ${centsOf(row.price)}¢` : "No bet yet"}</span>
        {row.strategy === "momentum" && row.entry_confidence != null && <p>Momentum entry: {direction(row.side)} · Market confidence {(num(row.entry_confidence) * 100).toFixed(1)}%</p>}
        {row.scalping && <p>Scalp cycles started: {row.scalping.cycles} · Spent {money(row.scalping.spent)} · Closed net {signedMoney(row.scalping.realized_pnl)}</p>}
        <p>{note}</p></footer>
    </article>;
  })}</div>;
}

const EVENT_NAMES = {
  buy_submitted: "Placed a bet", sell_submitted: "Tried to sell", filled: "Order went through", unfilled: "Order didn't go through",
  rejected: "Kalshi refused the order", settled: "Market ended", waiting_for_liquidity: "Wants to sell, but nobody is buying yet",
  skipped: "Skipped a market", enabled: "Bot turned on", paused: "Bot turned off", started_paused: "Bot restarted (left off for safety)",
  settings_saved: "Rules saved", error: "Something went wrong",
};

function Diary({ events }) {
  return <ol className="trading-actions">{events.map((event) => <li key={event.id}><time>{timestamp(event.ts_ms)}</time><strong>{EVENT_NAMES[event.action] ?? event.action}</strong>{event.ticker && <small className="trading-muted">{coinOf(event.ticker)}</small>}<span>{event.reason}</span></li>)}</ol>;
}

function DecisionChecks({ detail }) {
  let checks;
  try { checks = JSON.parse(detail); } catch { return null; }
  if (!Array.isArray(checks) || !checks.length) return null;
  return <div className="checks">{checks.map((check, index) => <span className={`check ${check.agree ? "pass" : "fail"}`} key={`${check.name}-${index}`}>{check.agree ? <Check size={13} /> : <X size={13} />}{friendlyCheckName(check.name)}</span>)}</div>;
}

function Why({ decision }) {
  if (!decision) return <p className="trading-muted">No notes from the bot for this market</p>;
  const pick = { BUY_YES: "it would go UP", BUY_NO: "it would go DOWN" }[decision.recommendation] ?? "no clear winner";
  return <div className="trading-decision-body">
    <p>The bot's guess: <strong>{pick}</strong> ({Math.round(num(decision.confidence) * 100)}% sure)</p>
    <DecisionChecks detail={decision.confirmation_detail} />
  </div>;
}

function Stat({ label, value, tone }) {
  return <div><dt>{label}</dt><dd className={tone}>{value}</dd></div>;
}

function LiveCard({ position, events, decision, now }) {
  const quantity = num(position.quantity);
  const each = quantity > 0 ? `${Math.round((num(position.entry_cost) / quantity) * 100)}¢` : "--";
  const left = position.close_ts_ms ? clock((position.close_ts_ms - now) / 1000) : "--";
  const state = position.status === "pending" ? (position.pending?.action === "sell" ? "Selling…" : "Placing order…") : position.pending?.action === "sell" ? "Selling…" : "Running";
  const worth = position.net_pnl == null ? "--" : signedMoney(position.net_pnl);
  return <article className="trading-card" aria-label={`Trade ${position.ticker}`}>
    <header><strong className="trading-coin">{coinOf(position.ticker)}</strong><span className={`trading-bet ${position.side === "no" ? "down" : "up"}`}>Bet {direction(position.side)}</span><span className="trading-chip">{state}</span></header>
    <dl className="trading-policy">
      <Stat label="You paid" value={money(position.entry_cost)} />
      <Stat label="Contracts" value={position.quantity} />
      {num(position.entries) > 1 && <Stat label="Times bought" value={position.entries} />}
      <Stat label="Cost each" value={each} />
      <Stat label="If it wins you get" value={money(quantity)} tone="positive" />
      <Stat label="If you sold now" value={worth} tone={num(position.net_pnl) > 0 ? "positive" : num(position.net_pnl) < 0 ? "negative" : ""} />
      <Stat label="Market ends in" value={left} />
      {position.scalp && <Stat label="Scalp cycle" value={`${position.cycle_number}/${position.scalp.max_cycles}`} />}
    </dl>
    <p className="trading-muted">Rule used: <strong>{position.rule ?? "--"}</strong> · Plan: {exitPlan(position.policy)}</p>
    <p className="trading-muted">Market ID: <strong>{position.market_id ?? position.ticker}</strong>{position.entry_gate && ` · Model checks ${position.entry_gate.checks_agree}/${position.entry_gate.checks_total}, locked ${direction(position.entry_gate.call)}`}</p>
    {position.liquidity_warning && <p className="negative">Nobody is buying right now, so the bot can't sell yet.</p>}
    <details className="trading-decision"><summary><span>What the bot did</span><ChevronDown size={16} /></summary>{events.length ? <Diary events={events} /> : <p className="trading-muted">Nothing yet</p>}</details>
    <details className="trading-decision"><summary><span>Why the bot made this bet</span><ChevronDown size={16} /></summary>{position.scalp && position.entry_signal
      ? <p className="trading-muted">Live entry checked at {timestamp(position.entry_signal.ts_ms)}: {direction(position.side)} at {centsOf(position.entry_signal.price)}¢, {position.entry_signal.confidence_source === "market" ? "market-implied settlement chance" : "estimated win chance"} {(num(position.entry_signal.confidence) * 100).toFixed(1)}%. {position.strategy === "momentum" ? "Entry follows quote and coin movement, not the settlement model's pick. This is not a probability of scalp profit." : "This is the cycle's entry prediction, not the saved final pick."}</p>
      : <Why decision={decision} />}</details>
  </article>;
}

const ENDINGS = { settled: "Market ended", take_profit: "Cashed out early", stop_loss: "Sold to cut losses" };

function FinishedCard({ position }) {
  const pnl = num(position.net_pnl);
  const outcome = position.net_pnl == null ? "Closed" : pnl > 0 ? "Won" : pnl < 0 ? "Lost" : "Broke even";
  const tone = pnl > 0 ? "win" : pnl < 0 ? "loss" : "";
  const endedAt = position.closed_ms ?? position.opened_ms;
  return <li className={`trading-card trading-finished ${tone}`} aria-label={`Finished trade ${position.ticker}`}>
    <header>
      <div className="watch-identity"><span className={`watch-coin ${coinOf(position.ticker).toLowerCase()}`} aria-hidden="true">{coinOf(position.ticker).slice(0, 1)}</span><div><strong>{coinOf(position.ticker)}</strong><small>Bet {direction(position.side)}</small></div></div>
      <span className={`trading-outcome ${tone}`}>{outcome}</span>
    </header>
    <div className="finished-result"><span>Net profit / loss</span><strong className={pnl > 0 ? "positive" : pnl < 0 ? "negative" : ""}>{signedMoney(position.net_pnl)}</strong></div>
    <dl className="finished-details">
      <Stat label="You paid" value={money(position.entry_cost)} />
      <Stat label="Got back" value={money(position.exit_credit)} />
    </dl>
    <details className="finished-extra"><summary>Trade details</summary><dl className="finished-details">
      <Stat label="Market ID" value={position.market_id ?? position.ticker} />
      <Stat label="Contracts" value={position.bought_quantity ?? position.quantity ?? "--"} />
      <Stat label="Rule used" value={position.rule ?? "--"} />
      {position.entry_gate && <Stat label="Model checks at entry" value={`${position.entry_gate.checks_agree}/${position.entry_gate.checks_total} · locked ${direction(position.entry_gate.call)}`} />}
      {position.scalp && <Stat label="Scalp cycle" value={`${position.cycle_number}/${position.scalp.max_cycles}`} />}
    </dl></details>
    <footer><span>{ENDINGS[position.closed_by] ?? "Closed"}</span><time dateTime={endedAt == null ? undefined : new Date(endedAt).toISOString()}>{timestamp(endedAt)}</time></footer>
  </li>;
}

function FinishedBets({ positions }) {
  const [visibleCount, setVisibleCount] = React.useState(2);
  return <>
    <ol className="trading-cards trading-finished-list">{positions.slice(0, visibleCount).map((position, index) => <FinishedCard key={position.position_id ?? `${position.ticker}-${index}`} position={position} />)}</ol>
    {positions.length > 2 && <div className="trading-buttons finished-controls">
      <span className="trading-muted">Showing {Math.min(visibleCount, positions.length)} of {positions.length} finished bets</span>
      {visibleCount < positions.length && <button className="secondary-button" onClick={() => setVisibleCount((count) => count + 2)}>Load more</button>}
      {visibleCount > 2 && <button className="secondary-button" onClick={() => setVisibleCount(2)}>Show fewer</button>}
    </div>}
  </>;
}

function EnableDialog({ snapshot, blocked, busy, onCancel, onConfirm }) {
  const dialogRef = React.useRef(null);
  const [acknowledged, setAcknowledged] = React.useState(false);
  React.useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog.showModal) dialog.showModal();
    else dialog.setAttribute("open", "");
  }, []);
  return <dialog ref={dialogRef} className="trading-dialog" aria-labelledby="enable-title" onCancel={(event) => { event.preventDefault(); if (!busy) onCancel(); }}>
    <header><h2 id="enable-title">Turn on the real-money bot?</h2><button className="icon-button" aria-label="Cancel" title="Cancel" disabled={busy} onClick={onCancel}><X size={18} /></button></header>
    <p>{snapshot.environment === "demo" ? "This is a Kalshi demo account, so no real money is used." : "The bot will spend your real money."}</p>
    <h3>It will follow these rules</h3><RuleList settings={snapshot.settings} />
    <p>Every time a market matches one of your rules, the bot bets on its own (one bet per coin at a time). You can lose the money you bet. Turning the bot off later stops new bets, but bets already made keep going until the market ends.</p>
    <label className="trading-ack"><input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} />I understand the bot bets on its own and I could lose money.</label>
    {blocked && <p role="alert">Something changed. Close this and check again before turning the bot on.</p>}
    <div className="trading-buttons"><button className="secondary-button" disabled={busy} onClick={onCancel}>Cancel</button><button className="secondary-button" disabled={!acknowledged || blocked || busy} onClick={onConfirm}><Power size={16} />Yes, turn it on</button></div>
  </dialog>;
}

export default function Trading() {
  const [data, setData] = React.useState(null);
  const [forms, setForms] = React.useState({ live: null, paper: null });
  const [mode, setMode] = React.useState("paper");
  const [loading, setLoading] = React.useState(true);
  const [stale, setStale] = React.useState(true);
  const [readError, setReadError] = React.useState("");
  const [error, setError] = React.useState("");
  const [success, setSuccess] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [snapshot, setSnapshot] = React.useState(null);
  const [updatedAt, setUpdatedAt] = React.useState(null);
  const [now, setNow] = React.useState(Date.now);
  const [streamStatus, setStreamStatus] = React.useState("Connecting");
  const requestRef = React.useRef(null);
  const dataVersionRef = React.useRef(0);
  const busyRef = React.useRef(false);
  const mountedRef = React.useRef(false);

  const validateData = (body) => {
    if (MODES.some(([key]) => !body?.[key] || typeof body[key] !== "object" || !body[key].settings || typeof body[key].settings !== "object" || typeof body[key].enabled !== "boolean")) throw new Error("Couldn't read the bot's status");
  };

  const request = async (path, options = {}) => {
    const version = dataVersionRef.current;
    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;
    const response = await fetch(path, { cache: "no-store", ...options, signal: controller.signal });
    const body = await response.json();
    if (!response.ok) throw new Error(tradingErrorMessage(body.detail, response.status));
    validateData(body);
    if (controller.signal.aborted || !mountedRef.current || (!options.method && version !== dataVersionRef.current)) throw Object.assign(new Error("Aborted"), { name: "AbortError" });
    return body;
  };

  const accept = (body, resetMode = null) => {
    dataVersionRef.current += 1;
    setData(body);
    setForms((current) => Object.fromEntries(MODES.map(([key]) => [key, resetMode === key || current[key] === null ? scalpForm(body[key].settings) : current[key]])));
    setStale(false); setReadError(""); setUpdatedAt(Date.now());
  };

  const reload = async () => {
    if (busyRef.current) return;
    setLoading(true);
    try { accept(await request("/api/trading")); }
    catch (nextError) { if (mountedRef.current && nextError.name !== "AbortError") { setStale(true); setReadError(nextError.message); } }
    finally { if (mountedRef.current && !busyRef.current) setLoading(false); }
  };

  React.useEffect(() => {
    mountedRef.current = true;
    void reload();
    let socket;
    let retryTimer;
    let retryMs = 1000;
    let lastMessageAt = Date.now();
    let disconnectReason = "Live updates disconnected. Reconnecting automatically";
    let stopped = false;
    const disconnected = (message) => {
      setStreamStatus("Reconnecting");
      setStale(true);
      setReadError(message);
    };
    const connect = () => {
      if (stopped) return;
      setStreamStatus("Connecting");
      lastMessageAt = Date.now();
      disconnectReason = "Live updates disconnected. Reconnecting automatically";
      socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/trading`);
      socket.onmessage = (event) => {
        if (stopped) return;
        try {
          const message = JSON.parse(event.data);
          if (message.type !== "trading_state") return;
          validateData(message.data);
          lastMessageAt = Date.now();
          retryMs = 1000;
          setStreamStatus("Live");
          if (!busyRef.current) {
            accept(message.data);
            setLoading(false);
          }
        } catch (nextError) {
          disconnectReason = `Couldn't read live updates: ${nextError.message}`;
          disconnected(disconnectReason);
          socket.close();
        }
      };
      socket.onerror = () => { socket.close(); };
      socket.onclose = () => {
        if (stopped) return;
        lastMessageAt = null;
        disconnected(disconnectReason);
        retryTimer = window.setTimeout(connect, retryMs);
        retryMs = Math.min(retryMs * 2, 15_000);
      };
    };
    connect();
    const interval = window.setInterval(() => {
      setNow(Date.now());
      if (lastMessageAt != null && Date.now() - lastMessageAt > 15_000) {
        disconnectReason = "Live updates stopped. Reconnecting automatically";
        disconnected(disconnectReason);
        socket.close();
      }
    }, 1000);
    return () => {
      stopped = true; mountedRef.current = false;
      window.clearInterval(interval); window.clearTimeout(retryTimer);
      socket.close(); requestRef.current?.abort();
    };
  }, []);

  const snap = data?.[mode];
  const form = forms[mode];
  const blockers = snap?.blockers ?? [];
  const strategySaved = Boolean(snap) && formKey(toUi(snap.settings)) === formKey(toUi(scalpSettings(scalpForm(snap.settings))));
  const dirty = Boolean(form) && Boolean(snap) && (!strategySaved || JSON.stringify(form) !== JSON.stringify(scalpForm(snap.settings)));
  const validation = form ? validateScalp(form) : "";
  const enableBlocked = !snap || stale || busy || dirty || blockers.length > 0 || Boolean(snap.error);
  const snapshotChanged = snapshot && (formKey(toUi(snapshot.settings)) !== formKey(toUi(data?.live?.settings)) || snapshot.environment !== data?.live?.environment);

  const mutate = async (kind, body) => {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError(""); setSuccess("");
    try {
      const next = await request(`/api/trading/${kind}`, { method: kind === "settings" ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      accept(next, kind === "settings" ? body.mode : null);
      setSuccess(kind === "settings" ? "Scalping settings saved." : `${modeName(body.mode)} turned ${next[body.mode].enabled ? "on" : "off"}.`);
      setSnapshot(null);
    } catch (nextError) {
      if (mountedRef.current && nextError.name !== "AbortError") { setError(nextError.message); setStale(true); setSnapshot(null); }
    } finally { busyRef.current = false; if (mountedRef.current) { setBusy(false); setLoading(false); } }
  };


  const toggle = () => {
    if (!snap) return;
    if (snap.enabled) return void mutate("control", { mode, enabled: false, confirm: false });
    if (mode === "paper") return void mutate("control", { mode: "paper", enabled: true, confirm: true, settings: structuredClone(snap.settings) });
    setSnapshot({ settings: structuredClone(snap.settings), environment: snap.environment });
  };

  const events = [...(snap?.events ?? [])].sort((a, b) => b.ts_ms - a.ts_ms);
  const positions = snap?.positions ?? [];
  const decisions = snap?.decisions ?? [];
  const running = positions.filter((position) => position.status === "pending" || position.status === "open");
  const finished = positions.filter((position) => position.status === "closed").sort((a, b) => (b.closed_ms ?? b.opened_ms ?? 0) - (a.closed_ms ?? a.opened_ms ?? 0));
  const scored = finished.filter((position) => position.net_pnl != null);
  const total = snap?.summary ? num(snap.summary.net_pnl) : scored.reduce((sum, position) => sum + num(position.net_pnl), 0);
  const wins = snap?.summary?.wins ?? scored.filter((position) => num(position.net_pnl) > 0).length;
  const finishedCount = snap?.summary?.finished ?? scored.length;
  const decisionFor = (ticker) => decisions.filter((decision) => decision.ticker === ticker).sort((a, b) => b.ts_ms - a.ts_ms)[0];
  const title = snap ? modeLabel(mode, snap.environment) : "Trading";

  return <section className="trading" aria-label="Trading control">
    <header className="trading-heading"><div><h2>Auto trading</h2><p className="trading-muted">{!snap ? "Status unknown" : stale ? `Might be out of date — last seen ${snap.enabled ? "ON" : "OFF"}` : `Bot is ${snap.enabled ? "ON" : "OFF"}`} · Updated {timestamp(updatedAt)}</p></div><button className="icon-button" title="Refresh" aria-label="Refresh" disabled={busy} onClick={() => void reload()}><RefreshCw size={18} className={loading ? "spin" : ""} /></button></header>
    <fieldset className="trading-modes" role="radiogroup" aria-label="Money type">{MODES.map(([key, text]) => <label key={key}><input type="radio" name="trading-mode" value={key} checked={mode === key} onChange={() => { setMode(key); setError(""); setSuccess(""); }} />{text}</label>)}</fieldset>
    {loading && !data && <p role="status">Loading the bot's status...</p>}
    {readError && <div className="alert" role="alert"><AlertTriangle size={17} /><span>{readError}. {data ? "Showing the last info we got — it may be out of date." : "Status unknown."}</span><button className="secondary-button" disabled={busy} onClick={() => void reload()}>Try again</button></div>}
    {error && <div className="alert" role="alert"><span>{error}</span><button className="icon-button" aria-label="Dismiss error" onClick={() => setError("")}><X size={16} /></button></div>}
    {success && <div className="trading-success" role="status"><Check size={16} />{success}<button className="icon-button" aria-label="Dismiss message" onClick={() => setSuccess("")}><X size={16} /></button></div>}
    {snap && <>
      <section className={`trading-status ${snap.enabled ? "on" : "off"}`} aria-label="Bot status">
        <div className="trading-control">
          <div>
            <p className="trading-muted">{title}</p>
            <h3>{snap.enabled ? "The bot is ON" : "The bot is OFF"}</h3>
            <p className="trading-muted">{snap.enabled ? "It places trades automatically when the saved strategy qualifies." : "Save your settings, then turn it on to scalp short-term movement."}</p>
          </div>
          <label className="trading-switch"><input type="checkbox" role="switch" aria-label={modeName(mode)} checked={snap.enabled} disabled={snap.enabled ? busy : enableBlocked} onChange={toggle} /><Power size={17} />{snap.enabled ? "ON" : "OFF"}</label>
        </div>
        {blockers.length > 0 && <div className="trading-blockers"><strong>Why the bot can't bet right now:</strong><ul>{blockers.map((blocker, index) => <li key={index}>{friendlyBlocker(blocker)}</li>)}</ul></div>}
        {!snap.enabled && dirty && <p className="trading-muted">Save your scalping settings before turning the bot on.</p>}
        {snap.error && <p className="negative" role="alert">{snap.error}</p>}
      </section>
      <section className="trading-section" aria-label="Scalping controls"><h3>Scalp market movement</h3>
        {!strategySaved && <p className="trading-muted">Your previous strategy is still saved. Turn the bot off and save here to replace it with momentum scalping. Existing trades keep their original exit plan.</p>}
        <form onSubmit={(event) => { event.preventDefault(); if (!validation && dirty && !snap.enabled && !busy && !stale) void mutate("settings", { mode, ...scalpSettings(form) }); }}>
          <ScalpControls form={form} disabled={snap.enabled || busy} onChange={(next) => { setForms({ ...forms, [mode]: next }); setSuccess(""); setError(""); }} />
          {validation && <p className="negative" role="alert">{validation}</p>}
          <div className="trading-buttons">
            <button className="secondary-button" type="submit" disabled={snap.enabled || busy || stale || !dirty || !!validation}><Save size={16} />Save settings</button>
            <span className="trading-muted">{snap.enabled ? "Turn the bot off to change settings." : dirty ? "You have unsaved changes" : "All changes saved"}</span>
          </div>
          <p className="trading-muted">Saving replaces only this mode's strategy and never starts trading. Test in Practice first. Real-money trading still requires your confirmation.</p>
        </form>
      </section>
      <section className="trading-section" aria-label="Scoreboard"><dl className="trading-score">
        <Stat label="Bets running now" value={running.length} />
        <Stat label="Finished bets" value={snap?.summary?.finished ?? finished.length} />
        <Stat label="Total won / lost" value={finishedCount ? signedMoney(total) : "--"} tone={total > 0 ? "positive" : total < 0 ? "negative" : ""} />
        <Stat label="Win rate" value={finishedCount ? `${Math.round((wins / finishedCount) * 100)}% (${wins} of ${finishedCount})` : "--"} />
      </dl></section>
      <section className="trading-section" aria-label="Bets happening now"><h3>Bets happening now</h3>
        {running.length ? <div className="trading-cards">{running.map((position, index) => <LiveCard key={position.position_id ?? `${position.ticker}-${index}`} position={position} now={now} events={events.filter((event) => position.position_id ? event.position_id === position.position_id : event.ticker === position.ticker)} decision={decisionFor(position.ticker)} />)}</div>
          : <p className="trading-muted">No bets running right now</p>}
      </section>
      <section className="trading-section" aria-label="Finished bets"><h3>Finished bets</h3>
        {finished.length ? <FinishedBets key={mode} positions={finished} />
          : <p className="trading-muted">No finished bets yet</p>}
      </section>
      <section className="trading-section" aria-label="Markets the bot is watching">
        <div className="watch-heading"><h3>Markets the bot is watching</h3><span className={`watch-live${streamStatus === "Live" && !stale ? " connected" : ""}`} role="status"><span aria-hidden="true" />{stale ? "Out of date" : streamStatus} · {snap.watch?.length ?? 0} {snap.watch?.length === 1 ? "market" : "markets"}</span></div>
        <p className="trading-muted">Bot leans is the settlement model, not a scalp direction. Momentum entries follow recent quote movement with matching short-term coin movement; Market confidence applies to the actual entry side. A high UP price alone is not evidence it is rising.{snap.enabled ? "" : " The bot is OFF, so this is just a preview."}</p>
        <WatchCards watch={snap.watch} now={now} updatedAt={updatedAt} stale={stale} enabled={snap.enabled} />
      </section>
      <section className="trading-section" aria-label="Bot diary">
        <details className="trading-decision"><summary><span>Bot diary ({events.length})</span><ChevronDown size={16} /></summary>
          {events.length ? <Diary events={events} /> : <p className="trading-muted">Nothing has happened yet</p>}
        </details>
      </section>
    </>}
    {snapshot && <EnableDialog snapshot={snapshot} busy={busy} blocked={enableBlocked || snapshotChanged || data?.live?.enabled} onCancel={() => setSnapshot(null)} onConfirm={() => { if (!enableBlocked && !snapshotChanged && !data.live.enabled) void mutate("control", { mode: "live", enabled: true, confirm: true, settings: snapshot.settings }); }} />}
  </section>;
}
