import { expect, test } from "@playwright/test";

const rule = { name: "Edge", enabled: true, coin: "ANY", side: "model", min_price: "0.50", max_price: "0.95", min_confidence: "0.50", min_edge: "0.00", min_seconds_left: 330, max_seconds_left: 390, budget: "1.00", take_profit: "0.50", stop_loss: "0.10" };
const settings = { rules: [rule] };
const policy = { budget: "1.00", take_profit: "0.50", stop_loss: "0.10" };
const SPEND = "Most to spend per trade ($)";
const ruleGroup = (page, index = 1) => page.getByRole("group", { name: `Rule ${index}` });

function makeSnapshot(mode, { populated = false, environment = "demo" } = {}) {
  const now = Date.now();
  return {
    mode,
    settings: structuredClone(settings),
    watch: populated ? [
      { ticker: "KXBTC15M-WATCH", seconds_left: 360, model_p_yes: 0.84, market_p_yes: 0.8, side: "yes", price: "0.80", rule: "Edge", status: "Matches 'Edge'" },
      { ticker: "KXSOL15M-WATCH", seconds_left: 700, model_p_yes: 0.4, market_p_yes: 0.45, side: null, price: null, rule: null, status: "No match - Edge: 700s left is outside 330-390s" },
    ] : [],
    enabled: false,
    environment,
    blockers: [],
    last_cycle_ms: now,
    error: null,
    positions: populated ? [
      { ticker: "KXBTC15M-POSITION", side: "yes", rule: "Edge", status: "pending", quantity: "2", entry_cost: "0.92", exit_credit: "0", net_pnl: null, close_ts_ms: now + 300000, opened_ms: now, policy: { ...policy }, pending: { client_order_id: "pending-buy", action: "buy", order_id: "order-123" } },
      { ticker: "KXSOL15M-DONE", side: "no", rule: "Edge", status: "closed", quantity: "3", entry_cost: "2.10", exit_credit: "3.00", net_pnl: "0.90", closed_by: "settled", result: "no", opened_ms: now - 900000, closed_ms: now - 600000, policy: { ...policy }, pending: null },
    ] : [],
    events: populated ? [
      { id: 1, ts_ms: now, action: "buy_submitted", reason: "Rule Edge matched", ticker: "KXBTC15M-POSITION" },
      { id: 2, ts_ms: now, action: "enabled", reason: "Auto trading enabled" },
    ] : [],
    decisions: populated ? [
      { ticker: "KXBTC15M-POSITION", ts_ms: now, recommendation: "BUY_YES", confidence: 0.8, confirmation_detail: JSON.stringify([{ name: "momentum", agree: true }, { name: "spread", agree: false }]), result: null, close_ts_ms: now + 60000 },
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
        if (path.endsWith("settings")) state[body.mode].settings = { rules: body.rules };
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
    block() { state.live.blockers = ["Trading worker is not healthy"]; },
  };
}

async function noOverflow(page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
}

for (const [name, size] of [["desktop", { width: 1440, height: 900 }], ["mobile", { width: 390, height: 844 }]]) {
  test(`${name}: real-money journey with running and finished bets, save, dialog, and off with blockers`, async ({ page }) => {
    await page.setViewportSize(size);
    const api = await mockTrading(page);
    await page.goto("/trading");
    await expect(page.getByRole("tab", { name: "Trading", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("radio", { name: "Practice (fake money)" })).toBeChecked();
    await page.getByRole("radio", { name: "Real money" }).check();
    await expect(page.getByText("Real-money mode — Kalshi demo account (still not real money)")).toBeVisible();
    const card = page.getByRole("article", { name: "Trade KXBTC15M-POSITION" });
    await expect(card).toBeVisible();
    await expect(card.getByText("Bet UP")).toBeVisible();
    await expect(card.getByText("Placing order…")).toBeVisible();
    await card.getByText("What the bot did").click();
    await expect(card.getByText("Placed a bet")).toBeVisible();
    await card.getByText("Why the bot made this bet").click();
    await expect(card.getByText("it would go UP")).toBeVisible();
    await expect(card.getByText("Momentum", { exact: false })).toBeVisible();
    const finished = page.getByRole("region", { name: "Finished bets" });
    await expect(finished.getByText("Won")).toBeVisible();
    await expect(finished.getByText("+$0.90")).toBeVisible();
    await expect(finished.getByText("Market ended")).toBeVisible();
    await expect(page.getByRole("article", { name: "Trade KXSOL15M-DONE" })).toHaveCount(0);
    const watch = page.getByRole("region", { name: "Markets the bot is watching" });
    await expect(watch.getByText("UP at 80¢")).toBeVisible();
    await expect(watch.getByText("Not yet: Edge: 11:40 left — waits for 6:30 to 5:30")).toBeVisible();
    await ruleGroup(page).getByLabel(SPEND, { exact: true }).fill("1.75");
    await ruleGroup(page).getByLabel("Cash out when up by ($)", { exact: true }).fill("0.65");
    await ruleGroup(page).getByLabel("Cut losses when down by ($)", { exact: true }).fill("0.25");
    await expect(page.getByRole("switch")).toBeDisabled();
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(ruleGroup(page).getByLabel(SPEND, { exact: true })).toHaveValue("1.75");
    await page.getByRole("button", { name: "Save rules" }).click();
    await expect(page.getByText("Rules saved.", { exact: true })).toBeVisible();
    const saved = { rules: [{ ...rule, budget: "1.75", take_profit: "0.65", stop_loss: "0.25" }] };
    expect(api.writes[0]).toEqual({ path: "/api/trading/settings", body: { mode: "live", ...saved }, auth: undefined });
    await page.getByRole("switch").click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText("Turn on the real-money bot?")).toBeVisible();
    await expect(dialog.getByText(/Spend up to \$1\.75\. Cash out when up \$0\.65, sell if down \$0\.25\./)).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Yes, turn it on" })).toBeDisabled();
    await expect(page.getByRole("switch")).not.toBeChecked();
    await noOverflow(page);
    await page.screenshot({ path: `test-results/trading-confirm-${name}.png` });
    await dialog.getByRole("checkbox").check();
    await dialog.getByRole("button", { name: "Yes, turn it on" }).click();
    await expect(dialog).toHaveCount(0);
    await expect(page.getByRole("switch")).toBeChecked();
    await expect(page.getByText("The bot is ON")).toBeVisible();
    expect(api.writes[1].body).toEqual({ mode: "live", enabled: true, confirm: true, settings: saved });
    expect(api.writes[1].auth).toBeUndefined();
    await noOverflow(page);
    await page.screenshot({ path: `test-results/trading-${name}.png`, fullPage: true });
    api.block();
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(page.getByText("The bot's trading engine isn't running properly.")).toBeVisible();
    await expect(page.getByRole("switch")).toBeEnabled();
    await page.getByRole("switch").click();
    await expect(page.getByText("Real-money bot turned off.", { exact: true })).toBeVisible();
    await expect(page.getByRole("switch")).not.toBeChecked();
    expect(api.writes[2].body).toEqual({ mode: "live", enabled: false, confirm: false });
    await noOverflow(page);
    await page.getByRole("tab", { name: /Live Picks/ }).click();
    const reads = api.reads;
    await page.clock.install();
    await page.clock.fastForward(6000);
    expect(api.reads).toBe(reads);
    await page.getByRole("tab", { name: "Trading", exact: true }).click();
    await page.getByRole("radio", { name: "Real money" }).check();
    await expect(ruleGroup(page).getByLabel(SPEND, { exact: true })).toHaveValue("1.75");
  });

  test(`${name}: practice turns on instantly without a dialog and keeps per-mode rules`, async ({ page }) => {
    await page.setViewportSize(size);
    const api = await mockTrading(page, { populated: false, environment: "prod" });
    await page.goto("/trading");
    await expect(page.getByText("Practice mode — pretend money, nothing real is spent")).toBeVisible();
    await expect(page.getByText("No bets running right now", { exact: true })).toBeVisible();
    await expect(page.getByText("No finished bets yet", { exact: true })).toBeVisible();
    await page.getByRole("switch").click();
    await expect(page.getByText("Practice bot turned on.", { exact: true })).toBeVisible();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.getByRole("switch")).toBeChecked();
    expect(api.writes[0]).toEqual({ path: "/api/trading/control", body: { mode: "paper", enabled: true, confirm: true, settings }, auth: undefined });
    await expect(page.getByText("Turn the bot off to change rules.")).toBeVisible();
    await page.getByRole("switch").click();
    await expect(page.getByText("Practice bot turned off.", { exact: true })).toBeVisible();
    expect(api.writes[1].body).toEqual({ mode: "paper", enabled: false, confirm: false });
    await ruleGroup(page).getByLabel(SPEND, { exact: true }).fill("1.40");
    await page.getByRole("radio", { name: "Real money" }).check();
    await expect(page.getByText("Real-money mode — uses your real Kalshi money")).toBeVisible();
    await expect(ruleGroup(page).getByLabel(SPEND, { exact: true })).toHaveValue("1.00");
    await page.getByRole("radio", { name: "Practice (fake money)" }).check();
    await expect(ruleGroup(page).getByLabel(SPEND, { exact: true })).toHaveValue("1.40");
    await noOverflow(page);
    await page.screenshot({ path: `test-results/trading-paper-${name}.png`, fullPage: true });
  });

  test(`${name}: failures keep old data, block turning on, and recover`, async ({ page }) => {
    await page.setViewportSize(size);
    const api = await mockTrading(page, { populated: false, environment: "prod" });
    await page.goto("/trading");
    await page.getByRole("radio", { name: "Real money" }).check();
    await expect(page.getByText("No bets running right now", { exact: true })).toBeVisible();
    await page.getByRole("switch").click();
    await expect(page.getByRole("dialog").getByText("The bot will spend your real money.")).toBeVisible();
    await page.getByRole("dialog").getByRole("checkbox").check();
    api.fail(true);
    await page.getByRole("button", { name: "Yes, turn it on" }).click();
    await expect(page.getByText("Trading service unavailable", { exact: true })).toBeVisible();
    await expect(page.getByRole("switch")).not.toBeChecked();
    await expect(page.getByText("Real-money bot turned on.", { exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(page.getByText(/Showing the last info we got/)).toBeVisible();
    await expect(page.getByRole("switch")).toBeDisabled();
    api.fail(false);
    await page.getByRole("button", { name: "Try again", exact: true }).click();
    await expect(page.getByRole("switch")).toBeEnabled();
    await ruleGroup(page).getByLabel(SPEND, { exact: true }).fill("1.60");
    api.fail(true);
    await page.getByRole("button", { name: "Save rules" }).click();
    await expect(page.getByText("Trading service unavailable", { exact: true })).toBeVisible();
    await expect(page.getByText("Rules saved.", { exact: true })).toHaveCount(0);
    await expect(ruleGroup(page).getByLabel(SPEND, { exact: true })).toHaveValue("1.60");
    await noOverflow(page);
  });
}
