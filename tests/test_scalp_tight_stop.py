from decimal import Decimal

import pytest

from tests.test_momentum_scalping import momentum_rules, moving_trader
from tests.test_scalping import clock  # noqa: F401
from tests.test_trading_rules import book


async def tighten(trader, stop):
    settings = momentum_rules(take_profit="0.03", stop_loss=stop, market_loss_limit=stop)
    await trader.control(False, False)
    await trader.save_settings(settings)
    await trader.control(True, True)


@pytest.mark.asyncio
async def test_stop_too_tight_for_the_spread_and_fees_is_refused_with_a_clear_reason(tmp_path, clock):  # noqa: F811
    store, rest, feed, trader = await moving_trader(tmp_path, clock)
    await tighten(trader, "0.06")
    await trader.cycle()
    assert trader.positions() == []
    assert any("room before the $0.06 stop loss" in e["reason"] for e in trader.snapshot()["events"])
    store.close()


@pytest.mark.asyncio
async def test_small_stop_with_room_trades_and_a_failed_bet_loses_only_a_few_cents(tmp_path, clock):  # noqa: F811
    store, rest, feed, trader = await moving_trader(tmp_path, clock)
    await tighten(trader, "0.07")
    await trader.cycle()
    [position] = trader.positions()
    assert position["status"] == "open"
    rest.get_market_orderbook.return_value = book("0.60", "0.39")
    await trader.cycle()
    [closed] = trader.positions()
    assert closed["closed_by"] == "stop_loss"
    assert Decimal("-0.10") <= Decimal(closed["net_pnl"]) <= Decimal("-0.05")
    store.close()
