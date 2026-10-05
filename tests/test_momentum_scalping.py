import time
from decimal import Decimal

import pytest

from kalshi_bot.trading import parse_rules
from tests.test_auto_trader import entry_rest, live_market
from tests.test_scalping import clock, scalp_rules
from tests.test_trading_rules import book, enabled, paper_trader
from kalshi_bot.data.store import Store


def momentum_rules(**overrides):
    return scalp_rules(side="momentum", min_edge=None, min_confidence="0.60",
                       budget="1.00", **overrides)


def test_momentum_requires_protected_exits_and_no_settlement_edge():
    rule = parse_rules(momentum_rules())[0]
    assert parse_rules({"rules": [rule.to_json()]}) == [rule]
    for overrides in ({"scalping": False}, {"min_edge": "0.03"}, {"stop_loss": "0"}):
        values = momentum_rules()
        values["rules"][0].update(overrides)
        with pytest.raises(ValueError):
            parse_rules(values)


async def moving_trader(tmp_path, clock, down=False):
    store = Store(str(tmp_path / "momentum.db"))
    rest = entry_rest()
    feed = [live_market("KXBTC15M-MOVE", model_p_yes=0.61, yes_bid=0.60, yes_ask=0.61,
                        seconds_left=800)]
    feed[0]["momentum_short_per_sec"] = -0.001 if down else 0.001
    if down:
        feed[0].update(yes_bid=0.40, yes_ask=0.41)
    trader = await enabled(paper_trader(store, rest, feed), momentum_rules())
    await trader.cycle()
    assert trader.positions() == []
    assert "Warming up" in trader.watch[0]["status"]
    clock[0] += 10
    if down:
        feed[0].update(yes_bid=0.37, yes_ask=0.38)
        rest.get_market_orderbook.return_value = book("0.37", "0.62")
    else:
        feed[0].update(yes_bid=0.63, yes_ask=0.64)
        rest.get_market_orderbook.return_value = book("0.63", "0.36")
    return store, rest, feed, trader


@pytest.mark.asyncio
@pytest.mark.parametrize("down", [False, True])
async def test_buys_moving_side_not_settlement_edge_and_sells_for_net_cents(tmp_path, clock, down):
    store, rest, feed, trader = await moving_trader(tmp_path, clock, down)
    # The screenshot's model/quotes would choose NO under the legacy edge selector.
    if not down:
        legacy = parse_rules(scalp_rules())[0]
        assert legacy.pick_side(Decimal("0.61"), Decimal("0.64"), Decimal("0.37")) == "no"
    await trader.cycle()
    [position] = trader.positions()
    assert position["status"] == "open"
    assert position["side"] == ("no" if down else "yes")
    assert position["entry_signal"]["confidence_source"] == "market"
    assert position["execution_quote"]["expected_edge"] is None
    rest.get_market_orderbook.return_value = book("0.29", "0.70") if down else book("0.70", "0.29")
    await trader.cycle()
    [closed] = trader.positions()
    assert closed["status"] == "closed"
    assert closed["closed_by"] == "take_profit"
    assert Decimal(closed["net_pnl"]) >= Decimal("0.02")
    store.close()


