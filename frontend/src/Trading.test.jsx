import React from "react";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Trading, { validateSettings } from "./Trading";

const SETTINGS = { budget: "1.00", take_profit: "0.50", stop_loss: "0.10" };
const makeSnapshot = (mode, overrides = {}) => ({ mode, settings: { ...SETTINGS }, enabled: false, environment: "demo", blockers: [], last_cycle_ms: null, error: null, positions: [], events: [], decisions: [], ...overrides });
let state;
let fail;
let malformed;
beforeEach(() => {
  state = { live: makeSnapshot("live"), paper: makeSnapshot("paper") };
  fail = false; malformed = false;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
    if (fail) return { ok: false, json: async () => ({ detail: "Control rejected" }) };
    if (malformed) return { ok: true, json: async () => ({ live: structuredClone(state.live) }) };
    if (options?.method === "PUT") { const body = JSON.parse(options.body); state[body.mode].settings = { budget: body.budget, take_profit: body.take_profit, stop_loss: body.stop_loss }; }
    if (options?.method === "POST") { const body = JSON.parse(options.body); state[body.mode].enabled = body.enabled; }
    return { ok: true, json: async () => structuredClone(state) };
  });
});

const lastWrite = (method) => fetch.mock.calls.filter(([, options]) => options?.method === method).at(-1);

