import time
from decimal import Decimal

import pytest

from kalshi_bot.auto_trader import AutoTrader
from kalshi_bot.data.store import Store
from kalshi_bot.paper import PaperExchange
from kalshi_bot.trading import EntryRule, TradingPolicy, parse_rules, rules_json, taker_fee
from tests.test_auto_trader import entry_rest, live_market, rules


def paper_trader(store, rest, feed):
    return AutoTrader(store, PaperExchange(store, rest), execution_allowed=True, mode="paper",
                      account_identity="paper",
                      market_feed=lambda: [dict(row, ts_ms=int(time.time() * 1000)) for row in feed])


async def enabled(trader, settings):
    await trader.cycle()
    await trader.save_settings(settings)
    await trader.control(True, True)
    return trader


def book(yes_bid="0.79", no_bid="0.20", size="100"):
    return {"orderbook_fp": {"yes_dollars": [[yes_bid, size]], "no_dollars": [[no_bid, size]]}}


def test_rule_checks_coin_time_price_confidence_and_edge():
    rule = EntryRule.parse(rules(coin="SOL", min_price="0.60", max_price="0.90", min_confidence="0.80",
                                 min_edge="0.01", min_seconds_left=300, max_seconds_left=420)["rules"][0])
    p = Decimal("0.90")
    assert rule.check("KXSOL15M-X", 360, p, "yes", Decimal("0.80")) is None
    assert "coin" in rule.check("KXBTC15M-X", 360, p, "yes", Decimal("0.80"))
    assert "outside 300-420s" in rule.check("KXSOL15M-X", 200, p, "yes", Decimal("0.80"))
    assert "outside 0.60-0.90" in rule.check("KXSOL15M-X", 360, p, "yes", Decimal("0.95"))
    assert "model gives" in rule.check("KXSOL15M-X", 360, Decimal("0.75"), "yes", Decimal("0.70"))
    # 0.90 - 0.88 - fee(0.01) = 0.01 edge passes; at 0.89 it does not.
    assert taker_fee(Decimal("0.88")) == Decimal("0.01")
    assert rule.check("KXSOL15M-X", 360, p, "yes", Decimal("0.88")) is None
    assert "edge" in rule.check("KXSOL15M-X", 360, p, "yes", Decimal("0.89"))
    assert rule.pick_side(Decimal("0.3")) == "no"


@pytest.mark.parametrize("bad", [
    {"min_price": "0.90", "max_price": "0.60"}, {"budget": "25.01"}, {"side": "maybe"},
    {"min_seconds_left": 500, "max_seconds_left": 400}, {"coin": "B-T"}, {"stop_loss": "1.00"},
])
def test_invalid_rules_rejected(bad):
    with pytest.raises((ValueError, ArithmeticError)):
        parse_rules(rules(**bad))


def test_legacy_settings_migrate_to_one_hold_to_settlement_rule(tmp_path):
    store = Store(str(tmp_path / "legacy.db"))
    store.save_trading_record("settings:paper", "settings",
                              {"budget": "1.50", "take_profit": "0.50", "stop_loss": "0.10"})
    trader = AutoTrader(store, None, mode="paper")
    [rule] = trader.rules()
    assert rule.policy == TradingPolicy(budget=Decimal("1.50"))
    assert store.trading_record("settings:paper") == rules_json([rule])
    store.close()


@pytest.mark.asyncio
async def test_favorite_is_bought_and_held_to_settlement(tmp_path):
    store = Store(str(tmp_path / "favorite.db"))
    rest = entry_rest()
    rest.get_market_orderbook.return_value = book()
    trader = await enabled(paper_trader(store, rest, [live_market(model_p_yes=0.85, yes_bid=0.79, yes_ask=0.80)]),
                           rules(budget="5.00"))
    await trader.cycle()
    [held] = trader.positions()
    assert held["status"] == "open" and held["side"] == "yes" and held["rule"] == "Test"
    # $5 at 0.80 with 2c/contract fee reserve = 6 contracts.
    assert held["quantity"] == "6"
    # A crash in price never triggers an exit when take-profit/stop-loss are 0.
    rest.get_market_orderbook.return_value = book(yes_bid="0.05", no_bid="0.94")
    await trader.cycle()
    assert trader.positions()[0]["status"] == "open"
    rest.get_market.return_value = {"market": {"status": "finalized", "result": "yes"}}
    await trader.cycle()
    held = trader.positions()[0]
    assert held["status"] == "closed"
    assert held["closed_by"] == "settled" and held["result"] == "yes"
    assert held["opened_ms"] <= held["closed_ms"] and held["close_ts_ms"] > held["opened_ms"]
    assert Decimal(held["net_pnl"]) == Decimal("6") - Decimal(held["entry_cost"])
    store.close()


