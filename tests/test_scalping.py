import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from kalshi_bot.auto_trader import AutoTrader, can_start_cycle, market_totals
from kalshi_bot.dashboard.server import create_app
from kalshi_bot.data.store import Store
from kalshi_bot.trading import parse_rules
from tests.test_auto_trader import entry_rest, live_market, rules
from tests.test_trading_rules import book, enabled, paper_trader


def scalp_rules(**overrides):
    settings = rules(scalping=True, min_edge="0.03", min_confidence="0.60",
                 min_seconds_left=90, max_seconds_left=840, budget="0.60",
                 take_profit="0.02", stop_loss="0.20", max_entries=1,
                 max_cycles=3, cycle_cooldown_sec=30,
                 market_spend_limit="3.00", market_loss_limit="0.50")
    settings["rules"][0].update(overrides)
    return settings


@pytest.fixture
def clock(monkeypatch):
    now = [time.time()]
    monkeypatch.setattr("kalshi_bot.auto_trader.time.time", lambda: now[0])
    return now


def lock_call(store, ticker, up=True, agree=4, total=4):
    """Record the model's one-shot locked decision, as the prediction loop does at T-6:30."""
    store.record_decision(
        ticker=ticker, ts_ms=int(time.time() * 1000), seconds_to_expiry=390.0, index_price=100.0,
        strike=99.0, model_p_yes=0.8 if up else 0.2, market_p_yes=0.5, edge=0.3,
        recommendation="BUY_YES" if up else "BUY_NO", confidence=0.8,
        confirmation_agree=agree, confirmation_total=total, confirmation_detail="[]",
    )


async def setup_trader(tmp_path, clock, side="yes", settings=None):
    store = Store(str(tmp_path / "scalping.db"))
    lock_call(store, "KXBTC15M-SCALP", up=side == "yes")
    rest = entry_rest()
    rest.get_market_orderbook.return_value = book("0.49", "0.50") if side == "yes" else book("0.50", "0.49")
    feed = [live_market("KXBTC15M-SCALP", model_p_yes=0.8 if side == "yes" else 0.2,
                        yes_bid=0.49 if side == "yes" else 0.50, yes_ask=0.50 if side == "yes" else 0.51,
                        seconds_left=800)]
    trader = await enabled(paper_trader(store, rest, feed), settings or scalp_rules())
    await trader.cycle()
    return store, rest, feed, trader


@pytest.mark.parametrize("bad", [
    {"scalping": "true"}, {"max_cycles": 0}, {"max_cycles": 11}, {"max_cycles": 1.5},
    {"cycle_cooldown_sec": 0}, {"cycle_cooldown_sec": 901},
    {"market_spend_limit": "0"}, {"market_spend_limit": "25.01"},
    {"market_loss_limit": "0"}, {"market_loss_limit": "4"},
    {"market_loss_limit": "NaN"}, {"market_spend_limit": "1.001"},
    {"take_profit": "0"}, {"stop_loss": "0"}, {"min_edge": None},
    {"min_edge": "0"}, {"min_seconds_left": 59}, {"max_seconds_left": 901},
    {"max_entries": 2}, {"budget": "4"}, {"stop_loss": "0.55"},
])
def test_scalping_rejects_unsafe_settings(bad):
    values = scalp_rules()
    values["rules"][0].update(bad)
    with pytest.raises((ValueError, ArithmeticError)):
        parse_rules(values)


def test_rules_round_trip_and_legacy_rules_do_not_opt_in():
    values = scalp_rules()
    rule = parse_rules(values)[0]
    assert rule.scalp.max_cycles == 3
    assert parse_rules({"rules": [rule.to_json()]}) == [rule]
    legacy = parse_rules(rules())[0]
    assert legacy.scalp is None
    assert "scalping" not in legacy.to_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["yes", "no"])
