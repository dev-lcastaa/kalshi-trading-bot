import { expect, test } from "@playwright/test";

const rule = { name: "Edge", enabled: true, coin: "ANY", side: "model", min_price: "0.50", max_price: "0.95", min_confidence: "0.50", min_edge: "0.00", min_seconds_left: 330, max_seconds_left: 390, budget: "1.00", take_profit: "0.50", stop_loss: "0.10", max_entries: 1, reentry_gap_sec: 60 };
const settings = { rules: [rule] };
const policy = { budget: "1.00", take_profit: "0.50", stop_loss: "0.10" };
const SPEND = "Most to spend per buy ($)";
const ruleGroup = (page, index = 1) => page.getByRole("group", { name: `Rule ${index}` });

function makeSnapshot(mode, { populated = false, environment = "demo" } = {}) {
  const now = Date.now();
  return {
    mode,
    settings: structuredClone(settings),
    watch: populated ? [
      { ticker: "KXBTC15M-WATCH", seconds_left: 360, close_ts_ms: now + 360000, model_p_yes: 0.84, market_p_yes: 0.8, side: "yes", price: "0.80", rule: "Edge", status: "Matches 'Edge'" },
      { ticker: "KXSOL15M-WATCH", seconds_left: 700, close_ts_ms: now + 700000, model_p_yes: 0.4, market_p_yes: 0.45, side: null, price: null, rule: null, status: "No match - Edge: 700s left is outside 330-390s" },
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
  const state = { live: makeSnapshot("live", { populated, environment }), paper: makeSnapshot("paper", { environment: "demo" }) };
  let failure = false;
  const sockets = new Set();
  const push = () => {
    for (const socket of sockets) socket.send(JSON.stringify({ type: "trading_state", data: state }));
  };
  await page.routeWebSocket("**/ws/**", (socket) => {
    if (!socket.url().endsWith("/ws/trading")) return;
    sockets.add(socket);
    push();
    const timer = setInterval(() => { if (!failure) push(); }, 2000);
    socket.onClose(() => { clearInterval(timer); sockets.delete(socket); });
  });
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
    state,
    push,
    get reads() { return reads; },
    fail(value) { failure = value; },
    block() { state.live.blockers = ["Trading worker is not healthy"]; },
  };
}

async function noOverflow(page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
}

for (const [name, size] of [["desktop", { width: 1440, height: 900 }], ["mobile", { width: 390, height: 844 }]]) {
  test(`${name}: finished bet cards show wins, losses, and neutral outcomes`, async ({ page }) => {
    await page.setViewportSize(size);
    const api = await mockTrading(page);
    const won = api.state.live.positions.find((position) => position.status === "closed");
    api.state.live.positions.push(
      { ...won, ticker: "KXBTC15M-LOSS", side: "yes", entry_cost: "1.00", exit_credit: "0.50", net_pnl: "-0.50", closed_by: "stop_loss", closed_ms: won.closed_ms + 1000 },
      { ...won, ticker: "KXSOL15M-EVEN", entry_cost: "1.00", exit_credit: "1.00", net_pnl: "0.00", closed_by: "take_profit", rule: "A long saved rule name for this trade", closed_ms: won.closed_ms - 1000 },
      { ...won, ticker: "KXBTC15M-UNKNOWN", net_pnl: null, closed_by: null, closed_ms: won.closed_ms - 2000 },
    );
    await page.goto("/trading");
    await page.getByRole("radio", { name: "Real money" }).check();
    const finished = page.getByRole("region", { name: "Finished bets" });
    const cards = finished.getByRole("listitem");
    await expect(cards).toHaveCount(2);
    await expect(cards.first()).toHaveAttribute("aria-label", "Finished trade KXBTC15M-LOSS");
    await expect(cards.first().getByText("Lost", { exact: true })).toBeVisible();
    await expect(cards.first().getByText("-$0.50")).toBeVisible();
    await expect(cards.first().getByText("Sold to cut losses")).toBeVisible();
    await expect(cards.nth(1).getByText("Won", { exact: true })).toBeVisible();
    await expect(cards.nth(1).getByText("+$0.90")).toBeVisible();
    await expect(cards.nth(1).getByText("$2.10")).toBeVisible();
    await expect(cards.nth(1).getByText("$3.00")).toBeVisible();
    const compactBoxes = await cards.evaluateAll((elements) => elements.map((element) => element.getBoundingClientRect().height));
    for (const height of compactBoxes) expect(height).toBeLessThanOrEqual(name === "mobile" ? 174 : 187);
    await finished.getByRole("button", { name: "Load more" }).click();
    await expect(cards).toHaveCount(4);
    await expect(finished.getByRole("button", { name: "Load more" })).toHaveCount(0);
    await expect(cards.nth(2).getByText("Broke even")).toBeVisible();
    await expect(cards.nth(2).getByText("$0.00")).toBeVisible();
    await expect(cards.nth(2).getByText("Cashed out early")).toBeVisible();
    await expect(cards.nth(3).locator(".finished-result strong")).toHaveText("--");
    await cards.nth(2).getByText("Trade details", { exact: true }).click();
    await expect(cards.nth(2).getByText("A long saved rule name for this trade")).toBeVisible();
    await cards.nth(2).getByText("Trade details", { exact: true }).click();
    const boxes = await cards.evaluateAll((elements) => elements.map((element) => {
      const { x, y, width } = element.getBoundingClientRect();
      return { x, y, width };
    }));
    if (name === "mobile") expect(boxes[1].y).toBeGreaterThan(boxes[0].y);
    else {
      expect(boxes[1].y).toBe(boxes[0].y);
      expect(boxes[1].x).toBeGreaterThan(boxes[0].x);
    }
    await noOverflow(page);
    await finished.screenshot({ path: `test-results/finished-bets-${name}.png` });
    await finished.getByRole("button", { name: "Show fewer" }).click();
    await expect(cards).toHaveCount(2);
    await expect(cards.first()).toHaveAttribute("aria-label", "Finished trade KXBTC15M-LOSS");
  });

  test(`${name}: market cards stream changes and count down without API polling`, async ({ page }) => {
    await page.setViewportSize(size);
    const api = await mockTrading(page);
    await page.clock.install();
    await page.goto("/trading");
    await page.getByRole("radio", { name: "Real money" }).check();
    const watch = page.getByRole("region", { name: "Markets the bot is watching" });
    const card = watch.getByRole("article", { name: "Watching KXBTC15M-WATCH" });
    await expect(card.getByText("84%")).toBeVisible();
    await expect(watch.getByText("Live · 2 markets")).toBeVisible();
    await page.clock.pauseAt(new Date(await page.evaluate(() => Date.now()) + 1000));
    const reads = api.reads;
    const countdown = card.locator(".watch-countdown strong");
    const [minutes, seconds] = (await countdown.textContent()).split(":").map(Number);
    const expectedLeft = minutes * 60 + seconds - 6;
    await page.clock.runFor(6000);
    await expect(countdown).toHaveText(`${Math.floor(expectedLeft / 60)}:${String(expectedLeft % 60).padStart(2, "0")}`);
    expect(api.reads).toBe(reads);
    api.state.live.watch[0].model_p_yes = 0.71;
    api.state.live.watch[0].market_p_yes = 0.68;
    api.push();
    await expect(card.getByText("71%")).toBeVisible();
    await expect(card.getByText("68%")).toBeVisible();
    expect(api.reads).toBe(reads);
    await noOverflow(page);
    await watch.screenshot({ path: `test-results/trading-watch-${name}.png` });
  });

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
    await expect(watch.getByText("Not yet: Edge: waits for 6:30 to 5:30")).toBeVisible();
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
    await expect(dialog.getByText(/Spend up to \$1\.75 once per market\. Cash out when up \$0\.65, sell if down \$0\.25\./)).toBeVisible();
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
