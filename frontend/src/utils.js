export const COINS = {
  BRTI: { name: "Bitcoin", symbol: "BTC", accent: "#f4b860" },
  SOLUSD_RTI: { name: "Solana", symbol: "SOL", accent: "#55d6c2" },
};

export function coinMeta(indexId) {
  return COINS[indexId] || { name: indexId || "Unknown", symbol: indexId || "--", accent: "#a8b4c7" };
}

export function money(value) {
  return value == null || Number.isNaN(Number(value))
    ? "--"
    : `$${Number(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function percent(value) {
  return value == null || Number.isNaN(Number(value)) ? "--" : `${(Number(value) * 100).toFixed(1)}%`;
}

export function countdown(milliseconds) {
  const seconds = Math.max(0, Math.floor(Number(milliseconds) / 1000));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

export function formatDecision(decision) {
  if (!decision) return "AWAITING";
  if (decision === "REDUCE_CONFIDENCE") return "BE CAREFUL";
  if (decision === "ALLOW") return "LOOKS GOOD";
  if (decision === "BLOCK") return "STOP";
  if (decision === "SCHEDULED") return "COMING SOON";
  if (decision === "AWAITING") return "WAITING";
  return decision.replaceAll("_", " ");
}

export function recommendationLabel(recommendation) {
  if (recommendation === "BUY_YES") return "BET UP";
  if (recommendation === "BUY_NO") return "BET DOWN";
  if (recommendation === "NO_EDGE") return "NO TRADE";
  return "LOCKING IN";
}