async def test_three_net_profitable_round_trips_keep_separate_history_and_hit_cap(tmp_path, clock, side):
    store, rest, feed, trader = await setup_trader(tmp_path, clock, side)
    for cycle in range(1, 4):
        held = trader.market_history(feed[0]["ticker"])[-1]
        assert held["side"] == side and held["cycle_number"] == cycle
        assert Decimal(held["entry_cost"]) == Decimal("0.52")
        rest.get_market_orderbook.return_value = book("0.57", "0.42") if side == "yes" else book("0.42", "0.57")
        await trader.cycle()
        last = trader.market_history(feed[0]["ticker"])[-1]
        assert last["status"] == "closed" and last["closed_by"] == "take_profit"
        assert Decimal(last["net_pnl"]) == Decimal("0.03")
        assert len(trader.positions()) == cycle
        if cycle < 3:
            assert "cooldown" in trader.watch[0]["status"].lower()
            clock[0] += 29
            await trader.cycle()
            assert len(trader.positions()) == cycle
            clock[0] += 1
            rest.get_market_orderbook.return_value = book("0.49", "0.50") if side == "yes" else book("0.50", "0.49")
            await trader.cycle()
    assert "3/3 cycles used" in trader.watch[0]["status"]
    clock[0] += 60
    await trader.cycle()
    assert len({p["position_id"] for p in trader.positions()}) == 3
    assert market_totals(trader.positions()) == (Decimal("1.56"), Decimal("0"), Decimal("0.09"))
    assert trader.snapshot()["summary"] == {"running": 0, "finished": 3, "wins": 3, "net_pnl": "0.09"}
    assert len({e["position_id"] for e in store.trading_events() if e["action"] == "buy_submitted"}) == 3
    assert (await trader.rest.get_positions(feed[0]["ticker"]))["market_positions"] == []
    store.close()


@pytest.mark.asyncio
async def test_restart_retains_cooldown_and_original_caps_even_after_settings_change(tmp_path, clock):
    store, rest, feed, trader = await setup_trader(tmp_path, clock)
    rest.get_market_orderbook.return_value = book("0.57", "0.42")
    await trader.cycle()
    original_id = trader.positions()[0]["position_id"]
    store.close()
    clock[0] += 5
    store = Store(str(tmp_path / "scalping.db"))
    restarted = paper_trader(store, rest, feed)
    await restarted.cycle()
    assert not restarted.enabled
    values = scalp_rules()
    values["rules"][0].update(max_cycles=10, cycle_cooldown_sec=5, market_spend_limit="25.00")
    await restarted.save_settings(values)
    await restarted.control(True, True)
    await restarted.cycle()
    assert len(restarted.positions()) == 1
    assert "cooldown" in restarted.watch[0]["status"].lower()
    clock[0] += 25
    rest.get_market_orderbook.return_value = book("0.49", "0.50")
    await restarted.cycle()
    assert len(restarted.positions()) == 2
    assert restarted.positions()[0]["position_id"] == original_id
    assert restarted.positions()[1]["scalp"]["max_cycles"] == 3
    assert restarted.positions()[1]["scalp"]["market_spend_limit"] == "3.00"
    store.close()


@pytest.mark.asyncio
async def test_stop_loss_stops_market_even_when_another_rule_is_added(tmp_path, clock):
    store, rest, feed, trader = await setup_trader(tmp_path, clock)
    rest.get_market_orderbook.return_value = book("0.30", "0.69")
    await trader.cycle()
    assert trader.positions()[0]["closed_by"] == "stop_loss"
    assert Decimal(trader.positions()[0]["net_pnl"]) < 0
    await trader.control(False, False)
    values = scalp_rules()
    values["rules"].append({**values["rules"][0], "name": "Bypass"})
    await trader.save_settings(values)
    await trader.control(True, True)
    clock[0] += 30
    rest.get_market_orderbook.return_value = book("0.49", "0.50")
    await trader.cycle()
    assert len(trader.positions()) == 1
    assert "fully closed profitable" in trader.watch[0]["status"]
    store.close()


@pytest.mark.asyncio
async def test_partial_profit_exit_never_starts_another_cycle_until_fully_sold(tmp_path, clock):
    store, rest, feed, trader = await setup_trader(tmp_path, clock)
    rest.get_market_orderbook.side_effect = [book("0.57", "0.42"), book("0.57", "0.42", "0.5")]
    await trader.cycle()
    assert trader.positions()[0]["quantity"] == "0.5"
    assert trader.positions()[0]["status"] == "open"
    clock[0] += 60
    rest.get_market_orderbook.side_effect = None
    rest.get_market_orderbook.return_value = book("0.57", "0.42")
    await trader.cycle()
    assert len(trader.positions()) == 1
    assert trader.positions()[0]["status"] == "closed"
    assert "cooldown" in trader.watch[0]["status"].lower()
    store.close()


