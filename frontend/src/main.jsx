import React from "react";
import { createRoot } from "react-dom/client";
import { Activity, AlertTriangle, BarChart3, Bot, CircleDot, Radio, Search, ShieldCheck, Waves } from "lucide-react";
import "./styles.css";

const coinMeta = {
  BRTI: { name: "Bitcoin", symbol: "BTC", accent: "#f7b955" },
  SOLUSD_RTI: { name: "Solana", symbol: "SOL", accent: "#59d6d0" },
};
const tabs = { active: "/api/active", closed: "/api/closed?limit=200", shadow: "/api/shadow-active" };

function money(value) { return value == null || Number.isNaN(Number(value)) ? "--" : `$${Number(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`; }
function percent(value) { return value == null ? "--" : `${(Number(value) * 100).toFixed(1)}%`; }
function metaFor(indexId) { return coinMeta[indexId] || { name: indexId || "Unknown", symbol: indexId || "--", accent: "#a8b4c7" }; }

function Review({ market }) {
  const reviews = [["6:30 review", market.llm_early_decision, market.llm_early_reason], ["2:30 review", market.llm_late_decision, market.llm_late_reason]].filter(([, decision]) => decision);
  if (!reviews.length) return null;
  return <section className="review" aria-label="Jetson LLM risk review"><div className="review-title"><Bot size={16} /> JETSON REVIEW</div>{reviews.map(([stage, decision, reason]) => <div className={`review-row review-${decision.toLowerCase()}`} key={stage}><span>{stage}</span><strong>{decision.replace("_", " ")}</strong><small>{reason || "No explanation returned."}</small></div>)}</section>;
}

function Checks({ market }) {
  if (!market.decision_confirmation_detail) return null;
  let checks = [];
  try { checks = JSON.parse(market.decision_confirmation_detail); } catch { return null; }
  return <div className="checks">{checks.map((check) => <span className={check.agree ? "check pass" : "check"} key={check.name}><CircleDot size={12} />{check.name}</span>)}</div>;
}

function WhalePanel({ ticker, onClose }) {
  const [minimum, setMinimum] = React.useState(100);
  const [trades, setTrades] = React.useState([]);
  const [loading, setLoading] = React.useState(true);
  const load = React.useCallback(async () => {
    setLoading(true);
    try { const response = await fetch(`/api/whale-trades?ticker=${encodeURIComponent(ticker)}&limit=30&min_usd=${minimum}`); setTrades(response.ok ? await response.json() : []); } catch { setTrades([]); } finally { setLoading(false); }
  }, [ticker, minimum]);
  React.useEffect(() => { load(); }, [load]);
  return <div className="whale-panel"><div className="whale-panel-head"><div><span className="eyebrow"><Waves size={14} /> ORDER FLOW</span><strong>Anonymous large fills</strong></div><button className="icon-btn" onClick={onClose} aria-label="Close whale tracker">×</button></div><div className="whale-controls">{[0, 50, 100, 250, 500, 1000].map((value) => <button className={minimum === value ? "selected" : ""} key={value} onClick={() => setMinimum(value)}>{value ? `$${value}+` : "ALL"}</button>)}</div>{loading ? <div className="whale-empty">Scanning order-book fills...</div> : trades.length ? <div className="whale-list">{trades.map((trade) => <div className="whale-row" key={`${trade.trade_id}-${trade.ts_ms}`}><span className={trade.side === "yes" ? "side-yes" : "side-no"}>{trade.side === "yes" ? "YES / UP" : "NO / DOWN"}</span><strong>{money(trade.notional_usd)}</strong><span>@ {Number(trade.price_cents || 0).toFixed(1)}c</span><small>{new Date(trade.ts_ms).toLocaleTimeString()}</small></div>)}</div> : <div className="whale-empty">No fills at or above {money(minimum)}.</div>}</div>;
}

