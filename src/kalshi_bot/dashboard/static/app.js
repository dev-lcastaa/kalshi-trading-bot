const COIN_META = {
  BRTI: {
    name: "Bitcoin",
    pair: "BTC / USD",
    icon: "\u20BF",
    symbol: "BTC",
    accent: "#f59e0b",
    gradient: "linear-gradient(135deg, #f59e0b, #d97706)",
  },
  SOLUSD_RTI: {
    name: "Solana",
    pair: "SOL / USD",
    icon: "\u25CE",
    symbol: "SOL",
    accent: "#06b6d4",
    gradient: "linear-gradient(135deg, #06b6d4, #8b5cf6)",
  },
};

const ENDPOINTS = {
  active: "/api/active",
  closed: "/api/closed",
};

const EMPTY_MESSAGES = {
  active: "No active 15-minute BTC/SOL markets matching filters right now.",
  closed: "No closed markets found.",
};

let currentTab = "active";
let currentCoinFilter = "all";
let searchQuery = "";
let rawMarketRows = [];
let decisionLeadSec = 390; // Overwritten by /api/config

let whaleMinUsd = 100;
try {
  const stored = Number(localStorage.getItem("whaleMinUsd"));
  if (Number.isFinite(stored) && stored >= 0) {
    whaleMinUsd = stored;
  }
} catch {
  whaleMinUsd = 100;
}

// Calibration stats store: overall and per-coin
const calibrationStore = {
  overall: null,
  BRTI: null,
  SOLUSD_RTI: null,
};

// Live price state, updated by WebSocket pushes
const sparklineHistories = {}; // index_id -> [{ts_ms, value}, ...] ascending
const latestLiveByIndex = {}; // index_id -> {ts_ms, value}
const seededIndexIds = new Set();

function getCoinMeta(indexId) {
  return COIN_META[indexId] ?? {
    name: indexId ?? "Unknown",
    pair: indexId ?? "",
    icon: "\u25CF",
    symbol: indexId ?? "",
    accent: "#6366f1",
    gradient: "linear-gradient(135deg, #6366f1, #a855f7)",
  };
}