@pytest.mark.asyncio
async def test_actual_entry_side_confidence_blocks_and_watch_explains_it(tmp_path, clock):
    store, rest, feed, trader = await moving_trader(tmp_path, clock)
    settings = momentum_rules()
    settings["rules"][0]["min_confidence"] = "0.65"
    await trader.control(False, False)
    await trader.save_settings(settings)
    await trader.control(True, True)
    await trader.cycle()
    assert trader.positions() == []
    assert trader.watch[0]["side"] == "yes"
    assert trader.watch[0]["entry_confidence"] == "0.635"
    assert "market implies YES 0.635 < 0.65" in trader.watch[0]["status"]
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["flat", "coin_disagrees", "spread_only", "reversal", "missing_momentum"])
async def test_does_not_buy_a_favorite_without_agreeing_movement(tmp_path, clock, change):
    store, rest, feed, trader = await moving_trader(tmp_path, clock)
    if change == "flat":
        feed[0].update(yes_bid=0.60, yes_ask=0.61)
    elif change == "coin_disagrees":
        feed[0]["momentum_short_per_sec"] = -0.001
    elif change == "spread_only":
        feed[0]["yes_bid"] = 0.60
    elif change == "missing_momentum":
        del feed[0]["momentum_short_per_sec"]
    else:
        trader.execution_feed = lambda *args: dict(feed[0], ts_ms=int(time.time() * 1000),
                                                  momentum_short_per_sec=-0.001)
    await trader.cycle()
    assert trader.positions() == []
    if change == "reversal":
        assert any("Entry changed" in event["reason"] for event in trader.snapshot()["events"])
    store.close()


@pytest.mark.asyncio
async def test_restart_requires_fresh_movement_history(tmp_path, clock):
    store, rest, feed, trader = await moving_trader(tmp_path, clock)
    restarted = paper_trader(store, rest, feed)
    await restarted.cycle()
    assert restarted.enabled is False
    assert restarted.quote_history
    assert "Warming up" in restarted.watch[0]["status"]
    store.close()


@pytest.mark.asyncio
async def test_executable_quote_reversal_blocks_entry_even_when_coin_still_rises(tmp_path, clock):
    store, rest, feed, trader = await moving_trader(tmp_path, clock)
    rest.get_market_orderbook.return_value = book("0.62", "0.37")
    await trader.cycle()
    assert trader.positions() == []
    assert any("Quote movement reversed" in event["reason"] for event in trader.snapshot()["events"])
    store.close()


@pytest.mark.asyncio
async def test_old_rise_does_not_qualify_after_quotes_stop_moving(tmp_path, clock):
    store, rest, feed, trader = await moving_trader(tmp_path, clock)
    trader.evaluate_rules()
    clock[0] += 10
    await trader.cycle()
    assert trader.positions() == []
    assert "Waiting for quote movement" in trader.watch[0]["status"]
    store.close()


@pytest.mark.asyncio
async def test_momentum_stop_loss_sells_and_never_chases_the_loss(tmp_path, clock):
    store, rest, feed, trader = await moving_trader(tmp_path, clock)
    await trader.cycle()
    rest.get_market_orderbook.return_value = book("0.40", "0.59")
    await trader.cycle()
    [closed] = trader.positions()
    assert closed["closed_by"] == "stop_loss"
    assert Decimal(closed["net_pnl"]) < 0
    clock[0] += 30
    rest.get_market_orderbook.return_value = book("0.70", "0.29")
    feed[0].update(yes_bid=0.70, yes_ask=0.71)
    await trader.cycle()
    assert len(trader.positions()) == 1
    assert "fully closed profitable" in trader.watch[0]["status"]
    store.close()


def test_profit_room_reserves_both_entry_and_exit_fees():
    policy = parse_rules(momentum_rules())[0].policy
    assert policy.profit_target_reachable(Decimal("0.93"), 1)
    assert not policy.profit_target_reachable(Decimal("0.94"), 1)


@pytest.mark.asyncio
async def test_actual_liquidity_size_must_still_have_room_for_profit(tmp_path, clock):
    store, rest, feed, trader = await moving_trader(tmp_path, clock)
    settings = momentum_rules()
    settings["rules"][0].update(budget="3.00", max_price="0.95")
    await trader.control(False, False)
    await trader.save_settings(settings)
    await trader.control(True, True)
    feed[0].update(yes_bid=0.93, yes_ask=0.94)
    # Three contracts have theoretical room, but only one is actually offered.
    rest.get_market_orderbook.return_value = book("0.93", "0.06", "1")
    await trader.cycle()
    assert trader.positions() == []
    assert any("Not enough price room" in event["reason"] for event in trader.snapshot()["events"])
    store.close()
