import asyncio
import time
from decimal import Decimal

import pytest

from kalshi_bot.auto_trader import AutoTrader
from kalshi_bot.data.store import Store

DELAY = 0.2


class SlowRest:
    """Every read takes DELAY seconds; bids sit far below the stop for each ticker in `crashed`."""

    def __init__(self, crashed=("A", "B"), failing=()):
        self.crashed = set(crashed)
        self.failing = set(failing)
        self.orders = []

    async def get_market(self, ticker):
        await asyncio.sleep(DELAY)
        if ticker in self.failing:
            raise RuntimeError("market read failed")
        return {"market": {"status": "active"}}

    async def get_positions(self, ticker):
        await asyncio.sleep(DELAY)
        return {"market_positions": [{"ticker": ticker, "position_fp": "2"}], "cursor": ""}

    async def get_market_orderbook(self, ticker):
        await asyncio.sleep(DELAY)
        return {"orderbook_fp": {"yes_dollars": [["0.40" if ticker in self.crashed else "0.90", "2"]]}}

    async def create_event_order(self, payload):
        self.orders.append(payload["ticker"])
        return {"order_id": payload["ticker"], "fill_count": "2"}

    async def get_order(self, order_id):
        return {"order": {"ticker": order_id, "status": "executed", "fill_count_fp": "2",
                          "client_order_id": self.client_ids[order_id]}}

    async def get_fills(self, order_id, cursor=""):
        return {"fills": [{"fill_id": order_id, "order_id": order_id, "ticker": order_id, "count_fp": "2",
                           "yes_price_dollars": "0.40", "fee_cost": "0.04"}], "cursor": ""}

    client_ids: dict = {}


def held(ticker):
    return {"ticker": ticker, "side": "yes", "status": "open", "quantity": "2",
            "account_identity": "test-account", "entry_cost": "0.94", "exit_credit": "0",
            "pending": None, "net_pnl": None,
            "policy": {"budget": "1", "take_profit": "0.50", "stop_loss": "0.10"}}


def trader_with(tmp_path, rest, tickers):
    rest.client_ids = {}
    original = rest.create_event_order

    async def create(payload):
        rest.client_ids[payload["ticker"]] = payload["client_order_id"]
        return await original(payload)

    rest.create_event_order = create
    store = Store(str(tmp_path / "latency.db"))
    trader = AutoTrader(store, rest, execution_allowed=True, account_identity="test-account")
    for ticker in tickers:
        trader.save_position(held(ticker))
    return trader, store


@pytest.mark.asyncio
async def test_a_monitor_pass_costs_one_round_trip_not_three(tmp_path):
    trader, store = trader_with(tmp_path, SlowRest(), ["A"])
    started = time.monotonic()
    await trader.monitor(trader.positions()[0])
    assert time.monotonic() - started < 2 * DELAY
    store.close()


@pytest.mark.asyncio
async def test_stops_in_different_markets_are_serviced_in_parallel(tmp_path):
    rest = SlowRest()
    trader, store = trader_with(tmp_path, rest, ["A", "B"])
    started = time.monotonic()
    await trader.cycle()
    assert time.monotonic() - started < 3 * DELAY
    assert sorted(rest.orders) == ["A", "B"]
    assert {p["status"] for p in trader.positions()} == {"closed"}
    assert all(p["closed_by"] == "stop_loss" or p.get("exit_trigger") == "stop_loss" for p in trader.positions())
    assert all(p["exit_trigger_ms"] > 0 for p in trader.positions())
    assert trader.snapshot()["last_cycle_duration_ms"] >= 0
    store.close()


@pytest.mark.asyncio
async def test_one_failing_market_does_not_delay_another_markets_stop(tmp_path):
    rest = SlowRest(failing=("A",))
    trader, store = trader_with(tmp_path, rest, ["A", "B"])
    await trader.cycle()
    assert rest.orders == ["B"]
    assert trader.error
    assert not trader.enabled
    by_ticker = {p["ticker"]: p for p in trader.positions()}
    assert by_ticker["B"]["status"] == "closed"
    assert by_ticker["A"]["status"] == "open"
    store.close()


@pytest.mark.asyncio
async def test_settled_market_ignores_the_unreadable_book(tmp_path):
    rest = SlowRest()

    async def settled(ticker):
        return {"market": {"status": "finalized", "result": "yes"}}

    async def no_book(ticker):
        raise RuntimeError("no order book for a settled market")

    rest.get_market = settled
    rest.get_market_orderbook = no_book
    trader, store = trader_with(tmp_path, rest, ["A"])
    await trader.cycle()
    position = trader.positions()[0]
    assert position["closed_by"] == "settled"
    assert Decimal(position["net_pnl"]) == Decimal("1.06")
    assert trader.error is None
    store.close()
