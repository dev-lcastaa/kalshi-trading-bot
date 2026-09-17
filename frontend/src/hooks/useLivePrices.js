import React from "react";

const HISTORY_WINDOW_MS = 10 * 60_000;

export function useLivePrices(indexIds) {
  const [livePrices, setLivePrices] = React.useState({});
  const [histories, setHistories] = React.useState({});
  const [socketStatus, setSocketStatus] = React.useState("CONNECTING");
  const [lastTickAt, setLastTickAt] = React.useState(null);
  const seededRef = React.useRef(new Set());
  const queueRef = React.useRef(new Map());
  const frameRef = React.useRef(null);
  const idsKey = [...new Set(indexIds.filter(Boolean))].sort().join(",");

  React.useEffect(() => {
    const ids = idsKey ? idsKey.split(",").filter((id) => !seededRef.current.has(id)) : [];
    if (!ids.length) return undefined;
    const controller = new AbortController();
    Promise.all(ids.map(async (id) => {
      const response = await fetch(`/api/price-history?index_id=${encodeURIComponent(id)}&minutes=10`, { signal: controller.signal });
      if (!response.ok) throw new Error(String(response.status));
      return [id, await response.json()];
    })).then((entries) => {
      entries.forEach(([id]) => seededRef.current.add(id));
      setHistories((current) => ({ ...current, ...Object.fromEntries(entries) }));
      setLivePrices((current) => ({ ...current, ...Object.fromEntries(entries.filter(([, ticks]) => ticks.length).map(([id, ticks]) => [id, ticks[ticks.length - 1].value])) }));
    }).catch((error) => { if (error.name !== "AbortError") return; });
    return () => controller.abort();
  }, [idsKey]);

  React.useEffect(() => {
    let socket;
    let retryTimer;
    let retryMs = 1000;
    let stopped = false;

    const flush = () => {
      frameRef.current = null;
      const updates = [...queueRef.current.values()];
      queueRef.current.clear();
      if (!updates.length) return;
      const cutoff = Date.now() - HISTORY_WINDOW_MS;
      setLivePrices((current) => ({ ...current, ...Object.fromEntries(updates.map((tick) => [tick.index_id, tick.value])) }));
      setHistories((current) => {
        const next = { ...current };
        for (const tick of updates) next[tick.index_id] = [...(next[tick.index_id] || []), { ts_ms: tick.ts_ms, value: tick.value }].filter((point) => point.ts_ms >= cutoff).slice(-1200);
        return next;
      });
      setLastTickAt(new Date());
    };

    const connect = () => {
      if (stopped) return;
      setSocketStatus("CONNECTING");
      socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/live`);
      socket.onopen = () => { retryMs = 1000; setSocketStatus("LIVE"); };
      socket.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data);
          if (message.type !== "index_tick") return;
          queueRef.current.set(message.index_id, message);
          if (frameRef.current == null) frameRef.current = window.requestAnimationFrame(flush);
        } catch {}
      };
      socket.onerror = () => { setSocketStatus("OFFLINE"); socket.close(); };
      socket.onclose = () => {
        if (stopped) return;
        setSocketStatus("CONNECTING");
        retryTimer = window.setTimeout(connect, retryMs);
        retryMs = Math.min(retryMs * 2, 15_000);
      };
    };

    connect();
    return () => {
      stopped = true;
      window.clearTimeout(retryTimer);
      if (frameRef.current != null) window.cancelAnimationFrame(frameRef.current);
      socket?.close();
    };
  }, []);

  return { livePrices, histories, socketStatus, lastTickAt };
}
