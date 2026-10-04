import { expect, test } from "@playwright/test";

const settings = { budget: "1.00", take_profit: "0.50", stop_loss: "0.10" };

function makeSnapshot(mode, { populated = false, environment = "demo" } = {}) {
  return {
    mode,
    settings: { ...settings },
    enabled: false,
    environment,
    blockers: [],
    last_cycle_ms: Date.now(),
    error: null,
    positions: populated ? [{ ticker: "BTC-POSITION", side: "yes", status: "pending", quantity: "2", entry_cost: "0.92", exit_credit: "0", net_pnl: null, policy: { ...settings }, pending: { client_order_id: "pending-buy", action: "buy", order_id: "order-123" } }] : [],
    events: populated ? [
      { id: 1, ts_ms: Date.now(), action: "buy", reason: "Confirmed checks", ticker: "BTC-POSITION" },
      { id: 2, ts_ms: Date.now(), action: "enabled", reason: "Trading was enabled" },
    ] : [],
    decisions: populated ? [
      { ticker: "BTC-POSITION", ts_ms: Date.now(), recommendation: "BUY_YES", confidence: 0.8, confirmation_detail: JSON.stringify([{ name: "momentum", agree: true }, { name: "spread", agree: false }]), result: null, close_ts_ms: Date.now() + 60000 },
      { ticker: "BTC-OTHER", ts_ms: Date.now(), recommendation: "NO_EDGE", confidence: 0.55, confirmation_detail: "[]", result: "yes", close_ts_ms: Date.now() + 60000 },
    ] : [],
  };
}

async function mockTrading(page, { populated = true, environment = "demo" } = {}) {
  await page.routeWebSocket("**/ws/**", () => {});
  const state = { live: makeSnapshot("live", { populated, environment }), paper: makeSnapshot("paper", { environment: "demo" }) };
  let failure = false;
  const writes = [];
  let reads = 0;
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.startsWith("/api/trading")) {
      if (request.method() === "GET") reads += 1;
      if (failure) { await route.fulfill({ status: 503, json: { detail: "Trading service unavailable" } }); return; }
      if (request.method() !== "GET") {
        const body = request.postDataJSON();
        writes.push({ path, body, auth: request.headers().authorization });
        if (path.endsWith("settings")) state[body.mode].settings = { budget: body.budget, take_profit: body.take_profit, stop_loss: body.stop_loss };
        else state[body.mode].enabled = body.enabled;
      }
      await route.fulfill({ json: state });
      return;
    }
    await route.fulfill({ json: ["/api/active", "/api/closed", "/api/shadow-active", "/api/price-history"].includes(path) ? [] : {} });
  });
  return {
    writes,
    get reads() { return reads; },
    fail(value) { failure = value; },
    block() { state.live.blockers = ["Order entries blocked"]; },
  };
}

async function noOverflow(page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
}

