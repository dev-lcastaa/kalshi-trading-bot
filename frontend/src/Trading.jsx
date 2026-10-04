import React from "react";
import { AlertTriangle, Check, ChevronDown, Power, RefreshCw, Save, X } from "lucide-react";
import { friendlyCheckName, percent } from "./utils";

const MODES = [["live", "Live auto trading"], ["paper", "Paper auto trading"]];
const FIELDS = [["budget", "Budget"], ["take_profit", "Take profit"], ["stop_loss", "Stop loss"]];
const settingsKey = (settings) => JSON.stringify(FIELDS.map(([key]) => settings?.[key]));
const timestamp = (value) => value == null ? "--" : new Date(value).toLocaleString();
const dollars = (value) => value == null ? "--" : `$${value}`;
const modeLabel = (mode, environment) => mode === "paper" ? "Paper trading — simulated, no real money" : environment === "prod" ? "Live trading — real money" : "Live trading — demo account";
const modeName = (mode) => mode === "paper" ? "Paper trading" : "Live trading";

export function validateSettings(settings) {
  if (FIELDS.some(([key]) => !/^\d+(\.\d{1,2})?$/.test(settings[key]) || !Number.isFinite(Number(settings[key])) || Number(settings[key]) <= 0)) return "Enter positive dollar amounts in whole cents.";
  if (Number(settings.budget) > 1.99) return "Budget must be $1.99 or less.";
  if (Number(settings.stop_loss) >= Number(settings.budget)) return "Stop loss must be less than budget.";
  return "";
}

function Policy({ settings }) {
  return <dl className="trading-policy">{FIELDS.map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{dollars(settings?.[key])}</dd></div>)}</dl>;
}

function DecisionChecks({ detail }) {
  let checks;
  try { checks = JSON.parse(detail); } catch { return <p className="trading-muted">Checks unavailable</p>; }
  if (!Array.isArray(checks) || !checks.length) return <p className="trading-muted">No checks recorded</p>;
  return <div className="checks">{checks.map((check, index) => <span className={`check ${check.agree ? "pass" : "fail"}`} key={`${check.name}-${index}`}>{check.agree ? <Check size={13} /> : <X size={13} />}{friendlyCheckName(check.name)}{check.detail ? `: ${check.detail}` : ""}</span>)}</div>;
}

function DecisionBody({ decision }) {
  return <div className="trading-decision-body">
    <p><strong>{decision.recommendation}</strong> | Confidence {percent(decision.confidence)} | Result {decision.result ?? "Pending"}</p>
    <p className="trading-muted">Decision {timestamp(decision.ts_ms)} | Close {timestamp(decision.close_ts_ms)}</p>
    <details className="trading-decision"><summary><span>Confirmation checks</span><ChevronDown size={16} /></summary><DecisionChecks detail={decision.confirmation_detail} /></details>
  </div>;
}

function ActionList({ events }) {
  return <ol className="trading-actions">{events.map((event) => <li key={event.id}><time>{timestamp(event.ts_ms)}</time><strong>{event.action}</strong>{event.ticker && <code>{event.ticker}</code>}<span>{event.reason}</span></li>)}</ol>;
}

function PositionCard({ position, events, decision }) {
  return <article className="trading-card" aria-label={`Position ${position.ticker}`}>
    <header><code>{position.ticker}</code><strong>{position.side.toUpperCase()} / {position.status}</strong></header>
    <dl className="trading-policy">{[["Quantity", position.quantity], ["Entry cost", dollars(position.entry_cost)], ["Exit credit", dollars(position.exit_credit)], ["Net P/L", dollars(position.net_pnl)]].map(([text, value]) => <div key={text}><dt>{text}</dt><dd>{value}</dd></div>)}</dl>
    <h4>Position policy</h4><Policy settings={position.policy} />
    {position.pending && <p className="trading-muted">Pending {position.pending.action}: <code>{position.pending.client_order_id}</code>{position.pending.order_id && <> / <code>{position.pending.order_id}</code></>}</p>}
    <h4>Actions</h4>{events.length ? <ActionList events={events} /> : <p className="trading-muted">No actions yet</p>}
    <h4>Decision</h4>{decision ? <DecisionBody decision={decision} /> : <p className="trading-muted">No decision recorded</p>}
  </article>;
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
    <header><h2 id="enable-title">Enable live trading?</h2><button className="icon-button" aria-label="Cancel enabling" title="Cancel enabling" disabled={busy} onClick={onCancel}><X size={18} /></button></header>
    <h3>Saved settings</h3><Policy settings={snapshot.settings} />
    <p>{snapshot.environment === "demo" ? "Demo orders only. No real money." : "Real-money orders will be placed."}</p>
    <p>Stops are not guaranteed. Disabling does not close positions; threshold exits continue.</p>
    <label className="trading-ack"><input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} />I acknowledge the trading risks.</label>
    {blocked && <p role="alert">Status or saved settings changed. Cancel and review before enabling.</p>}
    <div className="trading-buttons"><button className="secondary-button" disabled={busy} onClick={onCancel}>Cancel</button><button className="secondary-button" disabled={!acknowledged || blocked || busy} onClick={onConfirm}><Power size={16} />Confirm enable</button></div>
  </dialog>;
}

