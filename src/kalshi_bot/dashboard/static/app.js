const COIN_NAMES = {
  BRTI: "Bitcoin (BTC)",
  SOLUSD_RTI: "Solana (SOL)",
};

const COIN_ICONS = {
  BRTI: "\u20BF",
  SOLUSD_RTI: "\u25CE",
};

const ENDPOINTS = {
  active: "/api/active",
  closed: "/api/closed",
};

const EMPTY_MESSAGES = {
  active: "No open 15-minute BTC/SOL markets right now.",
  closed: "No closed markets yet.",
};

let currentTab = "active";
let decisionLeadSec = 390; // overwritten by /api/config on load

// Populated by refreshCalibration(); used to render the per-coin section headers.
const calibrationByCoin = {}; // index_id -> stats from /api/calibration

// Live price state, updated by WebSocket pushes rather than polling.
const sparklineHistories = {}; // index_id -> [{ts_ms, value}, ...] ascending
const latestLiveByIndex = {}; // index_id -> {ts_ms, value}
const seededIndexIds = new Set();

function coinName(indexId) {
  return COIN_NAMES[indexId] ?? indexId ?? "Unknown";
}

function coinIcon(indexId) {
  return COIN_ICONS[indexId] ?? "\u25CF";
}

function formatUsd(value) {
  return Number(value).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatCountdown(msRemaining) {
  if (msRemaining <= 0) return { text: "closed", cls: "expired" };
  const totalSec = Math.floor(msRemaining / 1000);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  const text = `${m}:${String(s).padStart(2, "0")}`;
  return { text, cls: totalSec <= 60 ? "closing-soon" : "" };
}

function formatElapsed(msElapsed) {
  const totalSec = Math.max(Math.floor(msElapsed / 1000), 0);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  if (m === 0) return `closed ${s}s ago`;
  return `closed ${m}m ${s}s ago`;
}

function outcomeHtml(r, predictedAbove) {
  if (r.result !== "yes" && r.result !== "no") return "";
  const settledAbove = r.result === "yes";
  const correct = settledAbove === predictedAbove;
  return `<p class="outcome ${correct ? "correct" : "incorrect"}">Result: ${settledAbove ? "ABOVE" : "BELOW"} — we were ${correct ? "right" : "wrong"}</p>`;
}

const TRADE_LABELS = {
  BUY_YES: ["BET UP", "up"],
  BUY_NO: ["BET DOWN", "down"],
  NO_EDGE: ["NO TRADE", "none"],
};

function tradeBannerHtml(r) {
  const rec = r.decision_recommendation;
  if (rec === undefined || rec === null) {
    return (
      '<div class="trade-banner trade-banner--pending">' +
      '<div class="trade-banner__header">' +
      "<span>Final call locks in</span>" +
      '<span class="trade-banner__countdown" data-role="decision-countdown">--:--</span>' +
      "</div>" +
      "</div>"
    );
  }
  const pair = TRADE_LABELS[rec] || ["NO TRADE", "none"];
  const label = pair[0];
  const cls = pair[1];
  const confidence = (Number(r.decision_confidence) * 100).toFixed(1);
  const minutesLeft = (Number(r.decision_seconds_to_expiry) / 60).toFixed(1);
  const agree = r.decision_confirmation_agree;
  const total = r.decision_confirmation_total;
  const confirmationNote =
    agree !== undefined && agree !== null && total
      ? ` &middot; ${agree}/${total} checks agreed`
      : "";
  let checks = [];
  try {
    checks = r.decision_confirmation_detail ? JSON.parse(r.decision_confirmation_detail) : [];
  } catch {
    checks = [];
  }
  const checksHtml = checks.length
    ? '<ul class="trade-banner__checks">' +
      checks
        .map(
          (c) =>
            `<li class="check ${c.agree ? "check--yes" : "check--no"}">${c.agree ? "✓" : "✗"} ${c.name}</li>`
        )
        .join("") +
      "</ul>"
    : "";
  return (
    `<div class="trade-banner trade-banner--${cls}">` +
    '<div class="trade-banner__header">' +
    `<span class="trade-banner__label">${label}</span>` +
    `<span class="trade-banner__meta">locked in with ${minutesLeft} min left &middot; ${confidence}% confidence${confirmationNote}</span>` +
    "</div>" +
    checksHtml +
    "</div>"
  );
}

function predictionHtml(r) {
  const hasSignal = r.model_p_yes !== null && r.model_p_yes !== undefined;
  if (!hasSignal) {
    return '<p class="waiting">Waiting for market data…</p>';
  }

  const modelP = Number(r.model_p_yes);
  const marketP = Number(r.market_p_yes);
  const predictedAbove = modelP >= 0.5;
  const confidence = (predictedAbove ? modelP : 1 - modelP) * 100;
  const rec = (r.recommendation || "NO_EDGE").toLowerCase();
  const recLabel =
    { buy_yes: "Yes looks cheap", buy_no: "No looks cheap", no_edge: "Priced fairly" }[rec] ??
    r.recommendation;
  const priceDiff = Number(r.index_price) - Number(r.strike);
  const diffSign = priceDiff >= 0 ? "+" : "";

  return `
    ${tradeBannerHtml(r)}

    <div class="prediction">
      <span class="call ${predictedAbove ? "above" : "below"}">${predictedAbove ? "ABOVE" : "BELOW"}</span>
      <span class="confidence">${confidence.toFixed(1)}% confident right now</span>
    </div>
    ${outcomeHtml(r, predictedAbove)}

    <div class="price-row">
      <span>Price: <strong data-role="index-price">$${formatUsd(r.index_price)}</strong>
        <span class="live-tag" data-role="live-tag">live</span></span>
      <span>Target: <strong>$${formatUsd(r.strike)}</strong></span>
      <span data-role="price-diff">${diffSign}${formatUsd(priceDiff)}</span>
    </div>

    <canvas class="sparkline" data-role="sparkline" data-strike="${r.strike}" width="640" height="160"></canvas>
    <div class="sparkline-meta">
      <span data-role="chart-low">low --</span>
      <span data-role="chart-range">last 10 min</span>
      <span data-role="chart-high">high --</span>
    </div>

    <div class="bar-label"><span>Our prediction</span><span>${(modelP * 100).toFixed(1)}%</span></div>
    <div class="bar-track">
      <div class="bar-fill model" style="width:${(modelP * 100).toFixed(1)}%"></div>
      <div class="bar-marker" style="left:${(marketP * 100).toFixed(1)}%" title="Market odds"></div>
    </div>

    <div class="bar-label"><span>Market odds</span><span>${(marketP * 100).toFixed(1)}%</span></div>
    <div class="bar-track">
      <div class="bar-fill market" style="width:${(marketP * 100).toFixed(1)}%"></div>
    </div>
    <p class="gap-note">Difference from market: ${r.edge >= 0 ? "+" : ""}${(Number(r.edge) * 100).toFixed(1)} pts</p>

    <div class="footer-row">
      <span class="badge ${rec}">${recLabel}</span>
      <span class="updated-at" data-role="updated">updated ${new Date(r.ts_ms).toLocaleTimeString()}</span>
    </div>
  `;
}

function cardHtml(r) {
  const isClosed = r.status === "closed";
  return `
    <article class="card ${isClosed ? "card--closed" : ""}" data-ticker="${r.ticker}"
              data-status="${r.status}" data-close-ts="${r.close_ts_ms}" data-closed-at="${r.closed_at_ms ?? ""}"
              data-index-id="${r.index_id ?? ""}">
      <div class="card-accent"></div>
      <div class="card-header">
        <div>
          <span class="coin-icon">${coinIcon(r.index_id)}</span>
          <span class="coin">${coinName(r.index_id)}</span>
          <span class="ticker">${r.ticker}</span>
        </div>
        <span class="countdown ${isClosed ? "expired" : ""}" data-role="countdown">--:--</span>
      </div>
      ${predictionHtml(r)}
    </article>
  `;
}

const COIN_ORDER = { BRTI: 0, SOLUSD_RTI: 1 };

function coinSectionHeaderInnerHtml(indexId) {
  const label = `${coinIcon(indexId)} ${coinName(indexId).split(" (")[0]} track record`;
  const stats = calibrationByCoin[indexId];
  return calibrationRowHtml(label, stats || { n: 0 });
}

function coinSectionHeaderHtml(indexId) {
  return `<div class="coin-section-header" data-index-id="${indexId}">${coinSectionHeaderInnerHtml(indexId)}</div>`;
}

function renderCards(rows) {
  const container = document.getElementById("cards");
  if (rows.length === 0) {
    container.innerHTML = `<p class="empty-state">${EMPTY_MESSAGES[currentTab]}</p>`;
    return;
  }
  const sorted = [...rows].sort((a, b) => {
    const order = (COIN_ORDER[a.index_id] ?? 99) - (COIN_ORDER[b.index_id] ?? 99);
    return order !== 0 ? order : a.close_ts_ms - b.close_ts_ms;
  });

  let html = "";
  let lastIndexId;
  sorted.forEach((r) => {
    if (r.index_id && r.index_id !== lastIndexId) {
      html += coinSectionHeaderHtml(r.index_id);
      lastIndexId = r.index_id;
    }
    html += cardHtml(r);
  });
  container.innerHTML = html;
  tickCountdowns();

  const indexIds = new Set(sorted.map((r) => r.index_id).filter(Boolean));
  indexIds.forEach((indexId) => {
    patchLiveCards(indexId); // apply whatever we already know immediately (no flash of stale data)
    ensureSparklineSeed(indexId).then(() => patchLiveCards(indexId));
  });
}

function drawSparkline(canvas, points, strike) {
  const dpr = window.devicePixelRatio || 1;
  const cssWidth = canvas.clientWidth || 640;
  const cssHeight = canvas.clientHeight || 160;
  canvas.width = cssWidth * dpr;
  canvas.height = cssHeight * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssWidth, cssHeight);

  if (!points || points.length < 2) {
    ctx.fillStyle = "rgba(255,255,255,0.25)";
    ctx.font = "11px system-ui";
    ctx.fillText("collecting price history…", 4, cssHeight / 2);
    return;
  }

  const values = points.map((p) => p.value);
  const strikeNum = Number(strike);
  const min = Math.min(...values, strikeNum);
  const max = Math.max(...values, strikeNum);
  const range = max - min || 1;

  // Reserve margin on the left for price-axis labels and at the bottom for time labels
  const padLeft = 56;
  const padRight = 6;
  const padTop = 10;
  const padBottom = 16;
  const plotW = cssWidth - padLeft - padRight;
  const plotH = cssHeight - padTop - padBottom;

  const xAt = (i) => padLeft + (i / (points.length - 1)) * plotW;
  const yAt = (v) => padTop + plotH - ((v - min) / range) * plotH;

  const lastAbove = values[values.length - 1] >= strikeNum;
  const lineColor = lastAbove ? "#33d17a" : "#ff5c5c";

  // Horizontal grid lines + price-axis labels (max / mid / min)
  ctx.font = "10px system-ui";
  ctx.fillStyle = "rgba(255,255,255,0.35)";
  ctx.strokeStyle = "rgba(255,255,255,0.08)";
  ctx.lineWidth = 1;
  [max, (max + min) / 2, min].forEach((v) => {
    const y = yAt(v);
    ctx.beginPath();
    ctx.moveTo(padLeft, y);
    ctx.lineTo(cssWidth - padRight, y);
    ctx.stroke();
    ctx.fillText(`$${formatUsd(v)}`, 2, y + 3);
  });

  // Strike reference line + label
  ctx.strokeStyle = "rgba(255,255,255,0.35)";
  ctx.setLineDash([3, 3]);
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(padLeft, yAt(strikeNum));
  ctx.lineTo(cssWidth - padRight, yAt(strikeNum));
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "rgba(255,255,255,0.55)";
  ctx.fillText(`strike $${formatUsd(strikeNum)}`, cssWidth - padRight - 90, yAt(strikeNum) - 4);

  // Gradient fill under the price line
  const gradient = ctx.createLinearGradient(0, 0, 0, cssHeight);
  gradient.addColorStop(0, lastAbove ? "rgba(51,209,122,0.25)" : "rgba(255,92,92,0.25)");
  gradient.addColorStop(1, "rgba(255,255,255,0)");
  ctx.beginPath();
  ctx.moveTo(xAt(0), yAt(values[0]));
  values.forEach((v, i) => ctx.lineTo(xAt(i), yAt(v)));
  ctx.lineTo(cssWidth - padRight, cssHeight - padBottom);
  ctx.lineTo(padLeft, cssHeight - padBottom);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();

  // Price line
  ctx.beginPath();
  ctx.moveTo(xAt(0), yAt(values[0]));
  values.forEach((v, i) => ctx.lineTo(xAt(i), yAt(v)));
  ctx.strokeStyle = lineColor;
  ctx.lineWidth = 1.75;
  ctx.lineJoin = "round";
  ctx.stroke();

  // Dot at the latest price
  const lastX = xAt(values.length - 1);
  const lastY = yAt(values[values.length - 1]);
  ctx.beginPath();
  ctx.arc(lastX, lastY, 3, 0, Math.PI * 2);
  ctx.fillStyle = lineColor;
  ctx.fill();

  // Time-axis labels (start / end of the visible window)
  ctx.fillStyle = "rgba(255,255,255,0.35)";
  ctx.font = "10px system-ui";
  ctx.textAlign = "left";
  ctx.fillText(new Date(points[0].ts_ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), padLeft, cssHeight - 3);
  ctx.textAlign = "right";
  ctx.fillText(new Date(points[points.length - 1].ts_ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), cssWidth - padRight, cssHeight - 3);
  ctx.textAlign = "left";
}