function formatUsd(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) {
    return "--";
  }
  return Number(value).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatCountdown(msRemaining) {
  if (msRemaining <= 0) return { text: "00:00", cls: "expired", rawSec: 0 };
  const totalSec = Math.floor(msRemaining / 1000);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  const text = `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  let cls = "";
  if (totalSec <= 60) {
    cls = "closing-urgent";
  } else if (totalSec <= 180) {
    cls = "closing-soon";
  }
  return { text, cls, rawSec: totalSec };
}

function formatElapsed(msElapsed) {
  const totalSec = Math.max(Math.floor(msElapsed / 1000), 0);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  if (m === 0) return `Settled ${s}s ago`;
  if (m < 60) return `Settled ${m}m ${s}s ago`;
  const h = Math.floor(m / 60);
  return `Settled ${h}h ${m % 60}m ago`;
}

function outcomeHtml(r, predictedAbove) {
  if (r.result !== "yes" && r.result !== "no") return "";
  const settledAbove = r.result === "yes";
  const correct = settledAbove === predictedAbove;
  return `
    <div class="settlement-banner ${correct ? "settlement--win" : "settlement--loss"}">
      <div class="settlement-icon">${correct ? "\u2713" : "\u2717"}</div>
      <div class="settlement-content">
        <div class="settlement-title">${correct ? "OUTCOME: WIN" : "OUTCOME: MISSED"}</div>
        <div class="settlement-detail">Settled <strong>${settledAbove ? "ABOVE GOAL" : "BELOW GOAL"}</strong> &bull; Model predicted <strong>${predictedAbove ? "ABOVE" : "BELOW"}</strong></div>
      </div>
    </div>
  `;
}

const TRADE_LABELS = {
  BUY_YES: { label: "BET UP", sub: "BUY YES (CONTRACT UNDERPRICED)", cls: "up", icon: "\u25B2" },
  BUY_NO: { label: "BET DOWN", sub: "BUY NO (CONTRACT UNDERPRICED)", cls: "down", icon: "\u25BC" },
  NO_EDGE: { label: "NO TRADE", sub: "FAIRLY PRICED", cls: "none", icon: "\u25CB" },
};

function tradeBannerHtml(r) {
  const rec = r.decision_recommendation;

  // 1. Pending Lock-in state (earlier in the 15m cycle)
  if (rec === undefined || rec === null) {
    const liveRec = (r.recommendation || "NO_EDGE").toUpperCase();
    const liveEdge = r.edge !== undefined && r.edge !== null ? (Number(r.edge) * 100).toFixed(1) : "0.0";
    let liveLean = "Monitoring order book & momentum\u2026";
    if (liveRec === "BUY_YES") {
      liveLean = `Live bias: Leaning UP (+${liveEdge}% pricing edge)`;
    } else if (liveRec === "BUY_NO") {
      liveLean = `Live bias: Leaning DOWN (+${Math.abs(Number(liveEdge))}% pricing edge)`;
    } else if (liveRec === "NO_EDGE") {
      liveLean = "Live bias: No trade (fairly priced by market)";
    }

    return `
      <div class="trade-banner trade-banner--pending">
        <div class="trade-banner__header">
          <div class="trade-banner__title-group">
            <span class="pulse-radar"></span>
            <div class="trade-banner__text">
              <span class="trade-banner__action">FINAL CALL LOCKS IN AT T-6:30</span>
              <span class="trade-banner__lean-text">${liveLean}</span>
            </div>
          </div>
          <div class="trade-banner__timer">
            <span class="timer-label">LOCKS IN</span>
            <span class="trade-banner__countdown" data-role="decision-countdown">--:--</span>
          </div>
        </div>
      </div>
    `;
  }

  // 2. Locked-in Actionable Decision
  const info = TRADE_LABELS[rec] || { label: "NO TRADE", sub: "FAIR VALUE", cls: "none", icon: "\u25CB" };
  const edgeVal = r.decision_edge !== undefined && r.decision_edge !== null ? Math.abs(Number(r.decision_edge) * 100).toFixed(1) : "0.0";
  const modelP = r.decision_model_p_yes !== undefined && r.decision_model_p_yes !== null ? Number(r.decision_model_p_yes) : 0.5;
  const winProb = rec === "BUY_NO" ? ((1 - modelP) * 100).toFixed(1) : (modelP * 100).toFixed(1);
  const lockedPrice = r.decision_index_price !== undefined && r.decision_index_price !== null
    ? Number(r.decision_index_price)
    : null;
  const goalPrice = Number(r.strike);
  const minutesLeft = r.decision_seconds_to_expiry !== undefined && r.decision_seconds_to_expiry !== null
    ? (Number(r.decision_seconds_to_expiry) / 60).toFixed(1)
    : "0.0";
  const agree = r.decision_confirmation_agree;
  const total = r.decision_confirmation_total;
  const confirmationNote =
    agree !== undefined && agree !== null && total
      ? `<span class="conf-count">${agree}/${total} momentum & book checks agreed</span>`
      : "";

  let checks = [];
  try {
    checks = r.decision_confirmation_detail ? JSON.parse(r.decision_confirmation_detail) : [];
  } catch {
    checks = [];
  }

  const checksHtml = checks.length
    ? '<div class="checks-grid">' +
      checks
        .map(
          (c) =>
            `<div class="check-pill ${c.agree ? "check-pill--pass" : "check-pill--fail"}">` +
            `<span class="check-icon">${c.agree ? "\u2713" : "\u2717"}</span>` +
            `<span class="check-name">${c.name}</span>` +
            `</div>`
        )
        .join("") +
      "</div>"
    : "";

  return `
    <div class="trade-banner trade-banner--${info.cls}">
      <div class="trade-banner__header">
        <div class="trade-banner__title-group">
          <span class="trade-banner__badge-icon">${info.icon}</span>
          <div class="trade-banner__text">
            <div class="trade-banner__main-row">
              <span class="trade-banner__main-call">${info.label}</span>
              <span class="trade-banner__sub-call">${info.sub}</span>
            </div>
            <span class="trade-banner__edge-caption">
              ${rec === "NO_EDGE" ? "Market is priced fairly with no edge." : `Locked-in Edge: <strong>+${edgeVal}% advantage</strong> over Kalshi odds (${winProb}% model win probability)`}
            </span>
            <span class="trade-banner__snapshot">
              Decision snapshot: ${lockedPrice !== null ? `$${formatUsd(lockedPrice)} vs goal $${formatUsd(goalPrice)}` : "price unavailable"}
              <span class="snapshot-note">&bull; live price below is current</span>
            </span>
          </div>
        </div>
        <div class="trade-banner__metrics">
          <div class="metric-pill">
            <span class="metric-val">${rec === "NO_EDGE" ? "0.0%" : `+${edgeVal}%`}</span>
            <span class="metric-label">MARKET EDGE</span>
          </div>
          <div class="metric-pill">
            <span class="metric-val">${winProb}%</span>
            <span class="metric-label">WIN PROB</span>
          </div>
          <div class="metric-pill">
            <span class="metric-val">${minutesLeft}m</span>
            <span class="metric-label">LOCKED AT</span>
          </div>
        </div>
      </div>
      ${confirmationNote ? `<div class="trade-banner__subbar">${confirmationNote}</div>` : ""}
      ${checksHtml}
    </div>
  `;
}

function predictionHtml(r) {
  const hasSignal = r.model_p_yes !== null && r.model_p_yes !== undefined;
  const indexPrice = r.index_price !== null && r.index_price !== undefined ? Number(r.index_price) : null;
  const strike = Number(r.strike);

  const priceDiff = indexPrice !== null ? indexPrice - strike : 0;
  const diffSign = priceDiff >= 0 ? "+" : "";
  const diffPct = strike > 0 ? ((priceDiff / strike) * 100).toFixed(2) : "0.00";

  if (!hasSignal) {
    return `
      ${tradeBannerHtml(r)}
      <div class="price-matrix">
        <div class="price-tile">
          <div class="tile-header">
            <span class="tile-label">INDEX PRICE</span>
            <span class="live-dot-tag" data-role="live-tag">LIVE</span>
          </div>
          <div class="tile-value tile-value--price" data-role="index-price">${indexPrice !== null ? "$" + formatUsd(indexPrice) : "Collecting..."}</div>
        </div>
        <div class="price-tile">
          <div class="tile-header">
            <span class="tile-label">GOAL PRICE</span>
            <span class="tile-tag">TARGET</span>
          </div>
          <div class="tile-value tile-value--strike">$${formatUsd(strike)}</div>
        </div>
        <div class="price-tile">
          <div class="tile-header">
            <span class="tile-label">GAP TO GOAL</span>
            <span class="tile-tag ${priceDiff >= 0 ? "tag--bull" : "tag--bear"}">${priceDiff >= 0 ? "ABOVE" : "BELOW"}</span>
          </div>
          <div class="tile-value ${priceDiff >= 0 ? "val--bull" : "val--bear"}" data-role="price-diff">
            ${indexPrice !== null ? `${diffSign}$${formatUsd(priceDiff)} <span class="pct-sub">(${diffSign}${diffPct}%)</span>` : "--"}
          </div>
        </div>
      </div>

      <div class="chart-container">
        <canvas class="sparkline" data-role="sparkline" data-strike="${strike}" width="640" height="150"></canvas>
        <div class="chart-hud">
          <span class="chart-tag" data-role="chart-low">LOW: --</span>
          <span class="chart-tag chart-tag--center">10-MIN TRAILING TIMEFRAME</span>
          <span class="chart-tag" data-role="chart-high">HIGH: --</span>
        </div>
      </div>

      <div class="waiting-box"><span class="spinner"></span> Awaiting initial model features and order book depth...</div>
    `;
  }

  const modelP = Number(r.model_p_yes);
  const marketP = Number(r.market_p_yes);
  const predictedAbove = modelP >= 0.5;
  const winProb = (predictedAbove ? modelP : 1 - modelP) * 100;
  const rec = (r.recommendation || "NO_EDGE").toLowerCase();
  const recLabel =
    { buy_yes: "YES Underpriced", buy_no: "NO Underpriced", no_edge: "Priced Fairly" }[rec] ??
    r.recommendation;
  const edgePts = (Number(r.edge) * 100).toFixed(1);
  const edgePositive = Number(r.edge) >= 0;

  return `
    ${tradeBannerHtml(r)}

    ${outcomeHtml(r, predictedAbove)}

    <!-- Price Target Matrix -->
    <div class="price-matrix">
      <div class="price-tile">
        <div class="tile-header">
          <span class="tile-label">LIVE INDEX PRICE</span>
          <span class="live-dot-tag" data-role="live-tag">LIVE</span>
        </div>
        <div class="tile-value tile-value--price" data-role="index-price">${indexPrice !== null ? "$" + formatUsd(indexPrice) : "--"}</div>
      </div>

      <div class="price-tile">
        <div class="tile-header">
          <span class="tile-label">GOAL PRICE</span>
          <span class="tile-tag">TARGET</span>
        </div>
        <div class="tile-value tile-value--strike">$${formatUsd(strike)}</div>
      </div>

      <div class="price-tile">
        <div class="tile-header">
          <span class="tile-label">GAP TO GOAL</span>
          <span class="tile-tag ${priceDiff >= 0 ? "tag--bull" : "tag--bear"}">${priceDiff >= 0 ? "ABOVE" : "BELOW"}</span>
        </div>
        <div class="tile-value ${priceDiff >= 0 ? "val--bull" : "val--bear"}" data-role="price-diff">
          ${diffSign}$${formatUsd(priceDiff)} <span class="pct-sub">(${diffSign}${diffPct}%)</span>
        </div>
      </div>
    </div>

    <!-- Interactive Sparkline Chart -->
    <div class="chart-container">
      <canvas class="sparkline" data-role="sparkline" data-strike="${strike}" width="640" height="150"></canvas>
      <div class="chart-hud">
        <span class="chart-tag" data-role="chart-low">LOW: --</span>
        <span class="chart-tag chart-tag--center">10-MIN TRAILING TIMEFRAME</span>
        <span class="chart-tag" data-role="chart-high">HIGH: --</span>
      </div>
    </div>

    <!-- Probability & Edge Meters -->
    <div class="prob-section">
      <div class="prob-row">
        <div class="prob-info">
          <span class="prob-label">AI Model Win Probability</span>
          <span class="prob-val prob-val--model">${(modelP * 100).toFixed(1)}% ABOVE GOAL</span>
        </div>
        <div class="prob-bar-track">
          <div class="prob-bar-fill prob-bar-fill--model" style="width: ${(modelP * 100).toFixed(1)}%"></div>
          <div class="prob-bar-target" style="left: ${(marketP * 100).toFixed(1)}%" title="Market Odds Reference"></div>
        </div>
      </div>

      <div class="prob-row">
        <div class="prob-info">
          <span class="prob-label">Kalshi Market Implied Odds</span>
          <span class="prob-val prob-val--market">${(marketP * 100).toFixed(1)}% ABOVE GOAL</span>
        </div>
        <div class="prob-bar-track">
          <div class="prob-bar-fill prob-bar-fill--market" style="width: ${(marketP * 100).toFixed(1)}%"></div>
        </div>
      </div>

      <div class="edge-callout-bar">
        <div class="edge-pill ${edgePositive ? "edge-pill--pos" : "edge-pill--neg"}">
          <span class="edge-icon">${edgePositive ? "\u25B2" : "\u25BC"}</span>
          <span>Pricing Advantage: <strong>${edgePositive ? "+" : ""}${edgePts} pts edge</strong></span>
        </div>
        <div class="confidence-pill">
          <span>Directional Probability: <strong>${winProb.toFixed(1)}% ${predictedAbove ? "ABOVE" : "BELOW"}</strong></span>
        </div>
      </div>
    </div>

    <!-- Card Footer -->
    <div class="card-footer">
      <span class="badge badge--${rec}">${recLabel}</span>
      <span class="updated-at" data-role="updated">Updated ${r.ts_ms ? new Date(r.ts_ms).toLocaleTimeString() : "--"}</span>
    </div>
  `;
}

function whaleFaceHtml() {
  return `
    <div class="whale-tracker-header">
      <div class="whale-tracker-title-bar">
        <div class="whale-tracker-heading">
          <span class="whale-icon-badge">\uD83D\uDC0B</span>
          <div>
            <h3 class="whale-title">Whale Tracker &mdash; Institutional Order Flow</h3>
            <p class="whale-caption">Surfacing anonymous high-volume fills and positioning on this 15-minute market.</p>
          </div>
        </div>
      </div>

      <div class="whale-filter-card">
        <div class="whale-filter-top">
          <span class="filter-label">Quick Minimum Filter:</span>
          <div class="whale-presets" data-role="whale-presets">
            <button type="button" class="preset-chip ${whaleMinUsd === 0 ? "active" : ""}" data-val="0">All</button>
            <button type="button" class="preset-chip ${whaleMinUsd === 50 ? "active" : ""}" data-val="50">$50+</button>
            <button type="button" class="preset-chip ${whaleMinUsd === 100 ? "active" : ""}" data-val="100">$100+</button>
            <button type="button" class="preset-chip ${whaleMinUsd === 250 ? "active" : ""}" data-val="250">$250+</button>
            <button type="button" class="preset-chip ${whaleMinUsd === 500 ? "active" : ""}" data-val="500">$500+</button>
            <button type="button" class="preset-chip ${whaleMinUsd === 1000 ? "active" : ""}" data-val="1000">$1,000+</button>
          </div>
        </div>

        <form class="whale-filter-form" data-role="whale-filter">
          <label class="custom-label">Custom Min USD:</label>
          <div class="whale-input-group">
            <span class="input-prefix">$</span>
            <input type="number" class="whale-input" data-role="whale-min" min="0" step="10"
                   value="${whaleMinUsd}" aria-label="Minimum trade amount in dollars" />
            <button type="submit" class="whale-btn-apply">Apply</button>
          </div>
        </form>
      </div>
    </div>

    <div class="whale-stream-container">
      <div class="whale-stream-header">
        <span>SIDE</span>
        <span>TRADE NOTIONAL</span>
        <span>FILL PRICE</span>
        <span>TIMESTAMP</span>
      </div>
      <div class="whale-stream-list" data-role="whale-list">
        <div class="whale-loading">
          <span class="spinner"></span> Scanning order book fills&hellip;
        </div>
      </div>
    </div>
  `;
}

function cardHtml(r) {
  const isClosed = r.status === "closed";
  const meta = getCoinMeta(r.index_id);
  return `
    <article class="card ${isClosed ? "card--closed" : ""} card--${meta.symbol.toLowerCase()}"
             data-ticker="${r.ticker}"
             data-status="${r.status}"
             data-close-ts="${r.close_ts_ms}"
             data-closed-at="${r.closed_at_ms ?? ""}"
             data-index-id="${r.index_id ?? ""}"
             data-view="prediction">
      <div class="card-glow" style="background: radial-gradient(circle at 80% 0%, ${meta.accent}15, transparent 60%);"></div>
      
      <!-- Card Header -->
      <div class="card-header">
        <div class="card-header__left">
          <div class="coin-badge" style="border-color: ${meta.accent}40;">
            <span class="coin-badge__icon" style="color: ${meta.accent};">${meta.icon}</span>
            <div class="coin-badge__text">
              <div class="coin-badge__name">${meta.name} <span class="badge-sub">15m</span></div>
              <div class="coin-badge__ticker-row">
                <span class="ticker-code">${r.ticker}</span>
                <button type="button" class="btn-copy-ticker" data-ticker="${r.ticker}" title="Copy market ticker">\u2398</button>
              </div>
            </div>
          </div>
        </div>

        <div class="card-header__right">
          <button type="button" class="whale-toggle-btn" data-role="whale-toggle" title="Toggle Whale Tracker feed">
            <span class="whale-toggle-icon">\uD83D\uDC0B</span>
            <span class="whale-toggle-label">Whale Tracker</span>
          </button>
          <div class="countdown-badge ${isClosed ? "expired" : ""}" data-role="countdown">
            <span class="clock-icon">\u23F1</span>
            <span class="countdown-val">--:--</span>
          </div>
        </div>
      </div>

      <!-- Card Faces -->
      <div class="card-face card-face--prediction" data-role="face-prediction">
        ${predictionHtml(r)}
      </div>
      <div class="card-face card-face--whales" data-role="face-whales" hidden>
        ${whaleFaceHtml()}
      </div>
    </article>
  `;
}

function renderCards(rows) {
  rawMarketRows = rows || [];
  const container = document.getElementById("cards");
  if (!container) return;

  // Update tab badge counts
  const activeCountEl = document.getElementById("active-count");
  const closedCountEl = document.getElementById("closed-count");
  if (currentTab === "active" && activeCountEl) {
    activeCountEl.textContent = rows.length;
  } else if (closedCountEl) {
    closedCountEl.textContent = rows.length;
  }

  // Filter rows by Coin and Search Query
  let filtered = rawMarketRows.filter((r) => {
    if (currentCoinFilter !== "all" && r.index_id !== currentCoinFilter) {
      return false;
    }
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      const tickerMatch = r.ticker && r.ticker.toLowerCase().includes(q);
      const strikeMatch = r.strike && String(r.strike).includes(q);
      const coinMatch = r.index_id && r.index_id.toLowerCase().includes(q);
      if (!tickerMatch && !strikeMatch && !coinMatch) return false;
    }
    return true;
  });

  if (filtered.length === 0) {
    container.innerHTML = `
      <div class="empty-state-card">
        <div class="empty-icon">\uD83D\uDCC2</div>
        <p class="empty-title">${EMPTY_MESSAGES[currentTab]}</p>
        <p class="empty-desc">${searchQuery ? "Try clearing your search query or coin filter." : "The engine is standing by for upcoming settlement windows."}</p>
      </div>
    `;
    return;
  }

  // Sort by Coin Priority (BTC first, then SOL) then Expiration timestamp
  const COIN_ORDER = { BRTI: 0, SOLUSD_RTI: 1 };
  const sorted = [...filtered].sort((a, b) => {
    const order = (COIN_ORDER[a.index_id] ?? 99) - (COIN_ORDER[b.index_id] ?? 99);
    return order !== 0 ? order : a.close_ts_ms - b.close_ts_ms;
  });

  // Preserve flipped cards state across re-renders
  const flippedTickers = new Set(
    [...container.querySelectorAll('.card[data-view="whales"]')].map((el) => el.dataset.ticker)
  );

  let html = "";
  sorted.forEach((r) => {
    html += cardHtml(r);
  });
  container.innerHTML = html;
  tickCountdowns();

  if (flippedTickers.size) {
    container.querySelectorAll(".card").forEach((card) => {
      if (flippedTickers.has(card.dataset.ticker)) {
        setCardView(card, "whales");
      }
    });
  }

  const indexIds = new Set(sorted.map((r) => r.index_id).filter(Boolean));
  indexIds.forEach((indexId) => {
    patchLiveCards(indexId);
    ensureSparklineSeed(indexId).then(() => patchLiveCards(indexId));
  });
}

function setCardView(card, view) {
  card.dataset.view = view;
  const predictionFace = card.querySelector('[data-role="face-prediction"]');
  const whaleFace = card.querySelector('[data-role="face-whales"]');
  const toggleBtn = card.querySelector('[data-role="whale-toggle"]');

  if (predictionFace) predictionFace.hidden = view !== "prediction";
  if (whaleFace) whaleFace.hidden = view !== "whales";
  if (toggleBtn) {
    toggleBtn.classList.toggle("active", view === "whales");
    const label = toggleBtn.querySelector(".whale-toggle-label");
    const icon = toggleBtn.querySelector(".whale-toggle-icon");
    if (label && icon) {
      if (view === "whales") {
        label.textContent = "Prediction Signal";
        icon.textContent = "\u26A1";
      } else {
        label.textContent = "Whale Tracker";
        icon.textContent = "\uD83D\uDC0B";
      }
    }
  }

  if (view === "whales") {
    loadWhaleTrades(card);
  }
}

async function loadWhaleTrades(card) {
  const ticker = card.dataset.ticker;
  const listEl = card.querySelector('[data-role="whale-list"]');
  if (!listEl) return;

  listEl.innerHTML = '<div class="whale-loading"><span class="spinner"></span> Scanning order book fills&hellip;</div>';
  try {
    const query = new URLSearchParams({ ticker, limit: "30", min_usd: String(whaleMinUsd) });
    const res = await fetch(`/api/whale-trades?${query}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const trades = await res.json();

    if (!trades || trades.length === 0) {
      listEl.innerHTML = `
        <div class="whale-empty-state">
          <div class="whale-empty-icon">\uD83D\uDD0D</div>
          <div class="whale-empty-text">No whale fills at or above $${whaleMinUsd.toLocaleString()} found yet.</div>
          <div class="whale-empty-sub">Lower the minimum threshold or wait for incoming market volume.</div>
        </div>
      `;
      return;
    }

    const maxUsd = Math.max(...trades.map((t) => t.notional_usd || 100), 500);
    listEl.innerHTML = trades.map((t) => whaleTradeRowHtml(t, maxUsd)).join("");
  } catch {
    listEl.innerHTML = '<div class="whale-error">Unable to fetch whale trades right now.</div>';
  }
}

