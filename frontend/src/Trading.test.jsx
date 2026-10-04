import React from "react";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Trading, { friendlyStatus, fromUi, toUi, validateSettings } from "./Trading";

const RULE = { name: "Edge", enabled: true, coin: "ANY", side: "model", min_price: "0.50", max_price: "0.95", min_confidence: "0.50", min_edge: "0.00", min_seconds_left: 330, max_seconds_left: 390, budget: "1.00", take_profit: "0.50", stop_loss: "0.10", max_entries: 1, reentry_gap_sec: 60 };
const SETTINGS = { rules: [RULE] };
const withRule = (overrides) => ({ rules: [{ ...RULE, ...overrides }] });
const uiWith = (overrides) => ({ rules: [{ ...toUi(SETTINGS).rules[0], ...overrides }] });
const rule = (index = 1) => within(screen.getByRole("group", { name: `Rule ${index}` }));
const SPEND = "Most to spend per buy ($)";
const makeSnapshot = (mode, overrides = {}) => ({ mode, settings: structuredClone(SETTINGS), watch: [], enabled: false, environment: "demo", blockers: [], last_cycle_ms: null, error: null, positions: [], events: [], decisions: [], ...overrides });
let state;
let fail;
let malformed;
beforeEach(() => {
  state = { live: makeSnapshot("live"), paper: makeSnapshot("paper") };
  fail = false; malformed = false;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
    if (fail) return { ok: false, json: async () => ({ detail: "Control rejected" }) };
    if (malformed) return { ok: true, json: async () => ({ live: structuredClone(state.live) }) };
    if (options?.method === "PUT") { const body = JSON.parse(options.body); state[body.mode].settings = { rules: body.rules }; }
    if (options?.method === "POST") { const body = JSON.parse(options.body); state[body.mode].enabled = body.enabled; }
    return { ok: true, json: async () => structuredClone(state) };
  });
});

const lastWrite = (method) => fetch.mock.calls.filter(([, options]) => options?.method === method).at(-1);
const goLive = (user) => user.click(screen.getByRole("radio", { name: "Real money" }));