function updateSparklineMeta(card, points) {
  const lowEl = card.querySelector('[data-role="chart-low"]');
  const highEl = card.querySelector('[data-role="chart-high"]');
  if (!points || points.length < 2) {
    if (lowEl) lowEl.textContent = "low --";
    if (highEl) highEl.textContent = "high --";
    return;
  }
  const values = points.map((p) => p.value);
  if (lowEl) lowEl.textContent = `low $${formatUsd(Math.min(...values))}`;
  if (highEl) highEl.textContent = `high $${formatUsd(Math.max(...values))}`;
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
    seededIndexIds.delete(indexId); // allow retrying on the next render
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
  document.querySelectorAll(`.card[data-index-id="${CSS.escape(indexId)}"]`).forEach((card) => {
    const canvas = card.querySelector('[data-role="sparkline"]');
    if (canvas) {
      drawSparkline(canvas, sparklineHistories[indexId] || [], canvas.dataset.strike);
      updateSparklineMeta(card, sparklineHistories[indexId] || []);
    }
    if (!live) return;
    const priceEl = card.querySelector('[data-role="index-price"]');
    const diffEl = card.querySelector('[data-role="price-diff"]');
    if (priceEl) priceEl.textContent = `$${formatUsd(live.value)}`;
    if (diffEl && canvas) {
      const diff = live.value - Number(canvas.dataset.strike);
      diffEl.textContent = `${diff >= 0 ? "+" : ""}${formatUsd(diff)}`;
    }
  });
}

