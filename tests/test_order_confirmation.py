import time
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest

from kalshi_bot import auto_trader
from kalshi_bot.auto_trader import AutoTrader
from kalshi_bot.data.store import Store


def http_error(status):
    request = httpx.Request("GET", "https://kalshi.test/portfolio/orders/order")
    return httpx.HTTPStatusError("read failed", request=request, response=httpx.Response(status, request=request))


def held():
    return {"ticker": "BTC", "side": "yes", "status": "open", "quantity": "2", "account_identity": "test-account",
            "entry_cost": "0.94", "exit_credit": "0", "pending": None, "net_pnl": None,
            "policy": {"budget": "1", "take_profit": "0.50", "stop_loss": "0.10"}}


def stop_loss_exchange(order_reads):
    """A held position whose stop loss fires; get_order replies come from order_reads in turn."""
    rest = AsyncMock()
    rest.get_market.return_value = {"market": {"status": "active"}}
    rest.get_positions.return_value = {"market_positions": [{"ticker": "BTC", "position_fp": "2"}]}
    rest.get_market_orderbook.return_value = {"orderbook_fp": {"yes_dollars": [["0.40", "2"]]}}
    rest.get_fills.return_value = {"fills": [{"fill_id": "fill", "order_id": "order", "ticker": "BTC",
        "count_fp": "2", "yes_price_dollars": "0.40", "fee_cost": "0.04"}]}
    order = {"ticker": "BTC", "status": "executed", "fill_count_fp": "2"}

    async def submit(payload):
        order["client_order_id"] = payload["client_order_id"]
        return {"order_id": "order", "fill_count": "2.00"}

    async def get_order(order_id):
        reply = order_reads.pop(0) if order_reads else "ok"
        if reply != "ok":
            raise http_error(reply)
        return {"order": dict(order)}

    rest.create_event_order.side_effect = submit
    rest.get_order.side_effect = get_order
    return rest


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(auto_trader, "CONFIRM_RETRY_DELAYS_SEC", (0, 0, 0))


def running_trader(tmp_path, rest):
    store = Store(str(tmp_path / "confirm.db"))
    trader = AutoTrader(store, rest, execution_allowed=True, account_identity="test-account")
    trader.save_position(held())
    trader.enabled = True
    return store, trader


@pytest.mark.asyncio
async def test_order_not_yet_visible_is_retried_and_bot_stays_on(tmp_path):
    rest = stop_loss_exchange([404, 404])
    store, trader = running_trader(tmp_path, rest)
    await trader.cycle()
    position = trader.positions()[0]
    assert position["status"] == "closed" and position["pending"] is None
    assert Decimal(position["net_pnl"]) == Decimal("-0.18")
    assert trader.enabled and trader.error is None
    assert rest.create_event_order.await_count == 1
    store.close()


@pytest.mark.asyncio
async def test_unconfirmed_order_keeps_bot_on_blocks_buys_and_recovers(tmp_path):
    rest = stop_loss_exchange([404] * 4)
    store, trader = running_trader(tmp_path, rest)
    await trader.cycle()
    assert trader.enabled
    assert "waiting for Kalshi to confirm" in trader.error
    assert trader.error in trader.blockers()
    assert trader.positions()[0]["pending"] is not None
    actions = [event["action"] for event in trader.snapshot()["events"]]
    assert "waiting_for_confirmation" in actions and "error" not in actions
    await trader.cycle()
    position = trader.positions()[0]
    assert position["status"] == "closed" and position["pending"] is None
    assert trader.enabled and trader.error is None
    assert rest.create_event_order.await_count == 1
    store.close()


@pytest.mark.asyncio
async def test_order_unconfirmed_too_long_turns_bot_off(tmp_path):
    rest = stop_loss_exchange([404] * 10)
    store, trader = running_trader(tmp_path, rest)
    await trader.cycle()
    position = trader.positions()[0]
    position["pending"]["submitted_ms"] = int(time.time() * 1000) - auto_trader.CONFIRM_GIVE_UP_MS - 1
    trader.save_position(position)
    await trader.cycle()
    assert not trader.enabled
    assert "has not confirmed a placed order" in trader.error
    assert trader.positions()[0]["pending"] is not None
    assert rest.create_event_order.await_count == 1
    store.close()


@pytest.mark.asyncio
async def test_real_order_read_failure_still_turns_bot_off(tmp_path):
    rest = stop_loss_exchange([403])
    store, trader = running_trader(tmp_path, rest)
    await trader.cycle()
    assert not trader.enabled
    assert trader.error == "Trading API or reconciliation failed; entries paused. Check Kalshi before intervening."
    assert trader.positions()[0]["pending"] is not None
    store.close()
