import React from "react";

const CLOSED_PAGE_SIZE = 24;
const ENDPOINTS = {
  active: "/api/active",
  closed: `/api/closed?limit=${CLOSED_PAGE_SIZE}&offset=0`,
  shadow: "/api/shadow-active",
};

async function getJson(url, signal) {
  const response = await fetch(url, { cache: "no-store", signal });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

export function useDashboardData(tab) {
  const [rows, setRows] = React.useState([]);
  const [loading, setLoading] = React.useState(true);
  const [loadingMore, setLoadingMore] = React.useState(false);
  const [error, setError] = React.useState(null);
  const [lastUpdated, setLastUpdated] = React.useState(null);
  const [hasMore, setHasMore] = React.useState(false);
  const requestRef = React.useRef(null);
  const generationRef = React.useRef(0);

  const load = React.useCallback(async ({ append = false } = {}) => {
    const generation = ++generationRef.current;
    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;
    append ? setLoadingMore(true) : setLoading(true);
    try {
      const offset = append ? rows.length : 0;
      const url = tab === "closed" ? `/api/closed?limit=${CLOSED_PAGE_SIZE}&offset=${offset}` : ENDPOINTS[tab];
      const nextRows = await getJson(url, controller.signal);
      if (generation !== generationRef.current) return;
      setRows((current) => append
        ? [...new Map([...current, ...nextRows].map((row) => [row.ticker, row])).values()]
        : nextRows);
      setHasMore(tab === "closed" && nextRows.length === CLOSED_PAGE_SIZE);
      setError(null);
      setLastUpdated(new Date());
    } catch (nextError) {
      if (nextError.name !== "AbortError" && generation === generationRef.current) setError(nextError);
    } finally {
      if (generation === generationRef.current) {
        setLoading(false);
        setLoadingMore(false);
      }
    }
  }, [rows.length, tab]);

  React.useEffect(() => {
    setRows([]);
    setHasMore(false);
    void load();
    if (tab === "closed") return () => requestRef.current?.abort();
    const period = tab === "shadow" ? 30_000 : 5_000;
    const interval = window.setInterval(() => {
      if (document.visibilityState === "visible") void load();
    }, period);
    return () => {
      window.clearInterval(interval);
      requestRef.current?.abort();
    };
  }, [tab]); // The callback is intentionally regenerated as rows change; tab owns the polling lifecycle.

  return { rows, loading, loadingMore, error, lastUpdated, hasMore, reload: load, loadMore: () => load({ append: true }) };
}

export function useTelemetry(tab) {
  const [decisionLeadSec, setDecisionLeadSec] = React.useState(390);
  const [calibration, setCalibration] = React.useState(null);
  const [external, setExternal] = React.useState(null);
  const [shadowComparison, setShadowComparison] = React.useState(null);

  React.useEffect(() => {
    const controller = new AbortController();
    getJson("/api/config", controller.signal)
      .then((config) => setDecisionLeadSec(Number(config.decision_lead_sec) || 390))
      .catch(() => {});
    return () => controller.abort();
  }, []);

  React.useEffect(() => {
    let controller;
    const load = async () => {
      controller?.abort();
      controller = new AbortController();
      try {
        const summary = await getJson("/api/calibration-summary", controller.signal);
        setCalibration({ ...summary.overall, BRTI: summary.BRTI, SOLUSD_RTI: summary.SOLUSD_RTI });
      } catch (error) {
        if (error.name !== "AbortError") return;
      }
    };
    void load();
    const interval = window.setInterval(() => document.visibilityState === "visible" && void load(), 60_000);
    return () => { window.clearInterval(interval); controller?.abort(); };
  }, []);

  React.useEffect(() => {
    let controller;
    const load = async () => {
      controller?.abort();
      controller = new AbortController();
      try { setExternal(await getJson("/api/external-status", controller.signal)); } catch (error) { if (error.name !== "AbortError") setExternal((current) => current ? { ...current, healthy: false } : null); }
    };
    void load();
    const interval = window.setInterval(() => document.visibilityState === "visible" && void load(), 10_000);
    return () => { window.clearInterval(interval); controller?.abort(); };
  }, []);

  React.useEffect(() => {
    if (tab !== "shadow") return undefined;
    let controller;
    const load = async () => {
      controller?.abort();
      controller = new AbortController();
      try { setShadowComparison(await getJson("/api/shadow-comparison?limit=10000", controller.signal)); } catch (error) { if (error.name !== "AbortError") setShadowComparison(null); }
    };
    void load();
    const interval = window.setInterval(() => document.visibilityState === "visible" && void load(), 30_000);
    return () => { window.clearInterval(interval); controller?.abort(); };
  }, [tab]);

  return { decisionLeadSec, calibration, external, shadowComparison };
}