describe("Trading safety", () => {
  it("validates cent amounts and budget limits", () => {
    expect(validateSettings(SETTINGS)).toBe("");
    for (const settings of [{ ...SETTINGS, budget: "2.00" }, { ...SETTINGS, stop_loss: "1.00" }, { ...SETTINGS, take_profit: "0.001" }, { ...SETTINGS, budget: "0" }]) expect(validateSettings(settings)).not.toBe("");
  });
  it("keeps settings and dirty state independent per mode and resets after save", async () => {
    const user = userEvent.setup(); render(<Trading />);
    const budget = await screen.findByLabelText("Budget ($)");
    await user.clear(budget); await user.type(budget, "1.75");
    await user.click(screen.getByRole("button", { name: "Refresh trading" }));
    expect(screen.getByLabelText("Budget ($)").value).toBe("1.75");
    await user.click(screen.getByRole("radio", { name: "Paper auto trading" }));
    expect(screen.getByText("Paper trading — simulated, no real money")).toBeTruthy();
    const paperBudget = screen.getByLabelText("Budget ($)");
    expect(paperBudget.value).toBe("1.00");
    await user.clear(paperBudget); await user.type(paperBudget, "1.50");
    await user.click(screen.getByRole("radio", { name: "Live auto trading" }));
    expect(screen.getByText("Live trading — demo account")).toBeTruthy();
    expect(screen.getByLabelText("Budget ($)").value).toBe("1.75");
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    await screen.findByText("Settings saved.");
    const [, put] = lastWrite("PUT");
    expect(JSON.parse(put.body)).toEqual({ mode: "live", budget: "1.75", take_profit: "0.50", stop_loss: "0.10" });
    expect(put.headers.Authorization).toBeUndefined();
    expect(state.live.settings.budget).toBe("1.75");
    expect(state.paper.settings.budget).toBe("1.00");
    expect(screen.getByText("Saved")).toBeTruthy();
    await user.click(screen.getByRole("radio", { name: "Paper auto trading" }));
    expect(screen.getByLabelText("Budget ($)").value).toBe("1.50");
    expect(screen.getByText("Unsaved changes")).toBeTruthy();
  });
  it("enables paper instantly without a dialog, echoing saved settings with confirm", async () => {
    const user = userEvent.setup(); render(<Trading />);
    await screen.findByLabelText("Budget ($)");
    await user.click(screen.getByRole("radio", { name: "Paper auto trading" }));
    await user.click(screen.getByRole("switch"));
    await screen.findByText("Paper trading enabled.");
    expect(screen.queryByRole("dialog")).toBeNull();
    const [, post] = lastWrite("POST");
    expect(JSON.parse(post.body)).toEqual({ mode: "paper", enabled: true, confirm: true, settings: SETTINGS });
    expect(post.headers.Authorization).toBeUndefined();
    expect(screen.getByRole("switch").checked).toBe(true);
    expect(screen.getByText("Pause trading to change settings.")).toBeTruthy();
    await user.click(screen.getByRole("switch"));
    await screen.findByText("Paper trading disabled.");
    expect(JSON.parse(lastWrite("POST")[1].body)).toEqual({ mode: "paper", enabled: false, confirm: false });
    expect(state.paper.enabled).toBe(false);
    expect(state.live.enabled).toBe(false);
  });
  it("requires the dialog and risk acknowledgment to enable live, then permits off with blockers", async () => {
    const user = userEvent.setup(); render(<Trading />);
    await screen.findByText("No positions");
    await user.click(screen.getByRole("switch"));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Enable live trading?")).toBeTruthy();
    expect(within(dialog).getByText("$1.00")).toBeTruthy();
    expect(within(dialog).getByRole("button", { name: "Confirm enable" }).disabled).toBe(true);
    expect(state.live.enabled).toBe(false);
    await user.click(within(dialog).getByRole("checkbox"));
    await user.click(within(dialog).getByRole("button", { name: "Confirm enable" }));
    await screen.findByText("Live trading enabled.");
    const [, post] = lastWrite("POST");
    expect(JSON.parse(post.body)).toEqual({ mode: "live", enabled: true, confirm: true, settings: SETTINGS });
    expect(post.headers.Authorization).toBeUndefined();
    state.live.blockers = ["Entries blocked"];
    await user.click(screen.getByRole("button", { name: "Refresh trading" }));
    await screen.findByText("Entries blocked");
    await user.click(screen.getByRole("switch"));
    await screen.findByText("Live trading disabled.");
    expect(JSON.parse(lastWrite("POST")[1].body)).toEqual({ mode: "live", enabled: false, confirm: false });
    expect(state.live.enabled).toBe(false);
  });
  it("retains stale data and never reports a failed enable as success", async () => {
    const user = userEvent.setup(); render(<Trading />);
    await screen.findByText("No positions");
    await user.click(screen.getByRole("switch"));
    await user.click(within(screen.getByRole("dialog")).getByRole("checkbox"));
    fail = true;
    await user.click(screen.getByRole("button", { name: "Confirm enable" }));
    await screen.findByText("Control rejected");
    expect(screen.queryByText("Live trading enabled.")).toBeNull();
    expect(screen.getByRole("switch").checked).toBe(false);
    expect(screen.getByRole("switch").disabled).toBe(true);
    await user.click(screen.getByRole("button", { name: "Refresh trading" }));
    await waitFor(() => expect(screen.getByText(/Last data retained/)).toBeTruthy());
    expect(screen.getByText("No positions")).toBeTruthy();
  });
  it("treats a response missing a mode as an error and retains stale data", async () => {
    const user = userEvent.setup(); render(<Trading />);
    await screen.findByText("No positions");
    malformed = true;
    await user.click(screen.getByRole("button", { name: "Refresh trading" }));
    await screen.findByText(/Trading status unavailable/);
    expect(screen.getByText(/Last data retained/)).toBeTruthy();
    expect(screen.getByText("No positions")).toBeTruthy();
    expect(screen.getByRole("switch").disabled).toBe(true);
    malformed = false;
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByRole("switch").disabled).toBe(false));
  });
  it("shows initial loading and retries an unknown status", async () => {
    fetch.mockImplementationOnce(() => new Promise(() => {}));
    const user = userEvent.setup(); render(<Trading />);
    expect(screen.getByText("Loading trading status...")).toBeTruthy();
    expect(screen.queryByRole("switch")).toBeNull();
    fail = true;
    await user.click(screen.getByRole("button", { name: "Refresh trading" }));
    await screen.findByText(/Status unknown\./);
    fail = false;
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText("No positions");
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
  it("does not optimistically enable while the server response is pending", async () => {
    const user = userEvent.setup(); render(<Trading />);
    await screen.findByLabelText("Budget ($)");
    await user.click(screen.getByRole("radio", { name: "Paper auto trading" }));
    let complete;
    fetch.mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }));
    await user.click(screen.getByRole("switch"));
    expect(screen.getByRole("switch").checked).toBe(false);
    expect(screen.queryByText("Paper trading enabled.")).toBeNull();
    const next = { live: structuredClone(state.live), paper: { ...structuredClone(state.paper), enabled: true } };
    await act(async () => { complete({ ok: true, json: async () => next }); });
    expect(screen.getByRole("switch").checked).toBe(true);
    await screen.findByText("Paper trading enabled.");
  });
  it("keeps off available with dirty settings, blockers, and stale status", async () => {
    const user = userEvent.setup(); render(<Trading />);
    const budget = await screen.findByLabelText("Budget ($)");
    state.live.enabled = true; state.live.blockers = ["No new entries"];
    await user.click(screen.getByRole("button", { name: "Refresh trading" }));
    await screen.findByText("No new entries");
    fail = true;
    await user.click(screen.getByRole("button", { name: "Refresh trading" }));
    await screen.findByText(/Last data retained/);
    expect(screen.getByRole("switch").disabled).toBe(false);
    fail = false;
    await user.click(screen.getByRole("switch"));
    await screen.findByText("Live trading disabled.");
    expect(JSON.parse(lastWrite("POST")[1].body)).toEqual({ mode: "live", enabled: false, confirm: false });
    expect(state.live.enabled).toBe(false);
  });
  it("renders each position card with its own actions and latest decision", async () => {
    state.live.positions = [{ ticker: "T1", side: "yes", status: "open", quantity: "2", entry_cost: "0.92", exit_credit: "0", net_pnl: null, policy: { ...SETTINGS }, pending: { client_order_id: "c1", action: "buy", order_id: "o1" } }];
    state.live.events = [
      { id: 1, ts_ms: 1000, action: "buy", reason: "Older action", ticker: "T1" },
      { id: 2, ts_ms: 2000, action: "sell", reason: "Newer action", ticker: "T1" },
      { id: 3, ts_ms: 3000, action: "enabled", reason: "Mode-level event" },
    ];
    state.live.decisions = [
      { ticker: "T1", ts_ms: 900, recommendation: "BUY_NO", confidence: 0.4, confirmation_detail: "[]", result: null, close_ts_ms: 2000 },
      { ticker: "T1", ts_ms: 1500, recommendation: "BUY_YES", confidence: 0.8, confirmation_detail: JSON.stringify([{ name: "momentum", agree: true }]), result: null, close_ts_ms: 2000 },
      { ticker: "T2", ts_ms: 1600, recommendation: "NO_EDGE", confidence: 0.6, confirmation_detail: "[]", result: "yes", close_ts_ms: 2600 },
    ];
    render(<Trading />);
    const card = await screen.findByRole("article", { name: "Position T1" });
    expect(within(card).getAllByText("T1").length).toBeGreaterThan(0);
    expect(within(card).getByText("YES / open")).toBeTruthy();
    expect(within(card).getByText("$0.92")).toBeTruthy();
    expect(within(card).getByText("c1")).toBeTruthy();
    const reasons = within(card).getAllByText(/action$/).map((node) => node.textContent);
    expect(reasons).toEqual(["Newer action", "Older action"]);
    expect(within(card).queryByText("Mode-level event")).toBeNull();
    expect(within(card).getByText("BUY_YES")).toBeTruthy();
    expect(within(card).queryByText("BUY_NO")).toBeNull();
    expect(within(card).getByText("momentum")).toBeTruthy();
    const activity = screen.getByRole("region", { name: "Activity" });
    expect(within(activity).getByText("Mode-level event")).toBeTruthy();
    expect(within(activity).queryByText("Newer action")).toBeNull();
    const others = screen.getByRole("region", { name: "Other decisions" });
    expect(within(others).getByText("Other decisions (1)")).toBeTruthy();
    expect(within(others).getByText("T2")).toBeTruthy();
    expect(within(others).getByText("NO_EDGE")).toBeTruthy();
  });
});