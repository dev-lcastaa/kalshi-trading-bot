import React from "react";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Trading, { friendlyStatus, fromUi, toUi, tradingErrorMessage, validateSettings } from "./Trading";
import { scalpForm, scalpSettings, validateScalp } from "./ScalpControls";

const RULE = { name: "Edge", enabled: true, coin: "ANY", side: "model", min_price: "0.50", max_price: "0.95", min_confidence: "0.50", min_edge: "0.00", min_seconds_left: 330, max_seconds_left: 390, budget: "1.00", take_profit: "0.50", stop_loss: "0.10", max_entries: 1, reentry_gap_sec: 60 };
const SETTINGS = { rules: [RULE] };
const withRule = (overrides) => ({ rules: [{ ...RULE, ...overrides }] });
const uiWith = (overrides) => ({ rules: [{ ...toUi(SETTINGS).rules[0], ...overrides }] });
const CURRENT = scalpSettings({ confidence: "65", budget: "1.00", stop_loss: "0.20" });
const rule = () => within(screen.getByRole("group", { name: "Scalping settings" }));
const SPEND = "Amount per trade ($)";
const makeSnapshot = (mode, overrides = {}) => ({ mode, settings: structuredClone(CURRENT), watch: [], enabled: false, environment: "demo", blockers: [], last_cycle_ms: null, error: null, positions: [], events: [], decisions: [], ...overrides });
let state;
let fail;
let malformed;
let sockets;
beforeEach(() => {
  sockets = [];
  vi.spyOn(globalThis, "WebSocket").mockImplementation(function (url) {
    this.url = url;
    this.close = vi.fn(() => this.onclose?.());
    sockets.push(this);
  });
  state = { live: makeSnapshot("live"), paper: makeSnapshot("paper") };
  fail = false; malformed = false;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
    if (fail) return { ok: false, json: async () => ({ detail: "Control rejected" }) };
    if (malformed) return { ok: true, json: async () => ({ live: structuredClone(state.live) }) };
    if (options?.method === "PUT") { const body = JSON.parse(options.body); state[body.mode].settings = { rules: body.rules, ...(body.risk ? { risk: body.risk } : {}) }; }
    if (options?.method === "POST") { const body = JSON.parse(options.body); state[body.mode].enabled = body.enabled; }
    return { ok: true, json: async () => structuredClone(state) };
  });
});

const lastWrite = (method) => fetch.mock.calls.filter(([, options]) => options?.method === method).at(-1);
const goLive = (user) => user.click(screen.getByRole("radio", { name: "Bot Real Trading" }));

