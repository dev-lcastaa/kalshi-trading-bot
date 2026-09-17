import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

const activeMarket = {
  ticker: "KXBTC15M-TEST",
  index_id: "BRTI",
  strike: 100000,
  close_ts_ms: Date.now() + 600000,
  status: "active",
  index_price: 100120,
  model_p_yes: 0.68,
  market_p_yes: 0.57,
  edge: 0.11,
  recommendation: "BUY_YES",
  confidence: 0.7,
  decision_recommendation: "BUY_YES",
  decision_confidence: 0.72,
  decision_model_p_yes: 0.68,
  decision_edge: 0.11,
  decision_index_price: 100100,
  decision_confirmation_agree: 2,
  decision_confirmation_total: 3,
  decision_confirmation_detail: JSON.stringify([{ name: "momentum", agree: true }, { name: "book", agree: false }]),
  llm_8m30_decision: "ALLOW",
  llm_8m30_reason: "Inputs are fresh.",
  llm_early_decision: "REDUCE_CONFIDENCE",
  llm_early_reason: "Spread widened.",
};

function json(data) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve(data), status: 200, statusText: "OK" });
}

let closedPages;

beforeEach(() => {
  localStorage.clear();
  closedPages = [[]];
  vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
    const url = String(input);
    if (url.startsWith("/api/active")) return json([activeMarket]);
    if (url.startsWith("/api/closed")) {
      const offset = Number(new URL(url, "http://localhost").searchParams.get("offset"));
      return json(offset ? closedPages[1] || [] : closedPages[0]);
    }
    if (url.startsWith("/api/shadow-active")) return json([{ ...activeMarket, recommendation: "BUY_NO", confirmation_agree: 2, confirmation_total: 3 }]);
    if (url.startsWith("/api/config")) return json({ decision_lead_sec: 390 });
    if (url.startsWith("/api/calibration-summary")) return json({ overall: { n: 0 }, BRTI: { n: 0 }, SOLUSD_RTI: { n: 0 } });
    if (url.startsWith("/api/external-status")) return json({ healthy: true, sources: [] });
    if (url.startsWith("/api/shadow-comparison")) return json({ groups: [] });
    if (url.startsWith("/api/price-history")) return json([{ ts_ms: Date.now() - 1000, value: 100000 }, { ts_ms: Date.now(), value: 100120 }]);
    if (url.startsWith("/api/whale-trades")) return json([]);
    throw new Error(`Unhandled request: ${url}`);
  });
});

describe("dashboard parity", () => {
  it("shows decision context and all five Jetson checkpoints", async () => {
    render(<App />);

    expect(await screen.findByText("KXBTC15M-TEST")).toBeTruthy();
    expect(screen.getByText("Bot says YES")).toBeTruthy();
    expect(screen.getByText("Market says YES")).toBeTruthy();
    expect(screen.getByText("2/3 passed")).toBeTruthy();
    for (const label of ["T-8:30", "T-6:30", "T-4:30", "T-2:30", "T-1:00"]) {
      expect(screen.getByText(label)).toBeTruthy();
    }
  });

  it("copies the market ticker and exposes the permanent Test Lab warning", async () => {
    const user = userEvent.setup();
    render(<App />);
    const copy = await screen.findByRole("button", { name: "Copy KXBTC15M-TEST" });
    await user.click(copy);
    expect(await screen.findByText("Copied")).toBeTruthy();

    await user.click(screen.getByRole("tab", { name: /Test Lab/ }));
    expect(await screen.findByText("Test model only")).toBeTruthy();
    expect(screen.getByText(/Do not use these picks to bet/)).toBeTruthy();
  });

  it("filters visible markets without refetching", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("KXBTC15M-TEST");
    const callsBefore = fetch.mock.calls.length;

    await user.type(screen.getByPlaceholderText("Search markets"), "missing");
    expect(screen.getByText("No markets found")).toBeTruthy();
    await user.clear(screen.getByPlaceholderText("Search markets"));
    expect(screen.getByText("KXBTC15M-TEST")).toBeTruthy();
    await waitFor(() => expect(fetch.mock.calls.length).toBe(callsBefore));
  });

  it("loads closed decisions incrementally", async () => {
    closedPages = [
      Array.from({ length: 24 }, (_, index) => ({ ...activeMarket, ticker: `CLOSED-${index}`, status: "closed", result: "yes" })),
      [{ ...activeMarket, ticker: "CLOSED-24", status: "closed", result: "yes" }],
    ];
    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByRole("tab", { name: /Past Results/ }));
    expect(await screen.findByText("CLOSED-0")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Show more results" }));
    expect(await screen.findByText("CLOSED-24")).toBeTruthy();
    expect(fetch.mock.calls.some(([url]) => String(url).includes("offset=24"))).toBe(true);
  });
});