function whaleTradeRowHtml(t, maxUsd) {
  const isYes = t.side === "yes";
  const sideLabel = isYes ? "YES / UP" : "NO / DOWN";
  const sideClass = isYes ? "side--yes" : "side--no";
  const notional = Math.round(t.notional_usd);
  const priceCents = Number(t.price_cents).toFixed(1);
  const count = t.count ? Number(t.count).toLocaleString() : Math.round(notional / (Number(t.price_cents) / 100 || 1));

  const secondsAgo = Math.max(0, Math.floor((Date.now() - t.ts_ms) / 1000));
  const ago = secondsAgo < 60 ? `${secondsAgo}s ago` : `${Math.floor(secondsAgo / 60)}m ago`;

  const barPct = Math.min(Math.max((notional / maxUsd) * 100, 10), 100);

  return `
    <div class="whale-row">
      <div class="whale-cell whale-cell--side">
        <span class="whale-side-badge ${sideClass}">${sideLabel}</span>
      </div>
      <div class="whale-cell whale-cell--amount">
        <div class="amount-val">$${notional.toLocaleString()}</div>
        <div class="amount-sub">${count} contracts</div>
        <div class="amount-bar" style="width: ${barPct}%;"></div>
      </div>
      <div class="whale-cell whale-cell--price">
        <div class="price-val">@ ${priceCents}&cent;</div>
      </div>
      <div class="whale-cell whale-cell--time">
        <span class="time-ago">${ago}</span>
      </div>
    </div>
  `;
}