function tickCountdowns() {
  document.querySelectorAll(".card").forEach((card) => {
    const el = card.querySelector('[data-role="countdown"]');
    if (el) {
      if (card.dataset.status === "closed") {
        const closedAt = Number(card.dataset.closedAt);
        el.textContent = Number.isFinite(closedAt) && closedAt > 0 ? formatElapsed(Date.now() - closedAt) : "closed";
        el.className = "countdown expired";
      } else {
        const closeTsMs = Number(card.dataset.closeTs);
        const { text, cls } = formatCountdown(closeTsMs - Date.now());
        el.textContent = text;
        el.className = `countdown ${cls}`;
      }
    }

    const decisionEl = card.querySelector('[data-role="decision-countdown"]');
    if (decisionEl) {
      const closeTsMs = Number(card.dataset.closeTs);
      const decisionAtMs = closeTsMs - decisionLeadSec * 1000;
      const msRemaining = decisionAtMs - Date.now();
      if (msRemaining <= 0) {
        decisionEl.textContent = "any moment\u2026";
      } else {
        const { text } = formatCountdown(msRemaining);
        decisionEl.textContent = text;
      }
    }
  });
}

function setStatus(state, label) {
  const el = document.getElementById("conn-status");
  el.className = `status status--${state}`;
  el.textContent = label;
}

