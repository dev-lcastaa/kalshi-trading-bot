import pytest

from kalshi_bot.auto_trader import locked_gate_reason
from kalshi_bot.data.store import Store
from tests.test_auto_trader import entry_rest, live_market
from tests.test_scalping import clock, lock_call, scalp_rules  # noqa: F401
from tests.test_trading_rules import book, enabled, paper_trader

TICKER = "KXBTC15M-GATE"


async def gated_trader(tmp_path):
    store = Store(str(tmp_path / "gate.db"))
    rest = entry_rest()
    rest.get_market_orderbook.return_value = book("0.49", "0.50")
    feed = [live_market(TICKER, model_p_yes=0.8, yes_bid=0.49, yes_ask=0.50, seconds_left=800)]
    return store, rest, await enabled(paper_trader(store, rest, feed), scalp_rules())


def test_gate_requires_three_checks_and_the_locked_direction():
    decision = {"ticker": TICKER, "ts_ms": 1, "model_p_yes": 0.7, "recommendation": "NO_EDGE",
                "confirmation_agree": 3, "confirmation_total": 4, "confirmation_detail": "[]"}
    assert locked_gate_reason(None, "yes") is not None
    assert locked_gate_reason(decision, "yes") is None
    assert "conflicts" in locked_gate_reason(decision, "no")
    assert "2/4" in locked_gate_reason({**decision, "confirmation_agree": 2}, "yes")
    assert "0/0" in locked_gate_reason({**decision, "confirmation_agree": None, "confirmation_total": None}, "yes")


@pytest.mark.asyncio
async def test_scalper_waits_for_the_lock_then_trades_and_traces_the_market(tmp_path, clock):  # noqa: F811
    store, rest, trader = await gated_trader(tmp_path)
    await trader.cycle()
    assert trader.positions() == []
    assert "lock its direction" in trader.watch[0]["status"]
    rest.create_event_order.assert_not_called()

    lock_call(store, TICKER, agree=3, total=4)
    clock[0] += 3
    await trader.cycle()
    [position] = trader.positions()
    assert position["market_id"] == TICKER == position["ticker"]
    assert position["entry_gate"]["market_id"] == TICKER
    assert position["entry_gate"]["call"] == "yes"
    assert (position["entry_gate"]["checks_agree"], position["entry_gate"]["checks_total"]) == (3, 4)
    assert all(entry["market_id"] == TICKER for entry in position["execution_history"])
    events = [e for e in store.trading_events() if e.get("ticker")]
    assert events and all(e["market_id"] == TICKER for e in events)
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("lock", [{"agree": 2, "total": 4}, {"agree": 4, "total": 4, "up": False}])
async def test_scalper_refuses_weak_or_opposite_locked_calls(tmp_path, clock, lock):  # noqa: F811
    store, rest, trader = await gated_trader(tmp_path)
    lock_call(store, TICKER, **lock)
    await trader.cycle()
    assert trader.positions() == []
    assert "No match" in trader.watch[0]["status"]
    rest.create_event_order.assert_not_called()
    store.close()