for (const [name, size] of [["desktop", { width: 1440, height: 900 }], ["mobile", { width: 390, height: 844 }]]) {
  test(`${name}: live journey with position cards, save, dialog, and off with blockers`, async ({ page }) => {
    await page.setViewportSize(size);
    const api = await mockTrading(page);
    await page.goto("/trading");
    await expect(page.getByRole("tab", { name: "Trading", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("radio", { name: "Live auto trading" })).toBeChecked();
    await expect(page.getByText("Live trading — demo account")).toBeVisible();
    await expect(page.getByPlaceholder("Search markets")).toHaveCount(0);
    const card = page.getByRole("article", { name: "Position BTC-POSITION" });
    await expect(card).toBeVisible();
    await expect(card.getByText("Confirmed checks")).toBeVisible();
    await expect(card.getByText("BUY_YES")).toBeVisible();
    await card.getByText("Confirmation checks").click();
    await expect(card.getByText("Momentum", { exact: false })).toBeVisible();
    await expect(card.getByText("pending-buy")).toBeVisible();
    const activity = page.getByRole("region", { name: "Activity" });
    await expect(activity.getByText("Trading was enabled")).toBeVisible();
    await expect(activity.getByText("Confirmed checks")).toHaveCount(0);
    await page.getByText("Other decisions (1)").click();
    await expect(page.getByText("BTC-OTHER")).toBeVisible();
    await expect(page.getByText("NO_EDGE")).toBeVisible();
    await page.getByLabel("Budget ($)", { exact: true }).fill("1.75");
    await page.getByLabel("Take profit ($)", { exact: true }).fill("0.65");
    await page.getByLabel("Stop loss ($)", { exact: true }).fill("0.25");
    await expect(page.getByRole("switch")).toBeDisabled();
    await page.getByRole("button", { name: "Refresh trading" }).click();
    await expect(page.getByLabel("Budget ($)", { exact: true })).toHaveValue("1.75");
    await page.getByRole("button", { name: "Save settings" }).click();
    await expect(page.getByText("Settings saved.", { exact: true })).toBeVisible();
    expect(api.writes[0]).toEqual({ path: "/api/trading/settings", body: { mode: "live", budget: "1.75", take_profit: "0.65", stop_loss: "0.25" }, auth: undefined });
    await page.getByRole("switch").click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText("Enable live trading?")).toBeVisible();
    for (const value of ["$1.75", "$0.65", "$0.25"]) await expect(dialog.getByText(value, { exact: true })).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Confirm enable" })).toBeDisabled();
    await expect(page.getByRole("switch")).not.toBeChecked();
    await noOverflow(page);
    await page.screenshot({ path: `test-results/trading-confirm-${name}.png` });
    await dialog.getByRole("checkbox").check();
    await dialog.getByRole("button", { name: "Confirm enable" }).click();
    await expect(dialog).toHaveCount(0);
    await expect(page.getByRole("switch")).toBeChecked();
    expect(api.writes[1].body).toEqual({ mode: "live", enabled: true, confirm: true, settings: { budget: "1.75", take_profit: "0.65", stop_loss: "0.25" } });
    expect(api.writes[1].auth).toBeUndefined();
    api.block();
    await page.getByRole("button", { name: "Refresh trading" }).click();
    await expect(page.getByText("Order entries blocked")).toBeVisible();
    await expect(page.getByRole("switch")).toBeEnabled();
    await page.getByRole("switch").click();
    await expect(page.getByText("Live trading disabled.", { exact: true })).toBeVisible();
    await expect(page.getByRole("switch")).not.toBeChecked();
    expect(api.writes[2].body).toEqual({ mode: "live", enabled: false, confirm: false });
    await noOverflow(page);
    await page.screenshot({ path: `test-results/trading-${name}.png`, fullPage: true });
    await page.getByRole("tab", { name: /Live Picks/ }).click();
    const reads = api.reads;
    await page.clock.install();
    await page.clock.fastForward(6000);
    expect(api.reads).toBe(reads);
    await page.getByRole("tab", { name: "Trading", exact: true }).click();
    await expect(page.getByLabel("Budget ($)", { exact: true })).toHaveValue("1.75");
  });

  test(`${name}: paper toggles instantly without a dialog and keeps per-mode settings`, async ({ page }) => {
    await page.setViewportSize(size);
    const api = await mockTrading(page, { populated: false, environment: "prod" });
    await page.goto("/trading");
    await expect(page.getByText("Live trading — real money")).toBeVisible();
    await page.getByRole("radio", { name: "Paper auto trading" }).check();
    await expect(page.getByText("Paper trading — simulated, no real money")).toBeVisible();
    await expect(page.getByText("No positions", { exact: true })).toBeVisible();
    await page.getByRole("switch").click();
    await expect(page.getByText("Paper trading enabled.", { exact: true })).toBeVisible();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.getByRole("switch")).toBeChecked();
    expect(api.writes[0]).toEqual({ path: "/api/trading/control", body: { mode: "paper", enabled: true, confirm: true, settings: { ...settings } }, auth: undefined });
    await expect(page.getByText("Pause trading to change settings.")).toBeVisible();
    await page.getByRole("switch").click();
    await expect(page.getByText("Paper trading disabled.", { exact: true })).toBeVisible();
    expect(api.writes[1].body).toEqual({ mode: "paper", enabled: false, confirm: false });
    await page.getByLabel("Budget ($)", { exact: true }).fill("1.40");
    await page.getByRole("radio", { name: "Live auto trading" }).check();
    await expect(page.getByText("Live trading — real money")).toBeVisible();
    await expect(page.getByLabel("Budget ($)", { exact: true })).toHaveValue("1.00");
    await page.getByRole("radio", { name: "Paper auto trading" }).check();
    await expect(page.getByLabel("Budget ($)", { exact: true })).toHaveValue("1.40");
    await noOverflow(page);
    await page.screenshot({ path: `test-results/trading-paper-${name}.png`, fullPage: true });
  });

  test(`${name}: failures retain data, disable enable, and recover`, async ({ page }) => {
    await page.setViewportSize(size);
    const api = await mockTrading(page, { populated: false, environment: "prod" });
    await page.goto("/trading");
    await expect(page.getByText("No positions", { exact: true })).toBeVisible();
    await expect(page.getByText("No activity recorded")).toBeVisible();
    await page.getByRole("switch").click();
    await expect(page.getByRole("dialog").getByText("Real-money orders will be placed.")).toBeVisible();
    await page.getByRole("dialog").getByRole("checkbox").check();
    api.fail(true);
    await page.getByRole("button", { name: "Confirm enable" }).click();
    await expect(page.getByText("Trading service unavailable", { exact: true })).toBeVisible();
    await expect(page.getByRole("switch")).not.toBeChecked();
    await expect(page.getByText("Live trading enabled.", { exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "Refresh trading" }).click();
    await expect(page.getByText(/Last data retained/)).toBeVisible();
    await expect(page.getByRole("switch")).toBeDisabled();
    await expect(page.getByText("No positions", { exact: true })).toBeVisible();
    api.fail(false);
    await page.getByRole("button", { name: "Retry", exact: true }).click();
    await expect(page.getByRole("switch")).toBeEnabled();
    await page.getByLabel("Budget ($)", { exact: true }).fill("1.60");
    api.fail(true);
    await page.getByRole("button", { name: "Save settings" }).click();
    await expect(page.getByText("Trading service unavailable", { exact: true })).toBeVisible();
    await expect(page.getByText("Settings saved.", { exact: true })).toHaveCount(0);
    await expect(page.getByLabel("Budget ($)", { exact: true })).toHaveValue("1.60");
    await noOverflow(page);
  });
}