function drawSparkline(canvas, points, strike) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const cssWidth = rect.width || canvas.clientWidth || 640;
  const cssHeight = rect.height || canvas.clientHeight || 150;

  canvas.width = cssWidth * dpr;
  canvas.height = cssHeight * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssWidth, cssHeight);

  if (!points || points.length < 2) {
    ctx.fillStyle = "rgba(255,255,255,0.3)";
    ctx.font = "12px 'JetBrains Mono', monospace";
    ctx.textAlign = "center";
    ctx.fillText("Gathering live 10-minute tick history\u2026", cssWidth / 2, cssHeight / 2);
    return;
  }

  const values = points.map((p) => p.value);
  const strikeNum = Number(strike);
  const min = Math.min(...values, strikeNum);
  const max = Math.max(...values, strikeNum);
  const range = max - min || 1;

  const padLeft = 68;
  const padRight = 16;
  const padTop = 14;
  const padBottom = 22;
  const plotW = cssWidth - padLeft - padRight;
  const plotH = cssHeight - padTop - padBottom;

  const xAt = (i) => padLeft + (i / (points.length - 1)) * plotW;
  const yAt = (v) => padTop + plotH - ((v - min) / range) * plotH;

  const lastAbove = values[values.length - 1] >= strikeNum;
  const strokeColor = lastAbove ? "#10b981" : "#f43f5e";

  // Grid Lines & Price Axis Labels
  ctx.font = "10px 'JetBrains Mono', monospace";
  ctx.fillStyle = "rgba(255, 255, 255, 0.4)";
  ctx.strokeStyle = "rgba(255, 255, 255, 0.05)";
  ctx.lineWidth = 1;

  [max, (max + min) / 2, min].forEach((v) => {
    const y = yAt(v);
    ctx.beginPath();
    ctx.moveTo(padLeft, y);
    ctx.lineTo(cssWidth - padRight, y);
    ctx.stroke();
    ctx.textAlign = "left";
    ctx.fillText(`$${formatUsd(v)}`, 6, y + 3);
  });

  // Goal Price Dotted Line
  const strikeY = yAt(strikeNum);
  ctx.save();
  ctx.strokeStyle = "rgba(168, 85, 247, 0.65)";
  ctx.setLineDash([4, 4]);
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(padLeft, strikeY);
  ctx.lineTo(cssWidth - padRight, strikeY);
  ctx.stroke();
  ctx.restore();

  // Goal Price Tag Box
  ctx.fillStyle = "rgba(168, 85, 247, 0.2)";
  ctx.fillRect(cssWidth - padRight - 110, strikeY - 14, 110, 16);
  ctx.fillStyle = "#c084fc";
  ctx.font = "bold 9px 'JetBrains Mono', monospace";
  ctx.textAlign = "right";
  ctx.fillText(`GOAL $${formatUsd(strikeNum)}`, cssWidth - padRight - 6, strikeY - 3);

  // Gradient Area Fill
  const gradient = ctx.createLinearGradient(0, padTop, 0, padTop + plotH);
  if (lastAbove) {
    gradient.addColorStop(0, "rgba(16, 185, 129, 0.28)");
    gradient.addColorStop(1, "rgba(16, 185, 129, 0.0)");
  } else {
    gradient.addColorStop(0, "rgba(244, 63, 94, 0.28)");
    gradient.addColorStop(1, "rgba(244, 63, 94, 0.0)");
  }

  ctx.beginPath();
  ctx.moveTo(xAt(0), yAt(values[0]));
  values.forEach((v, i) => ctx.lineTo(xAt(i), yAt(v)));
  ctx.lineTo(xAt(values.length - 1), padTop + plotH);
  ctx.lineTo(xAt(0), padTop + plotH);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();

  // Price Path
  ctx.beginPath();
  ctx.moveTo(xAt(0), yAt(values[0]));
  values.forEach((v, i) => ctx.lineTo(xAt(i), yAt(v)));
  ctx.strokeStyle = strokeColor;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.stroke();

  // Pulse Glow at Current Price
  const lastX = xAt(values.length - 1);
  const lastY = yAt(values[values.length - 1]);

  ctx.beginPath();
  ctx.arc(lastX, lastY, 6, 0, Math.PI * 2);
  ctx.fillStyle = lastAbove ? "rgba(16, 185, 129, 0.3)" : "rgba(244, 63, 94, 0.3)";
  ctx.fill();

  ctx.beginPath();
  ctx.arc(lastX, lastY, 3.5, 0, Math.PI * 2);
  ctx.fillStyle = strokeColor;
  ctx.fill();

  // Time Axis Labels
  ctx.fillStyle = "rgba(255, 255, 255, 0.35)";
  ctx.font = "9px 'JetBrains Mono', monospace";
  ctx.textAlign = "left";
  ctx.fillText(
    new Date(points[0].ts_ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
    padLeft,
    cssHeight - 6
  );
  ctx.textAlign = "right";
  ctx.fillText(
    new Date(points[points.length - 1].ts_ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
    cssWidth - padRight,
    cssHeight - 6
  );
}

function updateSparklineMeta(card, points) {
  const lowEl = card.querySelector('[data-role="chart-low"]');
  const highEl = card.querySelector('[data-role="chart-high"]');
  if (!points || points.length < 2) {
    if (lowEl) lowEl.textContent = "LOW: --";
    if (highEl) highEl.textContent = "HIGH: --";
    return;
  }
  const values = points.map((p) => p.value);
  if (lowEl) lowEl.textContent = `LOW: $${formatUsd(Math.min(...values))}`;
  if (highEl) highEl.textContent = `HIGH: $${formatUsd(Math.max(...values))}`;
}

async function ensureSparklineSeed(indexId) {
  if (seededIndexIds.has(indexId)) return;
  seededIndexIds.add(indexId);
  try {
    const res = await fetch(`/api/price-history?index_id=${encodeURIComponent(indexId)}&minutes=10`);
    if (res.ok) {
      sparklineHistories[indexId] = await res.json();
    }
  } catch {
    seededIndexIds.delete(indexId);
  }
}

function appendLiveTick(indexId, tsMs, value) {
  latestLiveByIndex[indexId] = { ts_ms: tsMs, value };
  const hist = sparklineHistories[indexId] || (sparklineHistories[indexId] = []);
  hist.push({ ts_ms: tsMs, value });
  const cutoff = Date.now() - 10 * 60 * 1000;
  while (hist.length && hist[0].ts_ms < cutoff) hist.shift();
  patchLiveCards(indexId);
}

function patchLiveCards(indexId) {
  const live = latestLiveByIndex[indexId];
  const selector = CSS && CSS.escape ? `.card[data-index-id="${CSS.escape(indexId)}"]` : `.card[data-index-id="${indexId}"]`;
  document.querySelectorAll(selector).forEach((card) => {
    const canvas = card.querySelector('[data-role="sparkline"]');
    if (canvas) {
      drawSparkline(canvas, sparklineHistories[indexId] || [], canvas.dataset.strike);
      updateSparklineMeta(card, sparklineHistories[indexId] || []);
    }
    if (!live) return;
    const priceEl = card.querySelector('[data-role="index-price"]');
    const diffEl = card.querySelector('[data-role="price-diff"]');

    if (priceEl) {
      priceEl.textContent = `$${formatUsd(live.value)}`;
      priceEl.classList.remove("flash-update");
      void priceEl.offsetWidth; // trigger reflow
      priceEl.classList.add("flash-update");
    }

    if (diffEl && canvas) {
      const strike = Number(canvas.dataset.strike);
      const diff = live.value - strike;
      const diffPct = strike > 0 ? ((diff / strike) * 100).toFixed(2) : "0.00";
      const sign = diff >= 0 ? "+" : "";
      diffEl.innerHTML = `${sign}$${formatUsd(diff)} <span class="pct-sub">(${sign}${diffPct}%)</span>`;
      diffEl.className = `tile-value ${diff >= 0 ? "val--bull" : "val--bear"}`;
    }
  });
}

function tickCountdowns() {
  document.querySelectorAll(".card").forEach((card) => {
    const countdownEl = card.querySelector('[data-role="countdown"]');
    const valEl = countdownEl?.querySelector(".countdown-val");

    if (countdownEl && valEl) {
      if (card.dataset.status === "closed") {
        const closedAt = Number(card.dataset.closedAt);
        valEl.textContent = Number.isFinite(closedAt) && closedAt > 0 ? formatElapsed(Date.now() - closedAt) : "Settled";
        countdownEl.className = "countdown-badge expired";
      } else {
        const closeTsMs = Number(card.dataset.closeTs);
        const { text, cls } = formatCountdown(closeTsMs - Date.now());
        valEl.textContent = text;
        countdownEl.className = `countdown-badge ${cls}`;
      }
    }

    const decisionEl = card.querySelector('[data-role="decision-countdown"]');
    if (decisionEl) {
      const closeTsMs = Number(card.dataset.closeTs);
      const decisionAtMs = closeTsMs - decisionLeadSec * 1000;
      const msRemaining = decisionAtMs - Date.now();
      if (msRemaining <= 0) {
        decisionEl.textContent = "LOCKING IN NOW";
        decisionEl.classList.add("urgent");
      } else {
        const { text } = formatCountdown(msRemaining);
        decisionEl.textContent = text;
      }
    }
  });
}

function updateClock() {
  const clockEl = document.getElementById("system-clock");
  if (!clockEl) return;
  const now = new Date();
  const utcStr = now.toISOString().substring(11, 19) + " UTC";
  clockEl.textContent = utcStr;
}

async function refreshExternalStatus() {
  const element = document.getElementById("external-status");
  if (!element) return;
  try {
    const response = await fetch("/api/external-status");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const status = await response.json();
    const healthy = status.healthy;
    const count = status.sources?.length ?? 0;
    const label = healthy ? `EXT FEED: LIVE (${count})` : count ? "EXT FEED: STALE" : "EXT FEED: WAITING";
    element.textContent = label;
    element.className = `external-status external-status--${healthy ? "live" : count ? "stale" : "waiting"}`;
    const sourceDetail = (status.sources || [])
      .map((source) => `${source.symbol}: ${Math.round(source.age_ms)}ms`)
      .join(" | ");
    element.title = sourceDetail || "No external price ticks received yet";
  } catch {
    element.textContent = "EXT FEED: UNAVAILABLE";
    element.className = "external-status external-status--stale";
  }
}

function setStatus(state, label) {
  const el = document.getElementById("conn-status");
  if (!el) return;
  el.className = `status status--${state}`;
  el.textContent = label.toUpperCase();
}

let liveSocket = null;
let liveSocketBackoffMs = 1000;

function connectLiveSocket() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  liveSocket = new WebSocket(`${protocol}//${location.host}/ws/live`);

  liveSocket.addEventListener("open", () => {
    liveSocketBackoffMs = 1000;
    setStatus("live", "LIVE FEED");
  });

  liveSocket.addEventListener("message", (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }
    if (msg.type === "index_tick") {
      appendLiveTick(msg.index_id, msg.ts_ms, msg.value);
    }
  });

  liveSocket.addEventListener("close", () => {
    setStatus("pending", "RECONNECTING");
    setTimeout(connectLiveSocket, liveSocketBackoffMs);
    liveSocketBackoffMs = Math.min(liveSocketBackoffMs * 2, 15000);
  });

  liveSocket.addEventListener("error", () => {
    liveSocket.close();
  });
}

