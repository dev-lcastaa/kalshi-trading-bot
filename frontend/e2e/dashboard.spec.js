import { expect, test } from "@playwright/test";

const market = {
  ticker: "KXBTC15M-26SEP151200-100000",
  index_id: "BRTI",
  strike: 100000,
  status: "active",
  index_price: 100125,
  model_p_yes: .68,
  market_p_yes: .56,
  edge: .12,
  recommendation: "BUY_YES",
  confidence: .7,
  decision_recommendation: "BUY_YES",
  decision_confidence: .72,
  decision_model_p_yes: .68,
  decision_edge: .12,
  decision_index_price: 100080,
  decision_confirmation_agree: 2,
  decision_confirmation_total: 3,
  decision_confirmation_detail: JSON.stringify([{ name: "momentum", agree: true }, { name: "order book", agree: true }, { name: "spread", agree: false }]),
  llm_8m30_decision: "ALLOW",
  llm_8m30_reason: "Inputs are fresh and aligned.",
  llm_early_decision: "REDUCE_CONFIDENCE",
  llm_early_reason: "Spread widened near the decision boundary.",
  llm_4m30_decision: "ALLOW",
  llm_4m30_reason: "Momentum remains stable.",
};

async function mockApi(page) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const close_ts_ms = Date.now() + 7 * 60_000;
    let body;
    if (url.pathname === "/api/active") body = [{ ...market, close_ts_ms }];
    else if (url.pathname === "/api/closed") body = [];
    else if (url.pathname === "/api/shadow-active") body = [{ ...market, close_ts_ms, recommendation: "BUY_NO", confirmation_agree: 2, confirmation_total: 3 }];
    else if (url.pathname === "/api/config") body = { decision_lead_sec: 390 };
    else if (url.pathname === "/api/calibration-summary") body = { overall: { n: 42, settled_count: 76, model_brier: .182, market_brier: .211 }, BRTI: { n: 24, settled_count: 44, model_brier: .171, market_brier: .203 }, SOLUSD_RTI: { n: 18, settled_count: 32, model_brier: .196, market_brier: .223 } };
    else if (url.pathname === "/api/external-status") body = { healthy: true, sources: [{ source: "Coinbase", symbol: "BTC-USD", healthy: true, age_ms: 320 }, { source: "Kraken", symbol: "XBT/USD", healthy: true, age_ms: 540 }] };
    else if (url.pathname === "/api/price-history") body = Array.from({ length: 50 }, (_, index) => ({ ts_ms: Date.now() - (50 - index) * 12000, value: 99870 + index * 6 + Math.sin(index / 3) * 80 }));
    else if (url.pathname === "/api/shadow-comparison") body = { groups: [{ experiment_id: "v2-no-book-drift", index_id: "BRTI", pending: 3, scores: { live: { n: 18, brier: .189 }, shadow: { n: 18, brier: .176, actionable_correct: 9, actionable_n: 12 }, market: { brier: .214 } } }] };
    else if (url.pathname === "/api/whale-trades") body = [{ trade_id: "a", ts_ms: Date.now(), side: "yes", notional_usd: 1240, price_cents: 61.5 }, { trade_id: "b", ts_ms: Date.now() - 12000, side: "no", notional_usd: 780, price_cents: 39.2 }];
    else body = {};
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
}

async function expectNoHorizontalOverflow(page) {
  const dimensions = await page.evaluate(() => ({ viewport: window.innerWidth, document: document.documentElement.scrollWidth }));
  expect(dimensions.document).toBeLessThanOrEqual(dimensions.viewport);
}

test("desktop dashboard is complete and stable", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockApi(page);
  await page.goto("/");
  await expect(page.getByText(market.ticker)).toBeVisible();
  await expect(page.getByText("T-1:00", { exact: true })).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: "test-results/dashboard-desktop.png", fullPage: true });
});

test("iPhone 14 layout, order flow sheet, and Shadow Lab", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApi(page);
  await page.goto("/");
  await expect(page.getByText(market.ticker)).toBeVisible();
  await expectNoHorizontalOverflow(page);
  for (const control of await page.locator(".tabs button, .coin-filter button, .search").all()) {
    const box = await control.boundingBox();
    expect(box?.height).toBeGreaterThanOrEqual(44);
  }
  await page.screenshot({ path: "test-results/dashboard-iphone14.png", fullPage: true });

  await page.getByRole("button", { name: "Whale Tracker" }).click();
  await expect(page.getByRole("dialog", { name: /Whale Tracker/ })).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await page.getByText("$1,240.00").scrollIntoViewIfNeeded();
  await expect(page.getByText("$1,240.00")).toBeVisible();
  await page.screenshot({ path: "test-results/whale-iphone14.png" });
  await page.getByRole("button", { name: "Close Whale Tracker" }).click();

  await page.getByRole("tab", { name: /Test Lab/ }).click();
  await expect(page.getByText("Test model only")).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: "test-results/shadow-iphone14.png", fullPage: true });

  await page.goto("/shadow");
  await expect(page.getByRole("tab", { name: /Test Lab/ })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByText("Test model only")).toBeVisible();
});

test("iPhone 14 landscape remains readable without overflow", async ({ page }) => {
  await page.setViewportSize({ width: 844, height: 390 });
  await mockApi(page);
  await page.goto("/");
  await expect(page.getByText(market.ticker)).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: "test-results/dashboard-iphone14-landscape.png", fullPage: true });
});