@pytest.mark.asyncio
async def test_no_side_rule_buys_no_at_one_minus_yes_bid(tmp_path):
    store = Store(str(tmp_path / "no.db"))
    rest = entry_rest()
    rest.get_market_orderbook.return_value = book(yes_bid="0.20", no_bid="0.79")
    trader = await enabled(paper_trader(store, rest, [live_market(model_p_yes=0.15, yes_bid=0.20, yes_ask=0.21)]),
                           rules(side="model", min_price="0.70", max_price="0.85"))
    await trader.cycle()
    [held] = trader.positions()
    assert held["side"] == "no"
    assert held["quantity"] == "1"
    assert Decimal(held["entry_cost"]) == Decimal("0.80") + taker_fee(Decimal("0.80"))
    store.close()


@pytest.mark.asyncio
async def test_one_position_per_coin_but_coins_trade_concurrently(tmp_path):
    store = Store(str(tmp_path / "coins.db"))
    rest = entry_rest()
    rest.get_market_orderbook.return_value = book()
    feed = [live_market("KXBTC15M-A", 0.85, 0.79, 0.80, seconds_left=300),
            live_market("KXBTC15M-B", 0.85, 0.79, 0.80, seconds_left=600),
            live_market("KXSOL15M-A", 0.85, 0.79, 0.80, seconds_left=300)]
    trader = await enabled(paper_trader(store, rest, feed), rules())
    await trader.cycle()
    await trader.cycle()
    assert sorted(p["ticker"] for p in trader.positions()) == ["KXBTC15M-A", "KXSOL15M-A"]
    waiting = next(row for row in trader.snapshot()["watch"] if row["ticker"] == "KXBTC15M-B")
    assert "already open" in waiting["status"]
    store.close()


@pytest.mark.asyncio
async def test_price_moved_after_match_skips_without_blocking_market(tmp_path):
    store = Store(str(tmp_path / "moved.db"))
    rest = entry_rest()
    rest.get_market_orderbook.return_value = book(yes_bid="0.94", no_bid="0.04")  # real ask 0.96
    trader = await enabled(paper_trader(store, rest, [live_market(model_p_yes=0.9, yes_bid=0.79, yes_ask=0.80)]),
                           rules(max_price="0.90"))
    await trader.cycle()
    await trader.cycle()
    assert trader.positions() == []
    skips = [e for e in trader.snapshot()["events"] if e["action"] == "skipped"]
    assert len(skips) == 1 and "no longer matches" in skips[0]["reason"]
    rest.get_market_orderbook.return_value = book()
    trader.retry_after_ms.clear()
    await trader.cycle()
    assert trader.positions()[0]["status"] == "open"
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("market", [
    live_market(quality_flags=["stale_quote"]),
    live_market(model_p_yes=0.85, yes_bid=0.79, yes_ask=0.80, seconds_left=100),
])
async def test_degraded_or_nonmatching_markets_are_watched_not_traded(tmp_path, market):
    store = Store(str(tmp_path / "watch.db"))
    rest = entry_rest()
    rest.get_market_orderbook.return_value = book()
    trader = await enabled(paper_trader(store, rest, [market]), rules(min_seconds_left=300))
    await trader.cycle()
    assert trader.positions() == []
    [row] = trader.snapshot()["watch"]
    assert row["status"].startswith(("Degraded", "No match"))
    store.close()


@pytest.mark.asyncio
async def test_paused_trader_previews_matches_without_trading(tmp_path):
    store = Store(str(tmp_path / "preview.db"))
    rest = entry_rest()
    rest.get_market_orderbook.return_value = book()
    trader = paper_trader(store, rest, [live_market(model_p_yes=0.85, yes_bid=0.79, yes_ask=0.80)])
    await trader.cycle()
    await trader.save_settings(rules())
    await trader.cycle()
    [row] = trader.snapshot()["watch"]
    assert row["status"] == "Matches 'Test'" and row["side"] == "yes" and row["price"] == "0.80"
    assert trader.positions() == []
    store.close()


@pytest.mark.asyncio
async def test_cannot_enable_with_all_rules_disabled(tmp_path):
    store = Store(str(tmp_path / "disabled.db"))
    trader = paper_trader(store, entry_rest(), [])
    await trader.cycle()
    await trader.save_settings(rules(enabled=False))
    with pytest.raises(ValueError, match="at least one rule"):
        await trader.control(True, True)
    store.close()