describe("Trading tab", () => {
  it("converts rules to friendly cents/percent and back, and validates them", () => {
    expect(toUi(SETTINGS).rules[0]).toMatchObject({ min_price: "50", max_price: "95", min_confidence: "50", min_edge: "0", min_seconds_left: "330", budget: "1.00" });
    expect(fromUi(toUi(SETTINGS))).toEqual(SETTINGS);
    expect(fromUi(uiWith({ min_edge: "", budget: "5", min_price: "60" })).rules[0]).toMatchObject({ min_edge: null, budget: "5.00", min_price: "0.60" });
    expect(validateSettings(toUi(SETTINGS))).toBe("");
    for (const ok of [{ budget: "25.00" }, { take_profit: "0", stop_loss: "0" }, { min_edge: "" }, { min_edge: "-5" }, { side: "no", coin: "SOL" }, { max_entries: "5", reentry_gap_sec: "0" }]) expect(validateSettings(uiWith(ok))).toBe("");
    for (const bad of [{ budget: "25.01" }, { budget: "0" }, { stop_loss: "1.00" }, { take_profit: "0.001" }, { min_price: "96" }, { max_price: "100" }, { min_price: "0.5" }, { min_confidence: "150" }, { min_seconds_left: "400" }, { max_seconds_left: "abc" }, { name: " " }, { max_entries: "0" }, { max_entries: "11" }, { reentry_gap_sec: "901" }]) expect(validateSettings(uiWith(bad))).not.toBe("");
    expect(validateSettings({ rules: [] })).not.toBe("");
  });
  it("translates watch statuses into plain words", () => {
    expect(friendlyStatus("Matches 'Edge'")).toBe('Betting now — matches "Edge"');
    expect(friendlyStatus("Matches 'Edge' (waiting: a position on this coin is already open)")).toMatch(/already betting on this coin/);
    expect(friendlyStatus("No match - Edge: model gives YES 0.62 < 0.70")).toBe("Not yet: Edge: bot is 62% sure, you want 70%");
    expect(friendlyStatus("No match - Edge: NO costs 0.30, outside 0.50-0.95")).toBe("Not yet: Edge: DOWN costs 30¢, your range is 50¢–95¢");
    expect(friendlyStatus("No match - Edge: 500s left is outside 330-390s")).toBe("Not yet: Edge: 8:20 left — waits for 6:30 to 5:30");
    expect(friendlyStatus("Live read is stale")).toBe("Skipping — price data is out of date");
  });
  it("starts on practice mode and adds/removes rules, saving in the API format", async () => {
    state.paper.watch = [{ ticker: "KXBTC15M-A", seconds_left: 360, model_p_yes: 0.84, market_p_yes: 0.8, side: "yes", price: "0.80", rule: "Edge", status: "Matches 'Edge'" }];
    const user = userEvent.setup(); render(<Trading />);
    const watch = await screen.findByRole("region", { name: "Markets the bot is watching" });
    expect(screen.getByRole("radio", { name: "Practice (fake money)" }).checked).toBe(true);
    expect(within(watch).getByText("BTC")).toBeTruthy();
    expect(within(watch).getByText("UP at 80¢")).toBeTruthy();
    expect(within(watch).getByText('Betting now — matches "Edge"')).toBeTruthy();
    expect(rule().getByLabelText("Lowest price to pay (¢)").value).toBe("50");
    await user.click(screen.getByRole("button", { name: "Add a rule" }));
    const second = rule(2);
    await user.selectOptions(second.getByLabelText("Which coin"), "SOL");
    await user.selectOptions(second.getByLabelText("Which way to bet"), "no");
    await user.clear(second.getByLabelText(SPEND)); await user.type(second.getByLabelText(SPEND), "20");
    await user.clear(second.getByLabelText("Minimum expected profit (¢)"));
    await user.click(screen.getByRole("button", { name: "Save rules" }));
    await screen.findByText("Rules saved.");
    const body = JSON.parse(lastWrite("PUT")[1].body);
    expect(body.mode).toBe("paper");
    expect(body.rules).toHaveLength(2);
    expect(body.rules[1]).toMatchObject({ name: "Rule 2", coin: "SOL", side: "no", min_price: "0.50", budget: "20.00", min_edge: null, take_profit: "0.00", stop_loss: "0.00", min_seconds_left: 330 });
    await user.click(screen.getByRole("button", { name: "Remove rule 1" }));
    expect(screen.getByRole("button", { name: "Remove rule 1" }).disabled).toBe(true);
    expect(rule(1).getByLabelText("Rule name").value).toBe("Rule 2");
  });
  it("keeps rules and unsaved changes separate per mode and resets after save", async () => {
    const user = userEvent.setup(); render(<Trading />);
    await screen.findByRole("group", { name: "Rule 1" });
    await goLive(user);
    expect(screen.getByText("Real-money mode — Kalshi demo account (still not real money)")).toBeTruthy();
    const budget = rule().getByLabelText(SPEND);
    await user.clear(budget); await user.type(budget, "1.75");
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    expect(rule().getByLabelText(SPEND).value).toBe("1.75");
    await user.click(screen.getByRole("radio", { name: "Practice (fake money)" }));
    expect(screen.getByText("Practice mode — pretend money, nothing real is spent")).toBeTruthy();
    const paperBudget = rule().getByLabelText(SPEND);
    expect(paperBudget.value).toBe("1.00");
    await user.clear(paperBudget); await user.type(paperBudget, "1.50");
    await goLive(user);
    expect(rule().getByLabelText(SPEND).value).toBe("1.75");
    expect(screen.getByText("Save your rule changes before turning the bot on.")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Save rules" }));
    await screen.findByText("Rules saved.");
    const [, put] = lastWrite("PUT");
    expect(JSON.parse(put.body)).toEqual({ mode: "live", ...withRule({ budget: "1.75" }) });
    expect(put.headers.Authorization).toBeUndefined();
    expect(state.live.settings.rules[0].budget).toBe("1.75");
    expect(state.paper.settings.rules[0].budget).toBe("1.00");
    expect(screen.getByText("All changes saved")).toBeTruthy();
    await user.click(screen.getByRole("radio", { name: "Practice (fake money)" }));
    expect(rule().getByLabelText(SPEND).value).toBe("1.50");
    expect(screen.getByText("You have unsaved changes")).toBeTruthy();
  });
  it("turns practice on instantly without a dialog", async () => {
    const user = userEvent.setup(); render(<Trading />);
    await screen.findByRole("group", { name: "Rule 1" });
    expect(screen.getByText("The bot is OFF")).toBeTruthy();
    await user.click(screen.getByRole("switch"));
    await screen.findByText("Practice bot turned on.");
    expect(screen.queryByRole("dialog")).toBeNull();
    const [, post] = lastWrite("POST");
    expect(JSON.parse(post.body)).toEqual({ mode: "paper", enabled: true, confirm: true, settings: SETTINGS });
    expect(post.headers.Authorization).toBeUndefined();
    expect(screen.getByRole("switch").checked).toBe(true);
    expect(screen.getByText("The bot is ON")).toBeTruthy();
    expect(screen.getByText("Turn the bot off to change rules.")).toBeTruthy();
    await user.click(screen.getByRole("switch"));
    await screen.findByText("Practice bot turned off.");
    expect(JSON.parse(lastWrite("POST")[1].body)).toEqual({ mode: "paper", enabled: false, confirm: false });
    expect(state.paper.enabled).toBe(false);
    expect(state.live.enabled).toBe(false);
  });
  it("needs the dialog and a checkbox to turn real money on, and can always turn off", async () => {
    const user = userEvent.setup(); render(<Trading />);
    await screen.findByText("No bets running right now");
    await goLive(user);
    await user.click(screen.getByRole("switch"));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Turn on the real-money bot?")).toBeTruthy();
    expect(within(dialog).getByText(/Spend up to \$1\.00/)).toBeTruthy();
    expect(within(dialog).getByText("Edge")).toBeTruthy();
    expect(within(dialog).getByRole("button", { name: "Yes, turn it on" }).disabled).toBe(true);
    await user.click(within(dialog).getByRole("checkbox"));
    await user.click(within(dialog).getByRole("button", { name: "Yes, turn it on" }));
    await screen.findByText("Real-money bot turned on.");
    expect(JSON.parse(lastWrite("POST")[1].body)).toEqual({ mode: "live", enabled: true, confirm: true, settings: SETTINGS });
    state.live.blockers = ["Trading worker is not healthy"];
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await screen.findByText("The bot's trading engine isn't running properly.");
    await user.click(screen.getByRole("switch"));
    await screen.findByText("Real-money bot turned off.");
    expect(JSON.parse(lastWrite("POST")[1].body)).toEqual({ mode: "live", enabled: false, confirm: false });
  });
  it("keeps old data on errors and never reports a failed turn-on as success", async () => {
    const user = userEvent.setup(); render(<Trading />);
    await screen.findByText("No bets running right now");
    await goLive(user);
    await user.click(screen.getByRole("switch"));
    await user.click(within(screen.getByRole("dialog")).getByRole("checkbox"));
    fail = true;
    await user.click(screen.getByRole("button", { name: "Yes, turn it on" }));
    await screen.findByText("Control rejected");
    expect(screen.queryByText("Real-money bot turned on.")).toBeNull();
    expect(screen.getByRole("switch").checked).toBe(false);
    expect(screen.getByRole("switch").disabled).toBe(true);
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(screen.getByText(/Showing the last info we got/)).toBeTruthy());
    expect(screen.getByText("No bets running right now")).toBeTruthy();
  });
  it("treats a response missing a mode as an error", async () => {
    const user = userEvent.setup(); render(<Trading />);
    await screen.findByText("No bets running right now");
    malformed = true;
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await screen.findByText(/Couldn't read the bot's status/);
    expect(screen.getByRole("switch").disabled).toBe(true);
    malformed = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.getByRole("switch").disabled).toBe(false));
  });
  it("shows loading and retries an unknown status", async () => {
    fetch.mockImplementationOnce(() => new Promise(() => {}));
    const user = userEvent.setup(); render(<Trading />);
    expect(screen.getByText("Loading the bot's status...")).toBeTruthy();
    expect(screen.queryByRole("switch")).toBeNull();
    fail = true;
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await screen.findByText(/Status unknown\./);
    fail = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByText("No bets running right now");
  });
  it("polls every three seconds only while mounted", async () => {
    vi.useFakeTimers();
    try {
      const view = render(<Trading />);
      await act(async () => {});
      expect(fetch).toHaveBeenCalledTimes(1);
      await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
      expect(fetch).toHaveBeenCalledTimes(3);
      view.unmount();
      await vi.advanceTimersByTimeAsync(6000);
      expect(fetch).toHaveBeenCalledTimes(3);
    } finally { vi.useRealTimers(); }
  });
  it("does not flip the switch before the server answers", async () => {
    const user = userEvent.setup(); render(<Trading />);
    await screen.findByRole("group", { name: "Rule 1" });
    let complete;
    fetch.mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }));
    await user.click(screen.getByRole("switch"));
    expect(screen.getByRole("switch").checked).toBe(false);
    const next = { live: structuredClone(state.live), paper: { ...structuredClone(state.paper), enabled: true } };
    await act(async () => { complete({ ok: true, json: async () => next }); });
    expect(screen.getByRole("switch").checked).toBe(true);
    await screen.findByText("Practice bot turned on.");
  });
  it("splits running bets from finished bets and shows a scoreboard", async () => {
    const now = Date.now();
    state.paper.positions = [
      { ticker: "KXBTC15M-RUN", side: "yes", rule: "Edge", status: "open", quantity: "2", entry_cost: "1.40", exit_credit: "0", net_pnl: "0.20", close_ts_ms: now + 125000, opened_ms: now - 1000, policy: { budget: "1.50", take_profit: "0", stop_loss: "0" }, pending: null },
      { ticker: "KXSOL15M-WIN", side: "no", rule: "Edge", status: "closed", quantity: "2", entry_cost: "1.40", exit_credit: "2.00", net_pnl: "0.60", closed_by: "settled", result: "no", opened_ms: now - 900000, closed_ms: now - 800000, policy: {}, pending: null },
      { ticker: "KXBTC15M-LOSS", side: "yes", rule: "Edge", status: "closed", quantity: "1", entry_cost: "0.70", exit_credit: "0.40", net_pnl: "-0.30", closed_by: "stop_loss", opened_ms: now - 600000, closed_ms: now - 500000, policy: {}, pending: null },
      { ticker: "KXBTC15M-SKIP", side: "yes", rule: "Edge", status: "skipped", quantity: "0", entry_cost: "0", exit_credit: "0", net_pnl: null, policy: {}, pending: null },
    ];
    state.paper.events = [
      { id: 1, ts_ms: 1000, action: "buy_submitted", reason: "Rule Edge", ticker: "KXBTC15M-RUN" },
      { id: 2, ts_ms: 3000, action: "enabled", reason: "Auto trading enabled" },
    ];
    state.paper.decisions = [{ ticker: "KXBTC15M-RUN", ts_ms: 1500, recommendation: "BUY_YES", confidence: 0.8, confirmation_detail: JSON.stringify([{ name: "momentum", agree: true }]), result: null, close_ts_ms: now }];
    render(<Trading />);
    const card = await screen.findByRole("article", { name: "Trade KXBTC15M-RUN" });
    expect(within(card).getByText("Bet UP")).toBeTruthy();
    expect(within(card).getByText("Running")).toBeTruthy();
    expect(within(card).getByText("$1.40")).toBeTruthy();
    expect(within(card).getByText("70¢")).toBeTruthy();
    expect(within(card).getByText("$2.00")).toBeTruthy();
    expect(within(card).getByText("+$0.20")).toBeTruthy();
    expect(within(card).getByText(/^2:0[45]$/)).toBeTruthy();
    expect(within(card).getByText(/Hold until the market ends/)).toBeTruthy();
    expect(within(card).getByText("Placed a bet")).toBeTruthy();
    expect(within(card).getByText("it would go UP")).toBeTruthy();
    const finished = screen.getByRole("region", { name: "Finished bets" });
    const rows = within(finished).getAllByRole("listitem");
    expect(rows.map((row) => row.getAttribute("aria-label"))).toEqual(["Finished trade KXBTC15M-LOSS", "Finished trade KXSOL15M-WIN"]);
    expect(within(rows[0]).getByText("Lost")).toBeTruthy();
    expect(within(rows[0]).getByText("Sold to cut losses")).toBeTruthy();
    expect(within(rows[0]).getByText("-$0.30")).toBeTruthy();
    expect(within(rows[1]).getByText("Won")).toBeTruthy();
    expect(within(rows[1]).getByText("Market ended")).toBeTruthy();
    expect(within(rows[1]).getByText("Bet DOWN", { exact: false })).toBeTruthy();
    expect(screen.queryByRole("article", { name: "Trade KXBTC15M-SKIP" })).toBeNull();
    const score = screen.getByRole("region", { name: "Scoreboard" });
    expect(within(score).getByText("+$0.30")).toBeTruthy();
    expect(within(score).getByText("50% (1 of 2)")).toBeTruthy();
    const diary = screen.getByRole("region", { name: "Bot diary" });
    expect(within(diary).getByText("Bot diary (2)")).toBeTruthy();
    expect(within(diary).getByText("Bot turned on")).toBeTruthy();
  });
});