describe("Trading tab", () => {
  it("saves a custom per-market trade limit and preserves it across reloads and modes", async () => {
    const user = userEvent.setup(); render(<Trading />);
    const limit = await screen.findByLabelText("Trades per 15-minute market");
    expect(limit.value).toBe("3");
    await user.clear(limit); await user.type(limit, "5");
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    await screen.findByText("Scalping settings saved.");
    expect(state.paper.settings.rules[0]).toMatchObject({ max_cycles: 5, market_spend_limit: "5.00" });
    expect(state.paper.settings.risk.daily_loss_limit).toBe("3.00");
    expect(state.paper.enabled).toBe(false);
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    expect(limit.value).toBe("5");
    await goLive(user);
    expect(screen.getByLabelText("Trades per 15-minute market").value).toBe("3");
    await user.click(screen.getByRole("radio", { name: "Bot Simulation trading" }));
    expect(screen.getByLabelText("Trades per 15-minute market").value).toBe("5");
    expect(scalpForm(state.paper.settings).max_cycles).toBe("5");
    expect(scalpSettings({ ...scalpForm(state.paper.settings), budget: "10.00" }).rules[0].market_spend_limit).toBe("25.00");
  });
  it("validates trade counts from 1 to 10 and retains the default for older forms", () => {
    const form = { confidence: "65", budget: "1.00", stop_loss: "0.20" };
    expect(scalpSettings(form).rules[0].max_cycles).toBe(3);
    for (const value of ["1", "10"]) expect(validateScalp({ ...form, max_cycles: value })).toBe("");
    for (const value of ["", "0", "11", "-1", "1.5", "NaN"]) {
      expect(validateScalp({ ...form, max_cycles: value })).toContain("whole number from 1 to 10");
    }
  });
  it("omits execution costs and the last-check paragraph even when telemetry exists", async () => {
    state.paper.execution = { filled_orders: 2, orders: 2, entry_fees: "0.02",
      settlement_comparison: { measured_bets: 0, predicted_net: "0", realized_net: "0" } };
    render(<Trading />);
    await screen.findByRole("group", { name: "Scalping settings" });
    expect(screen.queryByRole("region", { name: "Execution costs" })).toBeNull();
    expect(screen.queryByText(/Last market check:/)).toBeNull();
    expect(screen.getByText("Amount includes buy fees. Profit target: $0.02 after fees. Stop loss triggers a sale, but losses can exceed it.")).toBeTruthy();
  });
  it("shows structured API validation errors and keeps failed settings unsaved", async () => {
    const user = userEvent.setup(); render(<Trading />);
    const amount = await screen.findByLabelText(SPEND);
    await user.clear(amount); await user.type(amount, "1.50");
    fetch.mockImplementationOnce(async () => ({
      ok: false, status: 422, json: async () => ({ detail: [{
        type: "literal_error", loc: ["body", "rules", 0, "side"],
        msg: "Input should be 'model', 'yes' or 'no'", input: "momentum",
      }] }),
    }));
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    expect(await screen.findByText("rules.0.side: Input should be 'model', 'yes' or 'no'")).toBeTruthy();
    expect(screen.queryByText("[object Object]")).toBeNull();
    expect(screen.queryByText("Scalping settings saved.")).toBeNull();
    expect(state.paper.settings).toEqual(CURRENT);
    expect(screen.getByRole("switch").checked).toBe(false);
    expect(amount.value).toBe("1.50");
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Save settings" }).disabled).toBe(false));
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    await screen.findByText("Scalping settings saved.");
    expect(state.paper.settings.rules[0].budget).toBe("1.50");
  });
  it("formats string and multi-field validation errors without object coercion", () => {
    expect(tradingErrorMessage("Pause trading before changing settings", 422)).toBe("Pause trading before changing settings");
    expect(tradingErrorMessage([
      { loc: ["body", "rules", 0, "side"], msg: "Invalid side" },
      { loc: ["body", "risk", "max_spread"], msg: "Invalid spread" },
    ], 422)).toBe("rules.0.side: Invalid side; risk.max_spread: Invalid spread");
    expect(tradingErrorMessage({ invalid: true }, 500)).toBe("Request failed (500)");
    expect(tradingErrorMessage([], 422)).toBe("Request failed (422)");
  });
  it("replaces legacy complexity only after saving, without enabling or touching the other mode", async () => {
    state.paper.settings = structuredClone(SETTINGS);
    const user = userEvent.setup(); render(<Trading />);
    await screen.findByRole("group", { name: "Scalping settings" });
    expect(rule().getByLabelText(SPEND).value).toBe("1.00");
    expect(rule().getAllByRole("textbox")).toHaveLength(4);
    expect(screen.queryByRole("button", { name: "Add a rule" })).toBeNull();
    expect(screen.queryByLabelText("Minimum expected profit (¢)")).toBeNull();
    expect(screen.getByRole("switch").disabled).toBe(true);
    expect(lastWrite("PUT")).toBeUndefined();
    expect(lastWrite("POST")).toBeUndefined();
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    await screen.findByText("Scalping settings saved.");
    const saved = JSON.parse(lastWrite("PUT")[1].body);
    expect(saved.mode).toBe("paper");
    expect(saved.rules).toHaveLength(1);
    expect(saved.rules[0]).toMatchObject({ side: "momentum", min_confidence: "0.65", min_edge: null,
      budget: "1.00", stop_loss: "0.10", take_profit: "0.02", scalping: true });
    expect(state.live.settings).toEqual(CURRENT);
    expect(state.live.enabled).toBe(false);
    expect(state.paper.enabled).toBe(false);
    expect(screen.getByRole("switch").disabled).toBe(false);
  });
  it("validates the trading controls and blocks saving invalid values", async () => {
    const user = userEvent.setup(); render(<Trading />);
    const field = await screen.findByLabelText("Stop loss amount ($)");
    await user.clear(field); await user.type(field, "1.00");
    expect(screen.getByRole("button", { name: "Save settings" }).disabled).toBe(true);
    expect(screen.getByRole("alert").textContent).toContain("less than");
    const form = { confidence: "65", budget: "1.00", stop_loss: "0.20" };
    expect(validateScalp(form)).toBe("");
    for (const bad of [{ confidence: "49" }, { confidence: "100" }, { confidence: "65.5" },
      { budget: "25.01" }, { budget: "NaN" }, { budget: "0" }, { stop_loss: "0" },
      { stop_loss: "1.00" }, { stop_loss: "0.001" }]) {
      expect(validateScalp({ ...form, ...bad })).not.toBe("");
    }
  });
  it("automatically derives protected exits and spending caps from the controls", async () => {
    const user = userEvent.setup(); render(<Trading />);
    const amount = await screen.findByLabelText(SPEND);
    await user.clear(amount); await user.type(amount, "10");
    expect(lastWrite("PUT")).toBeUndefined();
    expect(lastWrite("POST")).toBeUndefined();
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    await screen.findByText("Scalping settings saved.");
    const saved = JSON.parse(lastWrite("PUT")[1].body);
    expect(saved.rules[0]).toMatchObject({ scalping: true, max_cycles: 3, cycle_cooldown_sec: 30,
      market_spend_limit: "25.00", market_loss_limit: "0.20", max_entries: 1, min_edge: null,
      take_profit: "0.02", stop_loss: "0.20", min_seconds_left: 90, max_seconds_left: 840 });
    expect(saved.risk).toMatchObject({ min_edge: "0.00", max_spread: "0.03",
      max_open_positions: 2, max_open_cost: "20.00", daily_loss_limit: "30.00" });
    expect(state.live.enabled).toBe(false);
    expect(state.paper.enabled).toBe(false);
  });
  it("uses server totals and isolates the activity for each scalp cycle", async () => {
    const now = Date.now();
    const scalp = { max_cycles: 3, cycle_cooldown_sec: 30, market_spend_limit: "3.00", market_loss_limit: "0.50" };
    state.paper.summary = { running: 1, finished: 120, wins: 100, net_pnl: "2.40" };
    state.paper.positions = [
      { ticker: "KXBTC15M-CYCLES", position_id: "old", cycle_number: 1, scalp, status: "closed", side: "yes", quantity: "0", entry_cost: "0.52", exit_credit: "0.55", net_pnl: "0.03", closed_ms: now - 10000, policy: {}, closed_by: "take_profit" },
      { ticker: "KXBTC15M-CYCLES", position_id: "new", cycle_number: 2, scalp, status: "open", side: "no", quantity: "1", entry_cost: "0.52", net_pnl: "0.01", policy: {}, close_ts_ms: now + 600000 },
    ];
    state.paper.events = [
      { id: "old", ticker: "KXBTC15M-CYCLES", position_id: "old", action: "filled", reason: "Prior cycle fill", ts_ms: now - 1000 },
      { id: "new", ticker: "KXBTC15M-CYCLES", position_id: "new", action: "filled", reason: "Current cycle fill", ts_ms: now },
    ];
    render(<Trading />);
    const live = await screen.findByRole("article", { name: "Trade KXBTC15M-CYCLES" });
    expect(within(live).getByText("2/3")).toBeTruthy();
    expect(within(live).getByText("Current cycle fill")).toBeTruthy();
    expect(within(live).queryByText("Prior cycle fill")).toBeNull();
    const score = screen.getByRole("region", { name: "Scoreboard" });
    expect(within(score).getByText("120")).toBeTruthy();
    expect(within(score).getByText("+$2.40")).toBeTruthy();
    expect(within(score).getByText("83% (100 of 120)")).toBeTruthy();
  });
  it("shows confidence for the moving entry side instead of the opposite model edge", async () => {
    state.paper.watch = [{ ticker: "KXBTC15M-MOVE", seconds_left: 600,
      model_p_yes: 0.61, market_p_yes: 0.635, strategy: "momentum", side: "yes", price: "0.64",
      entry_confidence: "0.635", rule: null,
      status: "No match - Momentum scalp: market implies YES 0.635 < 0.65" }];
    render(<Trading />);
    const card = await screen.findByRole("article", { name: "Watching KXBTC15M-MOVE" });
    expect(within(card).getByText("Watching UP at 64¢")).toBeTruthy();
    expect(within(card).getByText("Momentum entry: UP · Market confidence 63.5%")).toBeTruthy();
    expect(within(card).getByText(/UP market confidence is 63.5%, you want 65%/)).toBeTruthy();
    expect(within(card).queryByText(/bot is 39%/)).toBeNull();
  });
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
  it("shows live opposite leans separately from the rule's entry side", async () => {
    state.paper.watch = [{ ticker: "KXBTC15M-LEAN", seconds_left: 360, model_p_yes: 0.2, market_p_yes: 0.7, side: "yes", price: "0.15", rule: "Edge", status: "Matches 'Edge'" }];
    render(<Trading />);
    const card = await screen.findByRole("article", { name: "Watching KXBTC15M-LEAN" });
    expect(within(card).getByRole("img", { name: "Bot leans: down at 80.0%" })).toBeTruthy();
    expect(within(card).getByRole("img", { name: "Market leans: up at 70.0%" })).toBeTruthy();
    expect(within(card).getByText("UP at 15¢")).toBeTruthy();
    state.paper.watch[0].model_p_yes = 0.9;
    await act(async () => sockets[0].onmessage({ data: JSON.stringify({ type: "trading_state", data: state }) }));
    expect(within(card).getByRole("img", { name: "Bot leans: up at 90.0%" })).toBeTruthy();
    expect(within(card).queryByRole("img", { name: "Bot leans: down at 80.0%" })).toBeNull();
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("starts on practice mode and saves a single automatic momentum strategy", async () => {
    state.paper.watch = [{ ticker: "KXBTC15M-A", seconds_left: 360, model_p_yes: 0.84, market_p_yes: 0.8, side: "yes", price: "0.80", rule: "Edge", status: "Matches 'Edge'" }];
    const user = userEvent.setup(); render(<Trading />);
    const watch = await screen.findByRole("region", { name: "Markets the bot is watching" });
    expect(screen.getByRole("radio", { name: "Bot Simulation trading" }).checked).toBe(true);
    expect(within(watch).getByText("BTC")).toBeTruthy();
    expect(within(watch).getByText("UP at 80¢")).toBeTruthy();
    expect(within(watch).getByText('Preview only — matches "Edge"')).toBeTruthy();
    expect(rule().getByLabelText("Confidence level (%)").value).toBe("65");
    await user.clear(rule().getByLabelText(SPEND)); await user.type(rule().getByLabelText(SPEND), "20");
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    await screen.findByText("Scalping settings saved.");
    const body = JSON.parse(lastWrite("PUT")[1].body);
    expect(body.mode).toBe("paper");
    expect(body.rules).toHaveLength(1);
    expect(body.rules[0]).toMatchObject({ coin: "ANY", side: "momentum", budget: "20.00",
      min_edge: null, take_profit: "0.02", stop_loss: "0.20" });
  });
  it("keeps rules and unsaved changes separate per mode and resets after save", async () => {
    const user = userEvent.setup(); render(<Trading />);
    await screen.findByRole("group", { name: "Scalping settings" });
    await goLive(user);
    expect(screen.getByText("Real-money mode — Kalshi demo account (still not real money)")).toBeTruthy();
    const budget = rule().getByLabelText(SPEND);
    await user.clear(budget); await user.type(budget, "1.75");
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    expect(rule().getByLabelText(SPEND).value).toBe("1.75");
    await user.click(screen.getByRole("radio", { name: "Bot Simulation trading" }));
    expect(screen.getByText("Practice mode — pretend money, nothing real is spent")).toBeTruthy();
    const paperBudget = rule().getByLabelText(SPEND);
    expect(paperBudget.value).toBe("1.00");
    await user.clear(paperBudget); await user.type(paperBudget, "1.50");
    await goLive(user);
    expect(rule().getByLabelText(SPEND).value).toBe("1.75");
    expect(screen.getByText("Save your scalping settings before turning the bot on.")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    await screen.findByText("Scalping settings saved.");
    const [, put] = lastWrite("PUT");
    expect(JSON.parse(put.body)).toEqual({ mode: "live", ...scalpSettings({ confidence: "65", budget: "1.75", stop_loss: "0.20" }) });
    expect(put.headers.Authorization).toBeUndefined();
    expect(state.live.settings.rules[0].budget).toBe("1.75");
    expect(state.paper.settings.rules[0].budget).toBe("1.00");
    expect(screen.getByText("All changes saved")).toBeTruthy();
    await user.click(screen.getByRole("radio", { name: "Bot Simulation trading" }));
    expect(rule().getByLabelText(SPEND).value).toBe("1.50");
    expect(screen.getByText("You have unsaved changes")).toBeTruthy();
  });
  it("turns practice on instantly without a dialog", async () => {
    const user = userEvent.setup(); render(<Trading />);
    await screen.findByRole("group", { name: "Scalping settings" });
    expect(screen.getByText("The bot is OFF")).toBeTruthy();
    await user.click(screen.getByRole("switch"));
    await screen.findByText("Practice bot turned on.");
    expect(screen.queryByRole("dialog")).toBeNull();
    const [, post] = lastWrite("POST");
    expect(JSON.parse(post.body)).toEqual({ mode: "paper", enabled: true, confirm: true, settings: CURRENT });
    expect(post.headers.Authorization).toBeUndefined();
    expect(screen.getByRole("switch").checked).toBe(true);
    expect(screen.getByText("The bot is ON")).toBeTruthy();
    expect(screen.getByText("Turn the bot off to change settings.")).toBeTruthy();
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
    expect(within(dialog).getByText("Momentum scalp")).toBeTruthy();
    expect(within(dialog).getByRole("button", { name: "Yes, turn it on" }).disabled).toBe(true);
    await user.click(within(dialog).getByRole("checkbox"));
    await user.click(within(dialog).getByRole("button", { name: "Yes, turn it on" }));
    await screen.findByText("Real-money bot turned on.");
    expect(JSON.parse(lastWrite("POST")[1].body)).toEqual({ mode: "live", enabled: true, confirm: true, settings: CURRENT });
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
  it("ticks the countdown without polling and cleans up the live connection", async () => {
    vi.useFakeTimers();
    try {
      state.paper.watch = [{ ticker: "KXBTC15M-A", seconds_left: 117, close_ts_ms: Date.now() + 117000, model_p_yes: 0.71, market_p_yes: 0.7, status: "watching" }];
      const view = render(<Trading />);
      await act(async () => {});
      expect(fetch).toHaveBeenCalledTimes(1);
      const card = screen.getByRole("article", { name: "Watching KXBTC15M-A" });
      expect(within(card).getByText("1:57")).toBeTruthy();
      await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
      expect(within(card).getByText("1:51")).toBeTruthy();
      expect(fetch).toHaveBeenCalledTimes(1);
      expect(sockets[0].url).toMatch(/\/ws\/trading$/);
      view.unmount();
      expect(sockets[0].close).toHaveBeenCalledOnce();
      await vi.advanceTimersByTimeAsync(6000);
      expect(fetch).toHaveBeenCalledTimes(1);
    } finally { vi.useRealTimers(); }
  });
  it("pushes changed probabilities and new markets without overwriting unsaved rules", async () => {
    const user = userEvent.setup(); render(<Trading />);
    await screen.findByRole("group", { name: "Scalping settings" });
    await user.clear(rule().getByLabelText(SPEND)); await user.type(rule().getByLabelText(SPEND), "1.75");
    state.paper.watch = [{ ticker: "KXSOL15M-NEXT", seconds_left: 900, close_ts_ms: Date.now() + 900000, model_p_yes: 0.62, market_p_yes: 0.51, status: "watching" }];
    const reads = fetch.mock.calls.length;
    await act(async () => sockets[0].onmessage({ data: JSON.stringify({ type: "trading_state", data: state }) }));
    const card = screen.getByRole("article", { name: "Watching KXSOL15M-NEXT" });
    expect(within(card).getByRole("img", { name: "Bot leans: up at 62.0%" })).toBeTruthy();
    expect(within(card).getByRole("img", { name: "Market leans: up at 51.0%" })).toBeTruthy();
    expect(screen.getByText("Live · 1 market")).toBeTruthy();
    expect(rule().getByLabelText(SPEND).value).toBe("1.75");
    expect(fetch).toHaveBeenCalledTimes(reads);
    state.paper.watch[0].model_p_yes = 0.73;
    await act(async () => sockets[0].onmessage({ data: JSON.stringify({ type: "trading_state", data: state }) }));
    expect(within(card).getByRole("img", { name: "Bot leans: up at 73.0%" })).toBeTruthy();
  });
  it("marks disconnected updates stale and reconnects with a fresh snapshot", async () => {
    vi.useFakeTimers();
    try {
      render(<Trading />);
      await act(async () => {});
      await act(async () => sockets[0].onmessage({ data: JSON.stringify({ type: "trading_state", data: state }) }));
      await act(async () => sockets[0].onclose());
      expect(screen.getByRole("switch").disabled).toBe(true);
      expect(screen.getByText(/Live updates disconnected/)).toBeTruthy();
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
      expect(sockets).toHaveLength(2);
      await act(async () => sockets[1].onmessage({ data: JSON.stringify({ type: "trading_state", data: state }) }));
      expect(screen.getByRole("switch").disabled).toBe(false);
      expect(screen.queryByText(/Live updates disconnected/)).toBeNull();
      expect(fetch).toHaveBeenCalledTimes(1);
    } finally { vi.useRealTimers(); }
  });
  it("rejects malformed pushed data and detects a stalled connection", async () => {
    vi.useFakeTimers();
    try {
      render(<Trading />);
      await act(async () => {});
      await act(async () => sockets[0].onmessage({ data: JSON.stringify({ type: "trading_state", data: { paper: state.paper } }) }));
      expect(screen.getByRole("switch").disabled).toBe(true);
      expect(screen.getByText(/Couldn't read live updates/)).toBeTruthy();
      expect(sockets[0].close).toHaveBeenCalledOnce();
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
      await act(async () => sockets[1].onmessage({ data: JSON.stringify({ type: "trading_state", data: state }) }));
      expect(screen.getByRole("switch").disabled).toBe(false);
      await act(async () => { await vi.advanceTimersByTimeAsync(16000); });
      expect(screen.getByRole("switch").disabled).toBe(true);
      expect(sockets[1].close).toHaveBeenCalledOnce();
    } finally { vi.useRealTimers(); }
  });
  it("does not let an older HTTP response replace a newer pushed snapshot", async () => {
    let complete;
    fetch.mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }));
    render(<Trading />);
    const older = structuredClone(state);
    state.paper.watch = [{ ticker: "KXBTC15M-NEW", seconds_left: 600, model_p_yes: 0.77, status: "watching" }];
    await act(async () => sockets[0].onmessage({ data: JSON.stringify({ type: "trading_state", data: state }) }));
    await act(async () => complete({ ok: true, json: async () => older }));
    expect(screen.getByRole("article", { name: "Watching KXBTC15M-NEW" })).toBeTruthy();
    expect(screen.getByRole("img", { name: "Bot leans: up at 77.0%" })).toBeTruthy();
  });
  it("does not flip the switch before the server answers", async () => {
    const user = userEvent.setup(); render(<Trading />);
    await screen.findByRole("group", { name: "Scalping settings" });
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
    expect(within(rows[0]).getByText("Net profit / loss")).toBeTruthy();
    expect(within(rows[0]).getByText("$0.70")).toBeTruthy();
    expect(within(rows[0]).getByText("$0.40")).toBeTruthy();
    expect(within(rows[0]).getByText("Edge")).toBeTruthy();
    expect(rows[0].querySelector("time").dateTime).toBe(new Date(now - 500000).toISOString());
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
  it("shows neutral finished cards for break-even and unknown results without inventing a profit", async () => {
    state.paper.positions = [
      { ticker: "KXBTC15M-EVEN", side: "yes", status: "closed", entry_cost: "1.00", exit_credit: "1.00", net_pnl: "0.00", closed_by: "take_profit", closed_ms: 2000, quantity: "2", rule: "Long rule name ".repeat(10) },
      { ticker: "KXSOL15M-UNKNOWN", side: "no", status: "closed", net_pnl: null },
    ];
    render(<Trading />);
    const even = await screen.findByRole("listitem", { name: "Finished trade KXBTC15M-EVEN" });
    expect(within(even).getByText("Broke even")).toBeTruthy();
    expect(within(even).getByText("$0.00")).toBeTruthy();
    expect(within(even).getByText("Cashed out early")).toBeTruthy();
    expect(even.classList.contains("win")).toBe(false);
    expect(even.classList.contains("loss")).toBe(false);
    const unknown = screen.getByRole("listitem", { name: "Finished trade KXSOL15M-UNKNOWN" });
    expect(within(unknown).getAllByText("Closed")).toHaveLength(2);
    expect(unknown.querySelector(".finished-result strong").textContent).toBe("--");
    expect(unknown.querySelector("time").hasAttribute("datetime")).toBe(false);
    expect(within(unknown).queryByText("$0.00")).toBeNull();
  });
  it("shows the newest two, loads two at a time, collapses, and resets when switching modes", async () => {
    const user = userEvent.setup();
    state.paper.positions = Array.from({ length: 5 }, (_, index) => ({
      ticker: `KXBTC15M-${index}`, status: "closed", side: "yes", closed_ms: index + 1,
      net_pnl: "0.10", entry_cost: "1.00", exit_credit: "1.10",
    }));
    state.live.positions = structuredClone(state.paper.positions);
    render(<Trading />);
    const finished = await screen.findByRole("region", { name: "Finished bets" });
    const visible = () => within(finished).getAllByRole("listitem").map((card) => card.getAttribute("aria-label"));
    expect(visible()).toEqual(["Finished trade KXBTC15M-4", "Finished trade KXBTC15M-3"]);
    expect(within(screen.getByRole("region", { name: "Scoreboard" })).getByText("+$0.50")).toBeTruthy();
    await user.click(within(finished).getByRole("button", { name: "Load more" }));
    expect(visible()).toHaveLength(4);
    await user.click(within(finished).getByRole("button", { name: "Load more" }));
    expect(visible()).toHaveLength(5);
    expect(within(finished).queryByRole("button", { name: "Load more" })).toBeNull();
    await user.click(within(finished).getByRole("button", { name: "Show fewer" }));
    expect(visible()).toHaveLength(2);
    await user.click(within(finished).getByRole("button", { name: "Load more" }));
    await goLive(user);
    expect(visible()).toHaveLength(2);
    await user.click(screen.getByRole("radio", { name: "Bot Simulation trading" }));
    expect(visible()).toHaveLength(2);
    state.paper.positions.push({ ...state.paper.positions[0], ticker: "KXBTC15M-NEW", closed_ms: 100 });
    await act(async () => sockets[0].onmessage({ data: JSON.stringify({ type: "trading_state", data: state }) }));
    expect(visible()).toEqual(["Finished trade KXBTC15M-NEW", "Finished trade KXBTC15M-4"]);
  });
});
