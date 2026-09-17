import React from "react";
import {
  Activity, AlertTriangle, ArrowLeft, BarChart3, Bitcoin, Bot, Check, ChevronDown,
  CircleDot, Copy, Fish, FlaskConical, History, Orbit, Radio, RefreshCw,
  Search, ShieldCheck, Trophy, Waves, X,
} from "lucide-react";
import { useDashboardData, useTelemetry } from "./hooks/useDashboardData";
import { useLivePrices } from "./hooks/useLivePrices";
import { coinMeta, countdown, formatDecision, money, percent, recommendationLabel } from "./utils";

const TABS = [
  ["active", "Live Picks", Activity],
  ["closed", "Past Results", History],
  ["shadow", "Test Lab", FlaskConical],
];
const REVIEW_STAGES = [
  ["8:30", 510, "llm_8m30_decision", "llm_8m30_reason"],
  ["6:30", 390, "llm_early_decision", "llm_early_reason"],
  ["4:30", 270, "llm_4m30_decision", "llm_4m30_reason"],
  ["2:30", 150, "llm_late_decision", "llm_late_reason"],
  ["1:00", 60, "llm_1m_decision", "llm_1m_reason"],
];

function MarketIcon({ indexId, size = 20 }) {
  if (indexId === "BRTI") return <Bitcoin size={size} aria-hidden="true" />;
  if (indexId === "SOLUSD_RTI") return <Orbit size={size} aria-hidden="true" />;
  return <CircleDot size={size} aria-hidden="true" />;
}

function CopyTicker({ ticker }) {
  const [copied, setCopied] = React.useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(ticker);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {}
  };
  return <button className="icon-button copy-button" type="button" onClick={copy} title="Copy market ticker" aria-label={`Copy ${ticker}`}>
    {copied ? <Check size={15} /> : <Copy size={15} />}<span className="copy-label">{copied ? "Copied" : "Copy"}</span>
  </button>;
}

function ProbabilityBars({ modelProbability, marketProbability }) {
  const values = [
    ["Bot says YES", modelProbability, "model"],
    ["Market says YES", marketProbability, "market"],
  ];
  return <section className="probabilities" aria-label="Bot and market chances">
    {values.map(([label, value, kind]) => {
      const numeric = Number(value);
      const width = Number.isFinite(numeric) ? Math.min(100, Math.max(0, numeric * 100)) : 0;
      return <div className="probability" key={kind}>
        <div><span>{label}</span><strong>{percent(value)} YES</strong></div>
        <div className="probability-track" aria-hidden="true"><span className={`probability-fill ${kind}`} style={{ width: `${width}%` }} /></div>
      </div>;
    })}
  </section>;
}

function Confirmation({ market }) {
  let checks = [];
  try { checks = market.decision_confirmation_detail ? JSON.parse(market.decision_confirmation_detail) : []; } catch {}
  const agree = market.decision_confirmation_agree;
  const total = market.decision_confirmation_total;
  if (!checks.length && agree == null && total == null) return null;
  return <section className="confirmation" aria-label="Safety checks">
    <div className="subsection-heading"><span>Safety checks</span><strong>{agree ?? "--"}/{total ?? "--"} passed</strong></div>
    {checks.length > 0 && <div className="checks">{checks.map((check) => <span className={`check ${check.agree ? "pass" : "fail"}`} key={check.name}>{check.agree ? <Check size={13} /> : <X size={13} />}{check.name}</span>)}</div>}
  </section>;
}

function JetsonTimeline({ market, secondsRemaining }) {
  return <details className="review" open>
    <summary><span><Bot size={16} /> AI safety review</span><ChevronDown size={16} /></summary>
    <div className="review-list">{REVIEW_STAGES.map(([label, lead, decisionKey, reasonKey]) => {
      const decision = market[decisionKey];
      const status = decision || (secondsRemaining > lead ? "SCHEDULED" : "AWAITING");
      return <div className={`review-row review-${status.toLowerCase()}`} key={label}>
        <time>T-{label}</time><strong>{formatDecision(status)}</strong>
        <span>{decision ? market[reasonKey] || "No explanation was provided." : status === "SCHEDULED" ? `Checks again with ${label} left` : "Waiting for the next check."}</span>
      </div>;
    })}</div>
  </details>;
}