export default function Trading() {
  const [data, setData] = React.useState(null);
  const [forms, setForms] = React.useState({ live: null, paper: null });
  const [mode, setMode] = React.useState("live");
  const [loading, setLoading] = React.useState(true);
  const [stale, setStale] = React.useState(true);
  const [readError, setReadError] = React.useState("");
  const [error, setError] = React.useState("");
  const [success, setSuccess] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [snapshot, setSnapshot] = React.useState(null);
  const [updatedAt, setUpdatedAt] = React.useState(null);
  const requestRef = React.useRef(null);
  const busyRef = React.useRef(false);
  const mountedRef = React.useRef(false);

  const request = async (path, options = {}) => {
    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;
    const response = await fetch(path, { cache: "no-store", ...options, signal: controller.signal });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`);
    if (MODES.some(([key]) => !body?.[key] || typeof body[key] !== "object" || !body[key].settings || typeof body[key].settings !== "object" || typeof body[key].enabled !== "boolean")) throw new Error("Trading status unavailable");
    if (controller.signal.aborted || !mountedRef.current) throw Object.assign(new Error("Aborted"), { name: "AbortError" });
    return body;
  };

  const accept = (body, resetMode = null) => {
    setData(body);
    setForms((current) => Object.fromEntries(MODES.map(([key]) => [key, resetMode === key || current[key] === null ? { ...body[key].settings } : current[key]])));
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
    const interval = window.setInterval(() => void reload(), 3000);
    return () => { mountedRef.current = false; window.clearInterval(interval); requestRef.current?.abort(); };
  }, []);

  const snap = data?.[mode];
  const form = forms[mode];
  const blockers = snap?.blockers ?? [];
  const dirty = Boolean(form) && Boolean(snap) && settingsKey(form) !== settingsKey(snap.settings);
  const validation = form ? validateSettings(form) : "";
  const label = snap ? modeLabel(mode, snap.environment) : "Trading";
  const enableBlocked = !snap || stale || busy || dirty || blockers.length > 0 || Boolean(snap.error);
  const snapshotChanged = snapshot && (settingsKey(snapshot.settings) !== settingsKey(data?.live?.settings) || snapshot.environment !== data?.live?.environment);

  const mutate = async (kind, body) => {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError(""); setSuccess("");
    try {
      const next = await request(`/api/trading/${kind}`, { method: kind === "settings" ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      accept(next, kind === "settings" ? body.mode : null);
      setSuccess(kind === "settings" ? "Settings saved." : `${modeName(body.mode)} ${next[body.mode].enabled ? "enabled" : "disabled"}.`);
      setSnapshot(null);
    } catch (nextError) {
      if (mountedRef.current && nextError.name !== "AbortError") { setError(nextError.message); setStale(true); setSnapshot(null); }
    } finally { busyRef.current = false; if (mountedRef.current) { setBusy(false); setLoading(false); } }
  };

  const toggle = () => {
    if (!snap) return;
    if (snap.enabled) return void mutate("control", { mode, enabled: false, confirm: false });
    if (mode === "paper") return void mutate("control", { mode: "paper", enabled: true, confirm: true, settings: { ...snap.settings } });
    setSnapshot({ settings: { ...snap.settings }, environment: snap.environment });
  };

  const events = [...(snap?.events ?? [])].sort((a, b) => b.ts_ms - a.ts_ms);
  const positions = snap?.positions ?? [];
  const decisions = snap?.decisions ?? [];
  const positionTickers = new Set(positions.map((position) => position.ticker));
  const modeEvents = events.filter((event) => !event.ticker);
  const otherDecisions = decisions.filter((decision) => !positionTickers.has(decision.ticker));
  const decisionFor = (ticker) => decisions.filter((decision) => decision.ticker === ticker).sort((a, b) => b.ts_ms - a.ts_ms)[0];

  return <section className="trading" aria-label="Trading control">
    <header className="trading-heading"><div><h2>Trading</h2><p className="trading-muted">{!snap ? "Status unknown" : stale ? `Stale status: last reported ${snap.enabled ? "enabled" : "paused"}` : snap.enabled ? "Enabled" : "Paused"} | Updated {timestamp(updatedAt)}</p></div><button className="icon-button" title="Refresh trading" aria-label="Refresh trading" disabled={busy} onClick={() => void reload()}><RefreshCw size={18} className={loading ? "spin" : ""} /></button></header>
    <fieldset className="trading-modes" role="radiogroup" aria-label="Trading mode">{MODES.map(([key, text]) => <label key={key}><input type="radio" name="trading-mode" value={key} checked={mode === key} onChange={() => { setMode(key); setError(""); setSuccess(""); }} />{text}</label>)}</fieldset>
    {loading && !data && <p role="status">Loading trading status...</p>}
    {readError && <div className="alert" role="alert"><AlertTriangle size={17} /><span>{readError}. {data ? "Last data retained; status is stale." : "Status unknown."}</span><button className="secondary-button" disabled={busy} onClick={() => void reload()}>Retry</button></div>}
    {error && <div className="alert" role="alert"><span>{error}</span><button className="icon-button" aria-label="Dismiss error" onClick={() => setError("")}><X size={16} /></button></div>}
    {success && <div className="trading-success" role="status"><Check size={16} />{success}<button className="icon-button" aria-label="Dismiss success" onClick={() => setSuccess("")}><X size={16} /></button></div>}
    {snap && <>
      <section className="trading-section" aria-label="Order controls">
        <div className="trading-control">
          <div><h3>{label}</h3><p className="trading-risk">{mode === "paper" ? "Simulated fills only. No real money." : snap.environment === "demo" ? "Demo orders only. No real money." : "Real-money orders."} Stops are not guaranteed. Disabling does not close positions; threshold exits continue.</p></div>
          <label className="trading-switch"><input type="checkbox" role="switch" aria-label={label} checked={snap.enabled} disabled={snap.enabled ? busy : enableBlocked} onChange={toggle} /><Power size={17} />{snap.enabled ? "Enabled" : "Paused"}</label>
        </div>
        {blockers.length > 0 && <ul className="trading-blockers">{blockers.map((blocker, index) => <li key={index}>{blocker}</li>)}</ul>}
        {snap.error && <p className="negative" role="alert">{snap.error}</p>}
        <p className="trading-muted">Last cycle: {timestamp(snap.last_cycle_ms)}</p>
      </section>
      <section className="trading-section" aria-label="Trading settings"><h3>Settings</h3>
        <form onSubmit={(event) => { event.preventDefault(); if (!validation && dirty && !snap.enabled && !busy && !stale) void mutate("settings", { mode, ...Object.fromEntries(FIELDS.map(([key]) => [key, Number(form[key]).toFixed(2)])) }); }}>
          <div className="trading-fields">{FIELDS.map(([key, text]) => <label key={key}>{text} ($)<input type="text" inputMode="decimal" value={form?.[key] ?? ""} disabled={snap.enabled || busy} onChange={(event) => { setForms({ ...forms, [mode]: { ...form, [key]: event.target.value } }); setSuccess(""); setError(""); }} /></label>)}</div>
          {validation && <p className="negative" role="alert">{validation}</p>}
          <div className="trading-buttons"><button className="secondary-button" type="submit" disabled={snap.enabled || busy || stale || !dirty || !!validation}><Save size={16} />Save settings</button><span className="trading-muted">{snap.enabled ? "Pause trading to change settings." : dirty ? "Unsaved changes" : "Saved"}</span></div>
        </form><h4>Saved settings</h4><Policy settings={snap.settings} />
      </section>
      <section className="trading-section" aria-label="Trading positions"><h3>Positions</h3>
        {positions.length ? <div className="trading-cards">{positions.map((position, index) => <PositionCard key={`${position.ticker}-${index}`} position={position} events={events.filter((event) => event.ticker === position.ticker)} decision={decisionFor(position.ticker)} />)}</div> : <p className="trading-muted">No positions</p>}
      </section>
      <section className="trading-section" aria-label="Activity"><h3>Activity</h3>
        {modeEvents.length ? <ActionList events={modeEvents} /> : <p className="trading-muted">No activity recorded</p>}
      </section>
      {otherDecisions.length > 0 && <section className="trading-section" aria-label="Other decisions">
        <details className="trading-decision"><summary><span>Other decisions ({otherDecisions.length})</span><ChevronDown size={16} /></summary>
          {otherDecisions.map((decision, index) => <div className="trading-other-decision" key={`${decision.ticker}-${decision.ts_ms}-${index}`}><code>{decision.ticker}</code><DecisionBody decision={decision} /></div>)}
        </details>
      </section>}
    </>}
    {snapshot && <EnableDialog snapshot={snapshot} busy={busy} blocked={enableBlocked || snapshotChanged || data?.live?.enabled} onCancel={() => setSnapshot(null)} onConfirm={() => { if (!enableBlocked && !snapshotChanged && !data.live.enabled) void mutate("control", { mode: "live", enabled: true, confirm: true, settings: snapshot.settings }); }} />}
  </section>;
}