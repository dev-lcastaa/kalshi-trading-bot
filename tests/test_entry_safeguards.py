import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx
from fastapi.testclient import TestClient

from kalshi_bot.auto_trader import AutoTrader
from kalshi_bot.dashboard.server import create_app
from kalshi_bot.data.store import Store
from kalshi_bot.paper import PaperExchange
from kalshi_bot.main import BotApp, MarketState
from kalshi_bot.prediction.market_recal import MarketRecalibrator
from kalshi_bot.prediction.model import RegularizedSettlementPredictor
from kalshi_bot.risk import RiskLimits
from tests.test_auto_trader import entry_rest, feed_trader, live_market, rules
from tests.test_fair_value import NOW, ticks


@pytest.mark.parametrize("bad", [
    {"daily_loss_limit": "-1"}, {"max_open_cost": "nan"}, {"max_spread": "1.01"},
    {"uncertainty_buffer": "0.001"}, {"max_signal_age_ms": 10001},
    {"max_book_age_ms": True}, {"max_open_positions": 1.5}, {"require_fair_value": "true"},
    {"unknown": 1},
])
def test_invalid_risk_limits_are_rejected(bad):
    with pytest.raises(ValueError):
        RiskLimits.parse(bad)


def test_budget_reserves_open_losses_and_fragmented_fill_fees():
    risk = RiskLimits.parse({"daily_loss_limit": "2", "max_open_cost": "1"})
    assert risk.affordable_count(10, Decimal("0.45"), Decimal("0"), Decimal("0")) == 2
    assert risk.affordable_count(10, Decimal("0.45"), Decimal("0.55"), Decimal("0")) == 0
    assert risk.affordable_count(10, Decimal("0.45"), Decimal("0"), Decimal("-1.80")) == 0
    assert risk.affordable_count(10, Decimal("0.45"), Decimal("0"), Decimal("0"), Decimal("0.50")) == 1


def test_uncertainty_buffer_and_spread_are_applied_to_executable_prices():
    risk = RiskLimits.parse({"min_edge": ".03", "uncertainty_buffer": ".02", "max_spread": ".03"})
    assert risk.quote_reason(Decimal(".90"), Decimal(".84"), Decimal(".82")) is None
    assert "buffered" in risk.quote_reason(Decimal(".88"), Decimal(".84"), Decimal(".82"))
    assert "spread" in risk.quote_reason(Decimal(".95"), Decimal(".84"), Decimal(".80"))
    assert "spread" in risk.quote_reason(Decimal(".95"), Decimal(".84"), None)


def test_execution_model_uses_current_book_without_mutating_live_quotes(tmp_path):
    app = BotApp.__new__(BotApp)
    app.store = Store(str(tmp_path / "model.db"))
    app.settings = SimpleNamespace(decision_model="fair-value", edge_threshold=.03)
    app.index_history = {"BRTI": ticks(100.5)}
    app.index_ticks = {"BRTI": app.index_history["BRTI"][-301:]}
    state = MarketState("BTC", 100, NOW + 390000, "BRTI")
    state.yes_bid_dollars, state.yes_ask_dollars = .40, .50
    state.quote_ts_ms = NOW - 60000
    app.markets = {"BTC": state}
    app.predictor = RegularizedSettlementPredictor()
    app.market_recalibrator = MarketRecalibrator()
    app.calibrators = {}
    low = app.execution_market("BTC", Decimal(".49"), Decimal(".50"), Decimal("2"), Decimal("3"), NOW)
    high = app.execution_market("BTC", Decimal(".89"), Decimal(".90"), Decimal("2"), Decimal("3"), NOW)
    assert high["model_p_yes"] > low["model_p_yes"]
    assert low["model"] == high["model"] == "fair-value"
    assert "stale_quote" not in high["quality_flags"]
    assert (state.yes_bid_dollars, state.yes_ask_dollars) == (.40, .50)
    stale = app.execution_market("BTC", Decimal(".49"), Decimal(".50"), Decimal("2"), Decimal("3"), NOW + 6000)
    assert "stale_index_tick" in stale["quality_flags"]
    app.store.close()