@pytest.mark.asyncio
async def test_entry_spending_cap_includes_fees_and_is_not_replenished_by_profits(tmp_path, clock):
    values = scalp_rules()
    values["rules"][0]["market_spend_limit"] = "0.90"
    store, rest, feed, trader = await setup_trader(tmp_path, clock, settings=values)
    rest.get_market_orderbook.return_value = book("0.57", "0.42")
    await trader.cycle()
    clock[0] += 30
    rest.get_market_orderbook.return_value = book("0.49", "0.50")
    await trader.cycle()
    assert len(trader.positions()) == 1
    assert "spending limit" in trader.watch[0]["status"]
    store.close()


@pytest.mark.asyncio
async def test_small_profit_target_is_net_after_both_fees_not_a_price_increase(tmp_path, clock):
    store, rest, feed, trader = await setup_trader(tmp_path, clock)
    for bid in ("0.51", "0.53", "0.55"):
        rest.get_market_orderbook.return_value = book(bid, str(Decimal("0.99") - Decimal(bid)))
        await trader.cycle()
        assert trader.positions()[0]["status"] == "open"
    rest.get_market_orderbook.return_value = book("0.56", "0.43")
    await trader.cycle()
    assert Decimal(trader.positions()[0]["net_pnl"]) == Decimal("0.02")
    assert trader.positions()[0]["closed_by"] == "take_profit"
    store.close()


@pytest.mark.asyncio
async def test_unexpected_fees_breaching_market_cap_pause_entries_but_keep_the_fill(tmp_path, clock, monkeypatch):
    store, rest, feed, trader = await setup_trader(
        tmp_path, clock, settings=scalp_rules(budget="0.80", market_spend_limit="1.12"))
    rest.get_market_orderbook.return_value = book("0.57", "0.42")
    await trader.cycle()
    clock[0] += 30
    rest.get_market_orderbook.return_value = book("0.54", "0.45")
    monkeypatch.setattr("kalshi_bot.paper.taker_fee", lambda count, price: Decimal("0.08"))
    await trader.cycle()
    assert not trader.enabled
    assert "exceeded market spending limit" in trader.error
    assert len(trader.positions()) == 2
    assert trader.positions()[1]["status"] == "open"
    assert trader.positions()[1]["quantity"] == "1"
    assert trader.positions()[1]["pending"] is None
    assert Decimal(trader.positions()[1]["entry_cost"]) == Decimal("0.63")
    store.close()


@pytest.mark.asyncio
async def test_real_mode_uses_same_cycle_guards_and_price_limited_ioc_orders(tmp_path, clock):
    from kalshi_bot.paper import PaperExchange

    store = Store(str(tmp_path / "real-surface.db"))
    rest = entry_rest()
    exchange = PaperExchange(store, rest)
    # Exercise the live trader's REST surface without sending any network orders.
    rest.get_positions.side_effect = exchange.get_positions
    rest.get_order.side_effect = exchange.get_order
    rest.get_fills.side_effect = exchange.get_fills
    rest.create_event_order.side_effect = exchange.create_event_order
    rest.get_market_orderbook.return_value = book("0.49", "0.50")
    feed = [live_market("KXBTC15M-LIVE", 0.8, 0.49, 0.50, seconds_left=800)]
    lock_call(store, "KXBTC15M-LIVE")
    trader = AutoTrader(store, rest, execution_allowed=True, mode="live", account_identity="test-account",
                        market_feed=lambda: [dict(row, ts_ms=int(clock[0] * 1000)) for row in feed])
    await enabled(trader, scalp_rules(max_cycles=2))
    await trader.cycle()
    rest.get_market_orderbook.return_value = book("0.57", "0.42")
    await trader.cycle()
    clock[0] += 30
    rest.get_market_orderbook.return_value = book("0.49", "0.50")
    await trader.cycle()
    assert len(trader.positions()) == 2
    orders = [call.args[0] for call in rest.create_event_order.call_args_list]
    assert [p["reduce_only"] for p in orders] == [False, True, False]
    assert all(p["time_in_force"] == "immediate_or_cancel" for p in orders)
    assert len({p["client_order_id"] for p in orders}) == 3
    assert all(p["account_identity"] == "test-account" for p in trader.positions())
    store.close()