let liveSocket = null;
let liveSocketBackoffMs = 1000;

function connectLiveSocket() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  liveSocket = new WebSocket(`${protocol}//${location.host}/ws/live`);

  liveSocket.addEventListener("open", () => {
    liveSocketBackoffMs = 1000;
    setStatus("live", "live");
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
    setStatus("pending", "reconnecting…");
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

function calibrationRowHtml(label, c) {
  if (!c.n) {
    return `<div class="calibration"><span class="calibration-label">${label}: waiting for the first settled market&hellip;</span></div>`;
  }
  const better = c.model_brier <= c.market_brier;
  return (
    '<div class="calibration">' +
    `<span class="calibration-label">${label} (last ${c.n} settled)</span>` +
    '<span class="calibration-chips">' +
    `<span class="chip ${better ? "chip--good" : "chip--bad"}">Us ${c.model_brier.toFixed(3)}</span>` +
    `<span class="chip">Market ${c.market_brier.toFixed(3)}</span>` +
    `<span class="chip chip--muted">Coin flip ${c.baseline_brier.toFixed(3)}</span>` +
    "</span>" +
    '<span class="calibration-hint">lower = more accurate</span>' +
    "</div>"
  );
}

async function refreshCalibration() {
  const el = document.getElementById("calibration");
  try {
    const [overall, btc, sol] = await Promise.all([
      fetchCalibration(),
      fetchCalibration("BRTI"),
      fetchCalibration("SOLUSD_RTI"),
    ]);
    el.innerHTML = calibrationRowHtml("Overall track record", overall);

    calibrationByCoin.BRTI = btc;
    calibrationByCoin.SOLUSD_RTI = sol;
    // Patch section headers already in the DOM in place, rather than re-rendering
    // all cards (which would wipe out sparkline canvases/live state).
    document.querySelectorAll(".coin-section-header").forEach((header) => {
      header.innerHTML = coinSectionHeaderInnerHtml(header.dataset.indexId);
    });
  } catch {
    el.innerHTML = '<div class="calibration">Calibration: unavailable</div>';
  }
}

function setTab(tab) {
  currentTab = tab;
  document.querySelectorAll(".tab-button").forEach((btn) => {
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
    setStatus("live", "live");
    document.getElementById("last-updated").textContent =
      `Dashboard refreshed ${new Date().toLocaleTimeString()}`;
  } catch (err) {
    setStatus("error", "connection error");
  }
}

document.querySelectorAll(".tab-button").forEach((btn) => {
  btn.addEventListener("click", () => setTab(btn.dataset.tab));
});

fetch("/api/config")
  .then((res) => res.json())
  .then((cfg) => {
    if (cfg.decision_lead_sec) decisionLeadSec = cfg.decision_lead_sec;
  })
  .catch(() => {});

connectLiveSocket();
refresh();
refreshCalibration();
// Price updates now arrive instantly over the WebSocket; this poll only needs
// to catch prediction/edge recomputation and market open/close transitions.
setInterval(refresh, 5000);
setInterval(tickCountdowns, 1000);
setInterval(refreshCalibration, 15000);