function Outcome({ market }) {
  if (!["yes", "no"].includes(market.result) || !market.decision_recommendation) return null;
  const predictedAbove = market.decision_recommendation === "BUY_YES";
  const settledAbove = market.result === "yes";
  const correct = predictedAbove === settledAbove;
  return <div className={`outcome ${correct ? "win" : "loss"}`}>
    <strong>{correct ? "WIN" : "MISSED"}</strong>
    <span>Finished {settledAbove ? "above" : "below"} the target. Bot picked {predictedAbove ? "above" : "below"}.</span>
  </div>;
}

function LiveGraph({ history = [], strike }) {
  if (history.length < 2) return <div className="graph graph-empty"><Activity size={18} /><span>Gathering 10-minute history</span></div>;
  const values = history.map((point) => Number(point.value)).filter(Number.isFinite);
  const numericStrike = Number(strike);
  if (values.length < 2 || !Number.isFinite(numericStrike)) return <div className="graph graph-empty">Price history unavailable</div>;
  const min = Math.min(...values, numericStrike);
  const max = Math.max(...values, numericStrike);
  const range = max - min || 1;
  const coordinates = values.map((value, index) => `${(index / (values.length - 1)) * 100},${94 - ((value - min) / range) * 80}`).join(" ");
  const strikeY = 94 - ((numericStrike - min) / range) * 80;
  const above = values.at(-1) >= numericStrike;
  return <figure className="graph">
    <svg viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="Live ten-minute price history">
      {[20, 50, 80].map((value) => <line className="graph-grid" x1="0" x2="100" y1={value} y2={value} key={value} />)}
      <line className="graph-strike" x1="0" x2="100" y1={strikeY} y2={strikeY} />
      <polyline className={`graph-line ${above ? "up" : "down"}`} points={coordinates} />
    </svg>
    <span className="graph-high">High {money(max)}</span><span className="graph-goal">Goal {money(strike)}</span><span className="graph-low">Low {money(min)}</span>
    <figcaption>Last 10 minutes</figcaption>
  </figure>;
}