async function fetchCalibration(indexId) {
  const qs = indexId ? `?index_id=${encodeURIComponent(indexId)}` : "";
  const res = await fetch(`/api/calibration${qs}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function renderHudCalibrationCard(title, coinSymbol, c) {
  if (!c || !c.n) {
    return `
      <div class="hud-card">
        <div class="hud-card__top">
          <span class="hud-card__symbol">${coinSymbol}</span>
          <span class="hud-card__label">${title}</span>
        </div>
        <div class="hud-card__body">
          <div class="hud-card__empty">Awaiting first settled outcome&hellip;</div>
        </div>
      </div>
    `;
  }

  const isBetter = c.model_brier <= c.market_brier;
  const delta = (c.market_brier - c.model_brier).toFixed(3);
  const deltaSign = Number(delta) >= 0 ? "+" : "";

  return `
    <div class="hud-card ${isBetter ? "hud-card--outperforming" : "hud-card--underperforming"}">
      <div class="hud-card__top">
        <div class="hud-card__badge-wrap">
          <span class="hud-card__symbol">${coinSymbol}</span>
          <span class="hud-card__label">${title}</span>
        </div>
        <span class="hud-card__status-pill ${isBetter ? "pill--success" : "pill--warning"}">
          ${isBetter ? "OUTPERFORMING MARKET" : "TRACKING MARKET"}
        </span>
      </div>

      <div class="hud-card__metrics-grid">
        <div class="hud-metric">
          <span class="hud-metric__label">US (BRIER)</span>
          <span class="hud-metric__val ${isBetter ? "val--better" : "val--worse"}">${c.model_brier.toFixed(3)}</span>
        </div>
        <div class="hud-metric">
          <span class="hud-metric__label">MARKET (BRIER)</span>
          <span class="hud-metric__val">${c.market_brier.toFixed(3)}</span>
        </div>
        <div class="hud-metric">
          <span class="hud-metric__label">COIN FLIP</span>
          <span class="hud-metric__val val--muted">${c.baseline_brier.toFixed(3)}</span>
        </div>
      </div>

      <div class="hud-card__footer">
        <span class="sample-badge">${c.n} settled markets</span>
        <span class="edge-delta">${deltaSign}${delta} Brier delta</span>
      </div>
    </div>
  `;
}

async function refreshCalibration() {
  const el = document.getElementById("calibration");
  if (!el) return;

  try {
    const [overall, btc, sol] = await Promise.all([
      fetchCalibration(),
      fetchCalibration("BRTI"),
      fetchCalibration("SOLUSD_RTI"),
    ]);

    calibrationStore.overall = overall;
    calibrationStore.BRTI = btc;
    calibrationStore.SOLUSD_RTI = sol;

    el.innerHTML = `
      ${renderHudCalibrationCard("Overall Engine", "\u26A1", overall)}
      ${renderHudCalibrationCard("Bitcoin Model", "\u20BF", btc)}
      ${renderHudCalibrationCard("Solana Model", "\u25CE", sol)}
    `;
  } catch {
    el.innerHTML = '<div class="hud-card"><div class="hud-card__empty">Calibration telemetry currently unavailable.</div></div>';
  }
}

function setTab(tab) {
  currentTab = tab;
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tab);
  });
  refresh();
}

async function refresh() {
  try {
    const res = await fetch(ENDPOINTS[currentTab]);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const rows = await res.json();
    renderCards(rows);
    setStatus("live", "LIVE FEED");
    const updatedEl = document.getElementById("last-updated");
    if (updatedEl) {
      updatedEl.textContent = `Last Refreshed: ${new Date().toLocaleTimeString()}`;
    }
  } catch (err) {
    setStatus("error", "FEED ERROR");
  }
}

function initApp() {
  // Tab Switching
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => setTab(btn.dataset.tab));
  });

  // Coin Filter Chips
  document.querySelectorAll(".filter-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      document.querySelectorAll(".filter-chip").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      currentCoinFilter = chip.dataset.filter;
      renderCards(rawMarketRows);
    });
  });

  // Search Box
  const searchInput = document.getElementById("market-search");
  const clearBtn = document.getElementById("search-clear");
  if (searchInput) {
    searchInput.addEventListener("input", (e) => {
      searchQuery = e.target.value.trim();
      if (clearBtn) clearBtn.hidden = !searchQuery;
      renderCards(rawMarketRows);
    });
  }
  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      if (searchInput) {
        searchInput.value = "";
        searchQuery = "";
        clearBtn.hidden = true;
        renderCards(rawMarketRows);
      }
    });
  }

  // Delegated Card Click Handlers
  const cardsContainer = document.getElementById("cards");
  if (cardsContainer) {
    cardsContainer.addEventListener("click", (event) => {
      // 1. Toggle Whale Tracker View
      const toggle = event.target.closest('[data-role="whale-toggle"]');
      if (toggle) {
        const card = toggle.closest(".card");
        if (card) {
          setCardView(card, card.dataset.view === "whales" ? "prediction" : "whales");
        }
        return;
      }

      // 2. Copy Ticker Button
      const copyBtn = event.target.closest(".btn-copy-ticker");
      if (copyBtn) {
        const ticker = copyBtn.dataset.ticker;
        if (ticker) {
          navigator.clipboard.writeText(ticker).then(() => {
            copyBtn.textContent = "\u2713";
            copyBtn.classList.add("copied");
            setTimeout(() => {
              copyBtn.textContent = "\u2398";
              copyBtn.classList.remove("copied");
            }, 1800);
          });
        }
        return;
      }

      // 3. Whale Tracker Preset Filter Chips
      const presetBtn = event.target.closest(".preset-chip");
      if (presetBtn) {
        const val = Number(presetBtn.dataset.val);
        if (Number.isFinite(val)) {
          whaleMinUsd = val;
          try {
            localStorage.setItem("whaleMinUsd", String(val));
          } catch {}
          const card = presetBtn.closest(".card");
          if (card) {
            card.querySelectorAll(".preset-chip").forEach((c) => c.classList.toggle("active", Number(c.dataset.val) === val));
            const input = card.querySelector('[data-role="whale-min"]');
            if (input) input.value = String(val);
            loadWhaleTrades(card);
          }
        }
        return;
      }
    });

    // Custom Whale Filter Submit
    cardsContainer.addEventListener("submit", (event) => {
      const form = event.target.closest('[data-role="whale-filter"]');
      if (!form) return;
      event.preventDefault();
      const input = form.querySelector('[data-role="whale-min"]');
      const value = Number(input?.value);
      if (!Number.isFinite(value) || value < 0) {
        input?.setCustomValidity("Enter zero or a positive dollar amount.");
        input?.reportValidity();
        return;
      }
      input.setCustomValidity("");
      whaleMinUsd = value;
      try {
        localStorage.setItem("whaleMinUsd", String(value));
      } catch {}
      const card = form.closest(".card");
      if (card) {
        card.querySelectorAll(".preset-chip").forEach((c) => c.classList.toggle("active", Number(c.dataset.val) === value));
        loadWhaleTrades(card);
      }
    });
  }

  // Fetch Config for decision lead time
  fetch("/api/config")
    .then((res) => res.json())
    .then((cfg) => {
      if (cfg.decision_lead_sec) decisionLeadSec = cfg.decision_lead_sec;
    })
    .catch(() => {});

  // Startup Loops
  connectLiveSocket();
  refresh();
  refreshCalibration();
  updateClock();
  refreshExternalStatus();

  setInterval(refresh, 5000);
  setInterval(tickCountdowns, 1000);
  setInterval(refreshCalibration, 15000);
  setInterval(updateClock, 1000);
  setInterval(refreshExternalStatus, 5000);
}

// Ensure execution happens even if DOM is already parsed
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initApp);
} else {
  initApp();
}