function MarketCard({ market, livePrices, closed = false }) {
  const meta = metaFor(market.index_id);
  const [whalesOpen, setWhalesOpen] = React.useState(false);
  const currentPrice = livePrices[market.index_id] ?? market.index_price;
  const recommendation = market.decision_recommendation || "PENDING";
  const direction = recommendation === "BUY_YES" ? "BET UP" : recommendation === "BUY_NO" ? "BET DOWN" : recommendation === "NO_EDGE" ? "NO TRADE" : "LOCKING IN";
  const gap = currentPrice == null ? null : Number(currentPrice) - Number(market.strike);
  return <article className={`market-card ${closed ? "market-closed" : ""}`} style={{ "--accent": meta.accent }}><header className="market-header"><div className="coin-mark">{meta.symbol}</div><div><h2>{meta.name} <em>15m</em></h2><code>{market.ticker}</code></div><div className="market-time"><Radio size={14} /> {closed ? "CLOSED" : "LIVE"}</div></header>{whalesOpen ? <WhalePanel ticker={market.ticker} onClose={() => setWhalesOpen(false)} /> : <><div className={`signal signal-${recommendation.toLowerCase()}`}><span className="signal-kicker">{closed ? "SETTLED DECISION" : "OFFICIAL SIGNAL"}</span><strong>{direction}</strong><span>{market.decision_recommendation ? `${percent(market.decision_confidence)} confidence` : "Awaiting model lock"}</span></div><div className="card-actions"><button onClick={() => setWhalesOpen(true)}><Waves size={14} /> WHALE TRACKER</button>{market.result && <span className={market.result === "yes" ? "result-yes" : "result-no"}>RESULT: {market.result.toUpperCase()}</span>}</div><Checks market={market} /><Review market={market} /><div className="price-grid"><div><label>LIVE INDEX</label><strong>{money(currentPrice)}</strong></div><div><label>GOAL PRICE</label><strong>{money(market.strike)}</strong></div><div><label>GAP TO GOAL</label><strong className={gap >= 0 ? "positive" : "negative"}>{gap == null ? "--" : `${gap >= 0 ? "+" : ""}${money(gap)}`}</strong></div></div><footer><span>Model {percent(market.model_p_yes)} above</span><span>Market {percent(market.market_p_yes)}</span><span>Checks {market.decision_confirmation_agree ?? "--"}/{market.decision_confirmation_total ?? "--"}</span></footer></>}</article>;
}

function ShadowCard({ market, livePrices }) {
  const meta = metaFor(market.index_id);
  const price = livePrices[market.index_id] ?? market.index_price;
  return <article className="market-card shadow-card" style={{ "--accent": meta.accent }}><header className="market-header"><div className="coin-mark">{meta.symbol}</div><div><h2>{meta.name} <em>SHADOW</em></h2><code>{market.ticker}</code></div><div className="market-time"><BarChart3 size={14} /> EXPERIMENT</div></header><div className="signal signal-shadow"><span className="signal-kicker">REGULARIZED CHALLENGER</span><strong>{market.recommendation || "PENDING"}</strong><span>{percent(market.model_p_yes)} model probability</span></div><div className="price-grid"><div><label>INDEX</label><strong>{money(price)}</strong></div><div><label>GOAL</label><strong>{money(market.strike)}</strong></div><div><label>CONFIRMATION</label><strong>{market.confirmation_agree ?? "--"}/{market.confirmation_total ?? "--"}</strong></div></div></article>;
}

function Hud({ calibration, external }) {
  const cards = [["Overall Engine", "ALL", calibration], ["Bitcoin Model", "BTC", calibration?.BRTI], ["Solana Model", "SOL", calibration?.SOLUSD_RTI]];
  return <section className="hud"><div className="hud-head"><div><span className="eyebrow"><BarChart3 size={14} /> PERFORMANCE & CALIBRATION</span><h2>Accuracy telemetry</h2></div><span className={`feed-status ${external?.healthy ? "healthy" : "stale"}`}><Radio size={13} /> {external?.healthy ? "EXTERNAL FEED LIVE" : "EXTERNAL FEED STALE"}</span></div><div className="hud-grid">{cards.map(([title, symbol, card]) => <div className="hud-card" key={title}><div className="hud-card-top"><span>{symbol}</span><strong>{title}</strong></div>{card?.n ? <div className="hud-values"><div><label>MODEL BRIER</label><b>{Number(card.model_brier).toFixed(3)}</b></div><div><label>MARKET BRIER</label><b>{Number(card.market_brier).toFixed(3)}</b></div><div><label>SETTLED</label><b>{card.n}</b></div></div> : <div className="hud-empty">Collecting calibration data...</div>}</div>)}</div></section>;
}