@pytest.mark.asyncio
async def test_settings_persist_separately_and_confirmation_includes_risk(tmp_path):
    store = Store(str(tmp_path / "risk.db"))
    live = feed_trader(store, entry_rest())
    paper = feed_trader(store, entry_rest(), mode="paper")
    original = live.settings()
    await paper.cycle()
    settings = {**rules(), "risk": {"daily_loss_limit": "5", "max_open_cost": "2"}}
    await paper.save_settings(settings)
    assert live.settings() == original
    restarted = feed_trader(store, entry_rest(), mode="paper")
    assert restarted.risk_limits().max_open_cost == Decimal("2")
    assert not restarted.enabled
    with pytest.raises(ValueError, match="risk limits changed"):
        await paper.control(True, True, rules())
    await paper.control(True, True, paper.settings())
    assert paper.enabled
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["stale", "degraded", "side", "edge", "expired", "removed"])
async def test_ordinary_entries_recheck_prediction_after_book_await(tmp_path, change):
    store = Store(str(tmp_path / "fresh.db"))
    rest = entry_rest()
    feed = [live_market()]
    trader = AutoTrader(store, rest, execution_allowed=True, account_identity="test", market_feed=lambda: feed)
    await trader.cycle()
    await trader.save_settings(rules(min_edge="0.03"))
    await trader.control(True, True)

    async def book(_ticker):
        if change == "stale":
            feed[0]["ts_ms"] = int(time.time() * 1000) - 11000
        elif change == "degraded":
            feed[0]["quality_flags"] = ["stale_index_tick"]
        elif change == "side":
            feed[0]["model_p_yes"] = 0.1
        elif change == "edge":
            feed[0]["model_p_yes"] = 0.46
        elif change == "expired":
            feed[0]["close_ts_ms"] = int(time.time() * 1000) - 1
        else:
            feed.clear()
        return {"orderbook_fp": {"yes_dollars": [["0.44", "10"]], "no_dollars": [["0.55", "10"]]}}

    rest.get_market_orderbook.side_effect = book
    await trader.cycle()
    rest.create_event_order.assert_not_called()
    assert trader.positions() == []
    assert any(event["action"] == "skipped" for event in trader.snapshot()["events"])
    store.close()