@pytest.mark.asyncio
async def test_unknown_scalp_order_survives_restart_and_is_never_resubmitted(tmp_path, clock):
    store, rest, feed, trader = await setup_trader(tmp_path, clock)
    held = trader.positions()[0]
    held.update(pending={"client_order_id": "unknown", "action": "sell", "quantity": held["quantity"],
                         "reason": "take_profit"})
    trader.save_position(held)
    store.close()
    store = Store(str(tmp_path / "scalping.db"))
    restarted = AutoTrader(store, entry_rest(), execution_allowed=True, mode="paper",
                           account_identity="paper", market_feed=lambda: feed)
    await restarted.cycle()
    await restarted.cycle()
    restarted.rest.create_event_order.assert_not_called()
    assert restarted.positions()[0]["pending"]["client_order_id"] == "unknown"
    assert restarted.positions()[0]["position_id"] == held["position_id"]
    assert restarted.error
    assert not restarted.enabled
    store.close()


@pytest.mark.asyncio
async def test_zero_fill_cycle_is_not_scored_and_cannot_retry_as_a_new_cycle(tmp_path, clock):
    store = Store(str(tmp_path / "zero.db"))
    rest = entry_rest()
    feed = [live_market("KXBTC15M-SCALP", 0.8, 0.49, 0.50, seconds_left=800)]
    lock_call(store, "KXBTC15M-SCALP")
    trader = await enabled(paper_trader(store, rest, feed), scalp_rules())
    rest.get_market_orderbook.side_effect = [book("0.49", "0.50"), book("0.79", "0.20")]
    await trader.cycle()
    assert trader.positions()[0]["status"] == "skipped"
    assert trader.snapshot()["summary"]["finished"] == 0
    rest.get_market_orderbook.side_effect = None
    rest.get_market_orderbook.return_value = book("0.49", "0.50")
    clock[0] += 30
    await trader.cycle()
    assert len(trader.positions()) == 1
    assert "profitable" in trader.watch[0]["status"]
    store.close()


@pytest.mark.asyncio
async def test_settings_edit_cannot_add_to_an_open_scalp_or_bypass_its_owning_rule(tmp_path, clock):
    store, rest, feed, trader = await setup_trader(tmp_path, clock)
    await trader.control(False, False)
    await trader.save_settings(rules(max_entries=4))
    await trader.control(True, True)
    rest.get_market_orderbook.return_value = book("0.49", "0.50")
    clock[0] += 30
    await trader.cycle()
    assert trader.positions()[0]["entries"] == 1
    assert "full exit" in trader.watch[0]["status"]
    store.close()


@pytest.mark.asyncio
async def test_tightened_loss_limit_uses_full_liquidation_value_not_best_bid(tmp_path, clock):
    store, rest, feed, trader = await setup_trader(tmp_path, clock, settings=scalp_rules(budget="1.50"))
    assert trader.positions()[0]["quantity"] == "2"
    await trader.control(False, False)
    await trader.save_settings(scalp_rules(budget="1.50", stop_loss="0.04", market_loss_limit="0.05"))
    rest.get_market_orderbook.return_value = {"orderbook_fp": {
        "yes_dollars": [["0.60", "0.1"], ["0.48", "1.9"]], "no_dollars": [["0.39", "100"]]
    }}
    await trader.cycle()
    assert trader.positions()[0]["status"] == "closed"
    assert trader.positions()[0]["closed_by"] == "stop_loss"
    assert Decimal(trader.positions()[0]["net_pnl"]) < Decimal("-0.05")
    store.close()