function App() {
  const [tab, setTab] = React.useState("active");
  const [coin, setCoin] = React.useState("all");
  const [query, setQuery] = React.useState("");
  const [rows, setRows] = React.useState([]);
  const [livePrices, setLivePrices] = React.useState({});
  const [calibration, setCalibration] = React.useState(null);
  const [external, setExternal] = React.useState(null);
  const [status, setStatus] = React.useState("CONNECTING");
  const [lastUpdated, setLastUpdated] = React.useState(null);
  const load = React.useCallback(async () => { try { const response = await fetch(tabs[tab], { cache: "no-store" }); if (!response.ok) throw new Error(); setRows(await response.json()); setStatus("LIVE"); setLastUpdated(new Date()); } catch { setStatus("API OFFLINE"); } }, [tab]);
  React.useEffect(() => { load(); const interval = setInterval(load, tab === "shadow" ? 30000 : 5000); return () => clearInterval(interval); }, [load, tab]);
  React.useEffect(() => { Promise.all([fetch("/api/calibration").then((r) => r.json()), fetch("/api/calibration?index_id=BRTI").then((r) => r.json()), fetch("/api/calibration?index_id=SOLUSD_RTI").then((r) => r.json())]).then(([all, btc, sol]) => setCalibration({ ...all, BRTI: btc, SOLUSD_RTI: sol })).catch(() => {}); const interval = setInterval(() => fetch("/api/external-status").then((r) => r.json()).then(setExternal).catch(() => {}), 5000); return () => clearInterval(interval); }, []);
  React.useEffect(() => { const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/live`); socket.onmessage = (event) => { try { const message = JSON.parse(event.data); if (message.type === "index_tick") setLivePrices((old) => ({ ...old, [message.index_id]: message.value })); } catch {} }; socket.onopen = () => setStatus("LIVE"); socket.onerror = () => setStatus("API OFFLINE"); return () => socket.close(); }, []);
  const filtered = rows.filter((row) => (coin === "all" || row.index_id === coin) && (!query || [row.ticker, row.index_id, row.strike].some((value) => String(value || "").toLowerCase().includes(query.toLowerCase()))));
  return <main className="shell"><header className="topbar"><div className="brand"><span className="brand-mark">AQ</span><div><strong>AQLABS</strong><small>QUANTITATIVE CRYPTO ENGINE</small></div></div><div className="status"><span className={status === "LIVE" ? "status-dot live" : "status-dot"}></span>{status}<span className="updated">{lastUpdated ? lastUpdated.toLocaleTimeString() : "--"}</span></div></header><section className="intro"><div><p className="eyebrow"><Activity size={15} /> MARKET MONITOR</p><h1>15-minute signal intelligence</h1><p className="lede">Prediction, calibration, order flow, and Jetson risk review in one surface.</p></div><div className="monitor"><ShieldCheck size={19} /> READ-ONLY MONITOR MODE</div></section><Hud calibration={calibration} external={external} />{status === "API OFFLINE" && <div className="alert"><AlertTriangle size={17} /> Python API is unavailable. Retrying automatically.</div>}<nav className="toolbar"><div className="tabs">{[["active", "Active Markets"], ["closed", "Closed History"], ["shadow", "Shadow Lab"]].map(([key, label]) => <button className={tab === key ? "active" : ""} key={key} onClick={() => setTab(key)}>{label}<b>{tab === key ? rows.length : ""}</b></button>)}</div><div className="filters"><button className={coin === "all" ? "active" : ""} onClick={() => setCoin("all")}>ALL</button><button className={coin === "BRTI" ? "active" : ""} onClick={() => setCoin("BRTI")}>BTC</button><button className={coin === "SOLUSD_RTI" ? "active" : ""} onClick={() => setCoin("SOLUSD_RTI")}>SOL</button><label><Search size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter ticker or goal" /></label></div></nav><section className="section-heading"><div><span className="eyebrow"><Waves size={15} /> {tab.toUpperCase()} FEED</span><h2>{filtered.length} {tab === "active" ? "active markets" : tab === "closed" ? "closed markets" : "shadow forecasts"}</h2></div><span className="polling">Updated {lastUpdated ? lastUpdated.toLocaleTimeString() : "--"}</span></section><section className="markets">{filtered.length ? filtered.map((market) => tab === "shadow" ? <ShadowCard market={market} livePrices={livePrices} key={market.ticker} /> : <MarketCard market={market} livePrices={livePrices} closed={tab === "closed"} key={market.ticker} />) : <div className="empty">No markets match the current view.</div>}</section></main>;
}

createRoot(document.getElementById("root")).render(<App />);