@pytest.mark.asyncio
async def test_slow_book_and_future_signal_are_not_traded(tmp_path, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr("kalshi_bot.auto_trader.time.time", lambda: clock[0])
    store = Store(str(tmp_path / "slow.db"))
    rest = entry_rest()
    feed = [live_market()]
    trader = AutoTrader(store, rest, execution_allowed=True, account_identity="test", market_feed=lambda: feed)
    await trader.cycle()
    await trader.save_settings(rules())
    await trader.control(True, True)

    async def book(_ticker):
        clock[0] += 3
        return {"orderbook_fp": {"yes_dollars": [["0.44", "10"]], "no_dollars": [["0.55", "10"]]}}

    rest.get_market_orderbook.side_effect = book
    await trader.cycle()
    rest.create_event_order.assert_not_called()
    assert "freshness limit" in trader.last_skip_reason["BTC"]
    feed[0]["ts_ms"] = int(clock[0] * 1000) + 1000
    assert trader.evaluate_rules() == []
    store.close()


def test_feed_created_during_preview_is_not_mistaken_for_a_future_signal(tmp_path, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr("kalshi_bot.auto_trader.time.time", lambda: clock[0])
    store = Store(str(tmp_path / "feed-clock.db"))

    def feed():
        clock[0] += .010
        return [live_market()]

    trader = AutoTrader(store, None, market_feed=feed)
    store.save_trading_record(trader.settings_key, "settings", rules())
    assert len(trader.evaluate_rules()) == 1
    store.close()


@pytest.mark.asyncio
async def test_execution_callback_reprices_at_actual_bid_and_ask(tmp_path):
    store = Store(str(tmp_path / "reprice.db"))
    rest = entry_rest()
    trader = feed_trader(store, rest)
    trader.execution_feed = lambda ticker, bid, ask, bid_size, ask_size, now: live_market(
        ticker, model_p_yes=0.46, yes_bid=float(bid), yes_ask=float(ask), ts_ms=now,
    )
    await trader.cycle()
    await trader.save_settings(rules(min_edge="0.03"))
    await trader.control(True, True)
    await trader.cycle()
    rest.create_event_order.assert_not_called()
    assert "edge" in trader.last_skip_reason["BTC"]
    store.close()


@pytest.mark.asyncio
async def test_slow_prediction_callback_cannot_use_an_expired_book(tmp_path, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr("kalshi_bot.auto_trader.time.time", lambda: clock[0])
    store = Store(str(tmp_path / "callback.db"))
    rest = entry_rest()
    trader = feed_trader(store, rest)
    await trader.cycle()
    await trader.save_settings(rules())
    await trader.control(True, True)

    def prediction(ticker, bid, ask, bid_size, ask_size, now):
        clock[0] += 3
        return live_market(ticker, ts_ms=now)

    trader.execution_feed = prediction
    await trader.cycle()
    rest.create_event_order.assert_not_called()
    assert "recomputation exceeded" in trader.last_skip_reason["BTC"]
    store.close()


@pytest.mark.asyncio
async def test_rejected_order_keeps_journal_consistent_and_pauses(tmp_path):
    store = Store(str(tmp_path / "rejected.db"))
    rest = entry_rest()
    request = httpx.Request("POST", "https://example.test/orders")
    rest.create_event_order.side_effect = httpx.HTTPStatusError(
        "rejected", request=request, response=httpx.Response(400, request=request),
    )
    trader = feed_trader(store, rest)
    await trader.cycle()
    await trader.save_settings(rules())
    await trader.control(True, True)
    await trader.cycle()
    held = trader.positions()[0]
    assert held["status"] == "skipped" and held["pending"] is None
    assert not held.get("execution_history")
    assert not trader.enabled and trader.error
    assert any(event["action"] == "rejected" for event in trader.snapshot()["events"])
    store.close()


@pytest.mark.asyncio
async def test_portfolio_cap_rechecks_between_different_coin_entries(tmp_path):
    store = Store(str(tmp_path / "portfolio.db"))
    rest = entry_rest()
    trader = feed_trader(
        store, PaperExchange(store, rest), [live_market("KXBTC15M-A"), live_market("KXSOL15M-A")],
        mode="paper", account_identity="paper",
    )
    await trader.cycle()
    await trader.save_settings({**rules(), "risk": {"max_open_cost": ".70"}})
    await trader.control(True, True)
    await trader.cycle()
    assert len(trader.positions()) == 1
    assert trader.open_cost() == Decimal(".47")
    assert "Risk limit" in trader.last_skip_reason["KXSOL15M-A"]
    trader.evaluate_rules()
    sol = next(row for row in trader.watch if row["ticker"] == "KXSOL15M-A")
    assert "Risk limit" in sol["status"]
    rest.create_event_order.assert_not_called()
    store.close()


@pytest.mark.asyncio
async def test_paper_depth_fills_metrics_and_limits_are_persistent(tmp_path):
    store = Store(str(tmp_path / "paper.db"))
    rest = entry_rest()
    rest.get_market_orderbook.return_value = {
        "orderbook_fp": {"yes_dollars": [["0.44", "10"]], "no_dollars": [["0.55", "1"]]},
    }
    trader = feed_trader(store, PaperExchange(store, rest), mode="paper", account_identity="paper")
    await trader.cycle()
    await trader.save_settings({**rules(budget="5"), "risk": {"max_open_positions": 1, "max_open_cost": "1"}})
    await trader.control(True, True)
    await trader.cycle()
    position = trader.positions()[0]
    assert position["quantity"] == "1"
    metric = position["execution_history"][0]
    assert metric["fees"] == "0.02"
    assert Decimal(metric["cost_above_mid"]) == Decimal("0.025")
    assert metric["quote"]["best_size"] == "1"
    assert trader.risk_blockers() == ["Maximum simultaneous positions reached"]
    await trader.control(False, False)
    store.release_trading_worker(trader.worker_id)
    restarted = feed_trader(store, PaperExchange(store, rest), mode="paper", account_identity="paper")
    assert restarted.snapshot()["execution"]["orders"] == 1
    assert restarted.risk_limits().max_open_positions == 1
    rest.get_market.return_value = {"market": {"status": "finalized", "result": "yes"}}
    await restarted.cycle()
    comparison = restarted.snapshot()["execution"]["settlement_comparison"]
    assert comparison["measured_bets"] == 1
    assert Decimal(comparison["predicted_net"]) == Decimal(".43")
    assert Decimal(comparison["realized_net"]) == Decimal(".53")
    rest.create_event_order.assert_not_called()
    store.close()


def test_reference_guard_requires_both_fresh_sources_and_matching_direction(tmp_path):
    store = Store(str(tmp_path / "refs.db"))
    trader = feed_trader(store, entry_rest())
    store.save_trading_record(trader.settings_key, "settings", {
        **rules(), "risk": {"require_reference_agreement": True},
    })
    now = int(time.time() * 1000)
    market = live_market(index_price=101, strike=100)
    assert "required" in trader.reference_reason(market, now)
    for source, price in (("coinbase", 101), ("kraken", 99)):
        store.insert_external_ticks([{
            "source": source, "symbol": "BTC-USD", "index_id": "BRTI", "ts_ms": now,
            "received_at_ms": now, "price": price, "bid": price, "ask": price, "volume_24h": None,
        }])
    assert "kraken disagrees" in trader.reference_reason(market, now)
    store.insert_external_ticks([{
        "source": "kraken", "symbol": "BTC-USD", "index_id": "BRTI", "ts_ms": now + 1,
        "received_at_ms": now + 1, "price": 102, "bid": 102, "ask": 102, "volume_24h": None,
    }])
    assert trader.reference_reason(market, now + 1) is None
    assert "required" in trader.reference_reason(market, now + 6000)
    store.close()


def test_api_validates_risk_fields_and_preserves_other_mode(tmp_path):
    store = Store(str(tmp_path / "api.db"))
    client = TestClient(create_app(store))
    original = client.get("/api/trading").json()["live"]["settings"]
    response = client.put("/api/trading/settings", json={
        "mode": "paper", **rules(), "risk": {"max_open_cost": "2.00", "require_fair_value": True},
    })
    assert response.status_code == 200
    assert response.json()["paper"]["settings"]["risk"]["require_fair_value"]
    assert response.json()["live"]["settings"] == original
    for bad in ({"max_open_cost": "-1"}, {"require_fair_value": "true"}, {"max_book_age_ms": 0}, {"unknown": 1}):
        assert client.put("/api/trading/settings", json={"mode": "paper", **rules(), "risk": bad}).status_code == 422
    store.close()