def test_history_summary_keeps_all_totals_and_all_running_positions(tmp_path):
    store = Store(str(tmp_path / "summary.db"))
    trader = AutoTrader(store, None, mode="paper")
    for i in range(120):
        trader.save_position({"ticker": f"BTC-{i}", "position_id": str(i), "opened_ms": i,
                              "status": "closed", "entry_cost": "0.50", "net_pnl": "0.02"})
    trader.save_position({"ticker": "BTC-running", "position_id": "running", "opened_ms": 1,
                          "status": "open", "entry_cost": "0.50", "net_pnl": None})
    snapshot = trader.snapshot()
    assert len(snapshot["positions"]) == 101
    assert any(p["status"] == "open" for p in snapshot["positions"])
    assert snapshot["summary"] == {"running": 1, "finished": 120, "wins": 120, "net_pnl": "2.40"}
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["stale", "degraded", "expired_window", "direction", "paused"])
async def test_entry_rechecks_after_async_book_reads(tmp_path, clock, change):
    store = Store(str(tmp_path / "fresh.db"))
    rest = entry_rest()
    feed = [live_market("KXBTC15M-SCALP", model_p_yes=0.8, yes_bid=0.49, yes_ask=0.50, seconds_left=800)]
    lock_call(store, "KXBTC15M-SCALP")
    trader = AutoTrader(store, rest, execution_allowed=True, account_identity="test-account", market_feed=lambda: feed)
    await enabled(trader, scalp_rules())

    async def read_book(_ticker):
        if change == "stale":
            feed[0]["ts_ms"] = int(clock[0] * 1000) - 11000
        elif change == "degraded":
            feed[0]["quality_flags"] = ["stale_quote"]
        elif change == "expired_window":
            feed[0]["close_ts_ms"] = int(clock[0] * 1000) + 30000
        elif change == "direction":
            feed[0]["model_p_yes"] = 0.1
        else:
            await trader.control(False, False)
        return book("0.49", "0.50")

    rest.get_market_orderbook.side_effect = read_book
    await trader.cycle()
    rest.create_event_order.assert_not_called()
    assert trader.positions() == []
    store.close()


def test_closed_legacy_loss_settlement_and_unfilled_orders_cannot_reenter():
    rule = parse_rules(scalp_rules())[0]
    last = {"status": "closed", "rule": "Test", "quantity": "0", "net_pnl": "0.03", "closed_ms": 1,
            "closed_by": "take_profit", "entry_cost": "0.52", "cycle_number": 1}
    assert can_start_cycle([last], rule, 100000) == "Bot position closed"
    last["scalp"] = rule.scalp.to_json()
    assert can_start_cycle([last], rule, 100000) is None
    for changes in [{"net_pnl": "-0.01"}, {"closed_by": "settled"}, {"net_pnl": "0"},
                    {"pending": {"action": "sell"}}, {"quantity": "0.5"}, {"status": "skipped"}]:
        assert can_start_cycle([{**last, **changes}], rule, 100000) is not None
    assert "loss limit" in can_start_cycle(
        [{**last, "net_pnl": "-0.50", "cycle_number": 1}, {**last, "cycle_number": 2}], rule, 100000)


def test_scalping_api_serialization_validation_and_confirmed_settings(tmp_path):
    store = Store(str(tmp_path / "api.db"))
    trader = AutoTrader(store, entry_rest(), execution_allowed=True, account_identity="test-account")
    client = TestClient(create_app(store, traders={"live": trader, "paper": AutoTrader(store, None, mode="paper")}))
    response = client.put("/api/trading/settings", json={"mode": "live", **scalp_rules()})
    assert response.status_code == 200
    saved = response.json()["live"]["settings"]
    assert saved["rules"][0]["scalping"] is True
    assert saved["rules"][0]["max_cycles"] == 3
    bad = {"rules": [{**saved["rules"][0], "stop_loss": "0"}]}
    assert client.put("/api/trading/settings", json={"mode": "live", **bad}).status_code == 422
    changed = {"rules": [{**saved["rules"][0], "max_cycles": 4}]}
    response = client.post("/api/trading/control", json={"mode": "live", "enabled": True, "confirm": True, "settings": changed})
    assert response.status_code == 409
    assert "Saved settings changed" in response.json()["detail"]
    assert not trader.enabled
    store.close()
