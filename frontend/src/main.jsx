import React from "react";
import { createRoot } from "react-dom/client";
import { Activity, AlertTriangle, Bot, CircleDot, Radio, ShieldCheck, Waves } from "lucide-react";
import "./styles.css";

const coinMeta = {
  BRTI: { name: "Bitcoin", symbol: "BTC", accent: "#f7b955" },
  SOLUSD_RTI: { name: "Solana", symbol: "SOL", accent: "#59d6d0" },
};

function money(value) {
  return value == null || Number.isNaN(Number(value)) ? "--" : `$${Number(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function percent(value) {
  return value == null ? "--" : `${(Number(value) * 100).toFixed(1)}%`;
}

function Review({ market }) {
  const reviews = [
    ["6:30 review", market.llm_early_decision, market.llm_early_reason],
    ["2:30 review", market.llm_late_decision, market.llm_late_reason],
  ].filter(([, decision]) => decision);
  if (!reviews.length) return null;
  return (
    <section className="review" aria-label="Jetson LLM risk review">
      <div className="review-title"><Bot size={16} /> JETSON REVIEW</div>
      {reviews.map(([stage, decision, reason]) => (
        <div className={`review-row review-${decision.toLowerCase()}`} key={stage}>
          <span>{stage}</span><strong>{decision.replace("_", " ")}</strong><small>{reason}</small>
        </div>
      ))}
    </section>
  );
}

function Checks({ market }) {
  if (!market.decision_confirmation_detail) return null;
  let checks = [];
  try { checks = JSON.parse(market.decision_confirmation_detail); } catch { return null; }
  return <div className="checks">{checks.map((check) => <span className={check.agree ? "check pass" : "check"} key={check.name}><CircleDot size={12} />{check.name}</span>)}</div>;
}

function MarketCard({ market }) {
  const meta = coinMeta[market.index_id] || { name: market.index_id, symbol: market.index_id, accent: "#a8b4c7" };
  const recommendation = market.decision_recommendation || "PENDING";
  const direction = recommendation === "BUY_YES" ? "BET UP" : recommendation === "BUY_NO" ? "BET DOWN" : recommendation === "NO_EDGE" ? "NO TRADE" : "LOCKING IN";
  const currentPrice = market.index_price;
  const gap = currentPrice == null ? null : Number(currentPrice) - Number(market.strike);
  return (
    <article className="market-card" style={{ "--accent": meta.accent }}>
      <header className="market-header">
        <div className="coin-mark">{meta.symbol}</div>
        <div><h2>{meta.name} <em>15m</em></h2><code>{market.ticker}</code></div>
        <div className="market-time"><Radio size={14} /> LIVE</div>
      </header>
      <div className={`signal signal-${recommendation.toLowerCase()}`}>
        <span className="signal-kicker">OFFICIAL SIGNAL</span><strong>{direction}</strong>
        <span>{market.decision_recommendation ? `${percent(market.decision_confidence)} confidence` : "Awaiting model lock"}</span>
      </div>
      <Checks market={market} />
      <Review market={market} />
      <div className="price-grid">
        <div><label>LIVE INDEX</label><strong>{money(currentPrice)}</strong></div>
        <div><label>GOAL PRICE</label><strong>{money(market.strike)}</strong></div>
        <div><label>GAP TO GOAL</label><strong className={gap >= 0 ? "positive" : "negative"}>{gap == null ? "--" : `${gap >= 0 ? "+" : ""}${money(gap)}`}</strong></div>
      </div>
      <footer><span>Model {percent(market.model_p_yes)} above</span><span>Market {percent(market.market_p_yes)}</span><span>Decision checks {market.decision_confirmation_agree ?? "--"}/{market.decision_confirmation_total ?? "--"}</span></footer>
    </article>
  );
}

function App() {
  const [markets, setMarkets] = React.useState([]);
  const [status, setStatus] = React.useState("CONNECTING");
  const [lastUpdated, setLastUpdated] = React.useState(null);
  const load = React.useCallback(async () => {
    try {
      const response = await fetch("/api/active", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setMarkets(await response.json()); setStatus("LIVE"); setLastUpdated(new Date());
    } catch { setStatus("API OFFLINE"); }
  }, []);
  React.useEffect(() => {
    load();
    const interval = setInterval(load, 5000);
    const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/live`);
    socket.onopen = () => setStatus("LIVE");
    socket.onerror = () => setStatus("API OFFLINE");
    return () => { clearInterval(interval); socket.close(); };
  }, [load]);
  return (
    <main className="shell">
      <header className="topbar"><div className="brand"><span className="brand-mark">AQ</span><div><strong>AQLABS</strong><small>QUANTITATIVE CRYPTO ENGINE</small></div></div><div className="status"><span className={status === "LIVE" ? "status-dot live" : "status-dot"}></span>{status}<span className="updated">{lastUpdated ? lastUpdated.toLocaleTimeString() : "--"}</span></div></header>
      <section className="intro"><div><p className="eyebrow"><Activity size={15} /> MARKET MONITOR</p><h1>15-minute signal intelligence</h1><p className="lede">Python owns the data engine. React owns the decision surface.</p></div><div className="monitor"><ShieldCheck size={19} /> READ-ONLY MONITOR MODE</div></section>
      {status === "API OFFLINE" && <div className="alert"><AlertTriangle size={17} /> Python API is unavailable. The frontend will retry automatically.</div>}
      <section className="section-heading"><div><span className="eyebrow"><Waves size={15} /> LIVE MARKETS</span><h2>{markets.length} active markets</h2></div><span className="polling">Polling every 5 seconds</span></section>
      <section className="markets">{markets.length ? markets.map((market) => <MarketCard market={market} key={market.ticker} />) : <div className="empty">Waiting for active market telemetry...</div>}</section>
    </main>
  );
}

createRoot(document.getElementById("root")).render(<App />);