function WhalePanel({ ticker, onClose, returnFocusRef }) {
  const [minimum, setMinimum] = React.useState(() => Number(localStorage.getItem("whaleMinUsd")) || 100);
  const [customMinimum, setCustomMinimum] = React.useState(String(minimum));
  const [trades, setTrades] = React.useState([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState(false);
  const closeRef = React.useRef(null);

  const close = React.useCallback(() => { onClose(); window.setTimeout(() => returnFocusRef.current?.focus(), 0); }, [onClose, returnFocusRef]);
  React.useEffect(() => {
    closeRef.current?.focus();
    const onKeyDown = (event) => { if (event.key === "Escape") close(); };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [close]);

  React.useEffect(() => {
    const controller = new AbortController();
    const load = async () => {
      setLoading(true); setError(false);
      try {
        const response = await fetch(`/api/whale-trades?ticker=${encodeURIComponent(ticker)}&limit=30&min_usd=${minimum}`, { cache: "no-store", signal: controller.signal });
        if (!response.ok) throw new Error(String(response.status));
        setTrades(await response.json());
      } catch (nextError) { if (nextError.name !== "AbortError") setError(true); }
      finally { if (!controller.signal.aborted) setLoading(false); }
    };
    void load();
    return () => controller.abort();
  }, [minimum, ticker]);

  const chooseMinimum = (value) => {
    const next = Math.max(0, Number(value) || 0);
    setMinimum(next); setCustomMinimum(String(next)); localStorage.setItem("whaleMinUsd", String(next));
  };

  return <div className="whale-overlay" onMouseDown={(event) => { if (event.target === event.currentTarget) close(); }}>
    <section className="whale-panel" role="dialog" aria-modal="true" aria-label={`Whale Tracker for ${ticker}`}>
      <header><div><span className="eyebrow"><Fish size={14} /> Whale Tracker</span><h3>Recent large trades</h3><code>{ticker}</code></div><button ref={closeRef} className="icon-button close-button" type="button" onClick={close} aria-label="Close Whale Tracker"><X size={18} /></button></header>
      <div className="whale-controls" aria-label="Minimum trade amount">{[0, 50, 100, 250, 500, 1000].map((value) => <button type="button" className={minimum === value ? "selected" : ""} key={value} onClick={() => chooseMinimum(value)}>{value ? `$${value}+` : "All"}</button>)}</div>
      <form className="custom-minimum" onSubmit={(event) => { event.preventDefault(); chooseMinimum(customMinimum); }}><label htmlFor={`whale-min-${ticker}`}>Custom minimum</label><div><span>$</span><input id={`whale-min-${ticker}`} inputMode="decimal" type="number" min="0" step="10" value={customMinimum} onChange={(event) => setCustomMinimum(event.target.value)} /><button type="submit">Apply</button></div></form>
      <div className="whale-list" aria-live="polite">{loading ? <div className="panel-state"><RefreshCw className="spin" size={18} />Looking for trades</div> : error ? <div className="panel-state error"><AlertTriangle size={18} />Trades are unavailable</div> : trades.length ? trades.map((trade) => <div className="whale-row" key={`${trade.trade_id}-${trade.ts_ms}`}><span className={trade.side === "yes" ? "side-yes" : "side-no"}>{trade.side === "yes" ? "YES / UP" : "NO / DOWN"}</span><strong>{money(trade.notional_usd)}</strong><span>@ {Number(trade.price_cents || 0).toFixed(1)}c</span><time>{new Date(trade.ts_ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time></div>) : <div className="panel-state">No trades of {money(minimum)} or more.</div>}</div>
      <button className="back-to-signal" type="button" onClick={close}><ArrowLeft size={16} /> Back to pick</button>
    </section>
  </div>;
}

function MarketCard({ market, livePrice, history, now, decisionLeadSec, closed }) {
  const [whalesOpen, setWhalesOpen] = React.useState(false);
  const whaleButtonRef = React.useRef(null);
  const meta = coinMeta(market.index_id);
  const currentPrice = livePrice ?? market.index_price;
  const recommendation = market.decision_recommendation || "PENDING";
  const liveRecommendation = market.recommendation || "NO_EDGE";
  const remaining = Number(market.close_ts_ms) - now;
  const secondsRemaining = Math.max(0, remaining / 1000);
  const decisionRemaining = remaining - decisionLeadSec * 1000;
  const gap = currentPrice == null ? null : Number(currentPrice) - Number(market.strike);
  const confidence = market.decision_confidence;
  const lockedModel = market.decision_model_p_yes ?? market.model_p_yes;
  const lockedEdge = market.decision_edge ?? market.edge;
  const liveBias = liveRecommendation === "BUY_YES" ? "Leaning up" : liveRecommendation === "BUY_NO" ? "Leaning down" : "Market appears fairly priced";
  return <article className={`market-card ${closed ? "market-closed" : ""}`} style={{ "--accent": meta.accent }}>
    <header className="market-header"><div className="coin-mark"><MarketIcon indexId={market.index_id} /></div><div className="market-identity"><div><h2>{meta.name}</h2><span>15 min</span></div><code>{market.ticker}</code></div><CopyTicker ticker={market.ticker} /><div className={`market-time ${secondsRemaining < 60 ? "urgent" : ""}`}><span><Radio size={12} />{closed ? "Closed" : "Live"}</span><strong>{closed ? "Settled" : countdown(remaining)}</strong></div></header>
    <section className={`signal signal-${recommendation.toLowerCase()}`}><span className="signal-kicker">{closed ? "Final pick" : recommendation === "PENDING" ? `Final pick in ${countdown(decisionRemaining)}` : "Final pick"}</span><strong>{recommendationLabel(recommendation)}</strong><span>{recommendation === "PENDING" ? `${liveBias} · ${percent(Math.abs(Number(market.edge || 0)))} price advantage` : `${percent(confidence)} confidence`}</span><b>{closed ? "Pick saved" : `Market closes in ${countdown(remaining)}`}</b></section>
    <div className="card-action-row">{market.result && <span className={`result result-${market.result}`}>Result: {market.result.toUpperCase()}</span>}<button ref={whaleButtonRef} className="secondary-button whale-button" type="button" onClick={() => setWhalesOpen(true)}><Fish size={16} /> Whale Tracker</button></div>
    <Outcome market={market} />
    <Confirmation market={market} />
    {recommendation !== "PENDING" && <section className="metric-grid" aria-label="Saved pick details"><div><span>Price advantage</span><strong>{lockedEdge == null ? "--" : `${Number(lockedEdge) >= 0 ? "+" : ""}${(Number(lockedEdge) * 100).toFixed(1)} pts`}</strong></div><div><span>Chance to win</span><strong>{recommendation === "BUY_NO" ? percent(1 - Number(lockedModel)) : percent(lockedModel)}</strong></div><div><span>Price when picked</span><strong>{money(market.decision_index_price)}</strong></div></section>}
    <section className="price-grid" aria-label="Current prices"><div><span>Current price</span><strong>{money(currentPrice)}</strong></div><div><span>Price to beat</span><strong>{money(market.strike)}</strong></div><div><span>Distance</span><strong className={gap == null ? "" : gap >= 0 ? "positive" : "negative"}>{gap == null ? "--" : `${gap >= 0 ? "+" : ""}${money(gap)}`}</strong></div></section>
    <LiveGraph history={history} strike={market.strike} />
    <ProbabilityBars modelProbability={market.model_p_yes} marketProbability={market.market_p_yes} />
    <JetsonTimeline market={market} secondsRemaining={secondsRemaining} />
    {whalesOpen && <WhalePanel ticker={market.ticker} onClose={() => setWhalesOpen(false)} returnFocusRef={whaleButtonRef} />}
  </article>;
}

function ShadowCard({ market, livePrice, now }) {
  const meta = coinMeta(market.index_id);
  const remaining = Number(market.close_ts_ms) - now;
  const edge = Number(market.edge);
  return <article className="market-card shadow-card" style={{ "--accent": meta.accent }}>
    <header className="market-header"><div className="coin-mark"><MarketIcon indexId={market.index_id} /></div><div className="market-identity"><div><h2>{meta.name}</h2><span>Test</span></div><code>{market.ticker}</code></div><div className="market-time"><span><FlaskConical size={12} />Test model</span><strong>{countdown(remaining)}</strong></div></header>
    <section className="signal signal-shadow"><span className="signal-kicker">New test model</span><strong>{recommendationLabel(market.recommendation)}</strong><span>{percent(market.model_p_yes)} chance of YES</span></section>
    <ProbabilityBars modelProbability={market.model_p_yes} marketProbability={market.market_p_yes} />
    <section className="price-grid"><div><span>Current price</span><strong>{money(livePrice ?? market.index_price)}</strong></div><div><span>Price to beat</span><strong>{money(market.strike)}</strong></div><div><span>Advantage</span><strong className={edge >= 0 ? "positive" : "negative"}>{Number.isFinite(edge) ? `${edge >= 0 ? "+" : ""}${(edge * 100).toFixed(1)} pts` : "--"}</strong></div><div><span>Checks passed</span><strong>{market.confirmation_agree ?? "--"}/{market.confirmation_total ?? "--"}</strong></div></section>
  </article>;
}

function CalibrationHud({ calibration, external }) {
  const cards = [["Overall", "ALL", calibration], ["Bitcoin", "BTC", calibration?.BRTI], ["Solana", "SOL", calibration?.SOLUSD_RTI]];
  const sources = external?.sources || [];
  return <section className="telemetry glass-panel"><header><div><span className="eyebrow"><Trophy size={14} /> Past performance</span><h2>Track record</h2><small className="helper-text">Lower error scores are better.</small></div><details className={`feed-status ${external?.healthy ? "healthy" : "stale"}`}><summary><Radio size={13} />Price sources {external?.healthy ? "live" : sources.length ? "delayed" : "waiting"}<ChevronDown size={13} /></summary><div>{sources.length ? sources.map((source) => <span key={`${source.source}-${source.symbol}`}><b>{source.source} {source.symbol}</b><small>{source.healthy ? "Live" : `${Math.round(Number(source.age_ms || 0) / 1000)}s old`}</small></span>) : <span>No prices received yet</span>}</div></details></header>
    <div className="hud-grid">{cards.map(([title, symbol, card]) => <article className="hud-card" key={symbol}><div><span>{symbol}</span><strong>{title}</strong></div>{card?.n ? <dl><div><dt>Bot error</dt><dd>{Number(card.model_brier).toFixed(3)}</dd></div><div><dt>Market error</dt><dd>{Number(card.market_brier).toFixed(3)}</dd></div><div><dt>Finished</dt><dd>{card.settled_count ?? card.n}</dd></div></dl> : <p>Waiting for finished markets</p>}</article>)}</div>
  </section>;
}

function ShadowComparison({ data }) {
  if (!data?.groups?.length) return <div className="empty-state compact">The test model is waiting for its first saved pick.</div>;
  return <section className="shadow-comparison">{data.groups.map((group) => {
    const live = group.scores?.live || {}; const shadow = group.scores?.shadow || {}; const market = group.scores?.market || {};
    const better = shadow.n > 0 && shadow.brier < live.brier;
    return <article className={`comparison-card ${better ? "better" : ""}`} key={`${group.experiment_id}-${group.index_id}`}><header><div><span>{coinMeta(group.index_id).name}</span><strong>{group.experiment_id}</strong></div><b>{better ? "Doing better" : "Need more results"}</b></header><dl><div><dt>Current bot error</dt><dd>{live.brier == null ? "--" : Number(live.brier).toFixed(3)}</dd></div><div><dt>Test bot error</dt><dd>{shadow.brier == null ? "--" : Number(shadow.brier).toFixed(3)}</dd></div><div><dt>Market error</dt><dd>{market.brier == null ? "--" : Number(market.brier).toFixed(3)}</dd></div></dl><p>{shadow.n || 0} finished · {group.pending || 0} waiting · {shadow.actionable_correct || 0}/{shadow.actionable_n || 0} test picks correct</p></article>;
  })}</section>;
}

export default function App() {
  const [tab, setTab] = React.useState(() => location.pathname === "/shadow" ? "shadow" : "active");
  const [coin, setCoin] = React.useState("all");
  const [query, setQuery] = React.useState("");
  const [now, setNow] = React.useState(Date.now());
  const dashboard = useDashboardData(tab);
  const telemetry = useTelemetry(tab);
  const indexIds = ["BRTI", "SOLUSD_RTI", ...dashboard.rows.map((row) => row.index_id)];
  const live = useLivePrices(indexIds);

  React.useEffect(() => {
    const interval = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(interval);
  }, []);

  const normalizedQuery = query.trim().toLowerCase();
  const filtered = dashboard.rows.filter((row) => (coin === "all" || row.index_id === coin) && (!normalizedQuery || [row.ticker, row.index_id, row.strike].some((value) => String(value || "").toLowerCase().includes(normalizedQuery))));
  const overallStatus = dashboard.error ? "DATA OFFLINE" : live.socketStatus === "LIVE" ? "LIVE" : live.socketStatus === "OFFLINE" ? "DATA OFFLINE" : "CONNECTING";
  const selectTab = (nextTab) => {
    setTab(nextTab);
    history.replaceState(null, "", nextTab === "shadow" ? "/shadow" : "/");
  };

  return <main className="shell" id="main-content">
    <header className="topbar"><a className="brand" href="#main-content" aria-label="AQLabs dashboard home"><span className="brand-mark">AQ</span><span><strong>AQLABS</strong><small>CRYPTO PICK TRACKER</small></span></a><div className="connection"><span className={`status-dot ${overallStatus === "LIVE" ? "live" : ""}`} /><div><strong>{overallStatus}</strong><small>{live.lastTickAt ? `Price updated ${live.lastTickAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}` : dashboard.lastUpdated ? `Updated ${dashboard.lastUpdated.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}` : "Waiting for data"}</small></div></div></header>

    <section className="workspace-heading"><div><span className="eyebrow"><Activity size={15} /> Live tracker</span><h1>Crypto picks</h1><p>Simple 15-minute Bitcoin and Solana picks.</p></div><div className="read-only"><ShieldCheck size={18} /><span><strong>Watching only</strong><small>This app never places bets</small></span></div></section>

    <CalibrationHud calibration={telemetry.calibration} external={telemetry.external} />
    {dashboard.error && <div className="alert" role="alert"><AlertTriangle size={17} /><span><strong>Live data connection lost.</strong> Showing the last update while we reconnect.</span><button className="icon-button" type="button" onClick={() => dashboard.reload()} aria-label="Try reconnecting"><RefreshCw size={16} /></button></div>}

    <nav className="toolbar glass-panel" aria-label="Market views and filters"><div className="tabs" role="tablist">{TABS.map(([key, label, Icon]) => <button role="tab" aria-selected={tab === key} className={tab === key ? "active" : ""} key={key} onClick={() => selectTab(key)}><Icon size={15} aria-hidden="true" />{label}{tab === key && <span>{dashboard.rows.length}</span>}</button>)}</div><div className="filters"><div className="coin-filter" aria-label="Filter by coin">{[["all", "All"], ["BRTI", "BTC"], ["SOLUSD_RTI", "SOL"]].map(([key, label]) => <button className={coin === key ? "active" : ""} type="button" key={key} onClick={() => setCoin(key)}>{label}</button>)}</div><label className="search"><Search size={16} /><span className="sr-only">Search markets</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search markets" /></label></div></nav>

    {tab === "shadow" && <><div className="shadow-warning"><AlertTriangle size={18} /><div><strong>Test model only</strong><span>This is an experiment. Do not use these picks to bet.</span></div></div><ShadowComparison data={telemetry.shadowComparison} /></>}

    <section className="section-heading"><div><span className="eyebrow"><Waves size={15} />{tab === "active" ? "Happening now" : tab === "closed" ? "History" : "Test picks"}</span><h2>{filtered.length} {tab === "active" ? "live picks" : tab === "closed" ? "past results" : "test picks"}</h2></div>{dashboard.loading && <span className="loading-label"><RefreshCw className="spin" size={14} />Updating</span>}</section>

    <section className="markets" aria-live="polite">{!dashboard.loading && !filtered.length ? <div className="empty-state"><Waves size={24} /><strong>No markets found</strong><span>Try another coin or search.</span></div> : filtered.map((market) => tab === "shadow" ? <ShadowCard key={market.ticker} market={market} livePrice={live.livePrices[market.index_id]} now={now} /> : <MarketCard key={market.ticker} market={market} livePrice={live.livePrices[market.index_id]} history={live.histories[market.index_id]} now={now} decisionLeadSec={telemetry.decisionLeadSec} closed={tab === "closed" || market.status === "closed"} />)}</section>

    {tab === "closed" && dashboard.hasMore && <div className="load-more"><button className="secondary-button" type="button" disabled={dashboard.loadingMore} onClick={dashboard.loadMore}>{dashboard.loadingMore ? <RefreshCw className="spin" size={16} /> : <ChevronDown size={16} />}{dashboard.loadingMore ? "Loading" : "Show more results"}</button></div>}
  </main>;
}
