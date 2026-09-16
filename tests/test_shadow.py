import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from kalshi_bot.dashboard.server import create_app
from kalshi_bot.data.store import Store
from kalshi_bot.features.engine import Features
from kalshi_bot.main import BotApp, MarketState
from kalshi_bot.prediction.model import RegularizedSettlementPredictor, SettlementAwarePredictor


@pytest.mark.asyncio
@pytest.mark.parametrize("shadow_fails", [False, True])
async def test_shadow_records_same_inputs_without_changing_live_decision(tmp_path, monkeypatch, shadow_fails):
    app = BotApp.__new__(BotApp)
    app.store = Store(str(tmp_path / "shadow.db"))
    app.settings = SimpleNamespace(
        poll_interval_sec=2, decision_lead_sec=390, edge_threshold=0.05,
        min_quote_size=1.0, min_index_history_sec=240.0,
        min_index_history_ticks=120, max_input_age_ms=5000,
        fee_multiplier=1.0, slippage_per_contract=0.0,
    )
    app.predictor = SettlementAwarePredictor()
    state = MarketState("BTC-TEST", 100.0, 700000, "BRTI")
    app.markets = {state.ticker: state}
    app.index_ticks = {"BRTI": [(timestamp * 1000, 100.0 + (timestamp % 2) * 0.001) for timestamp in range(21, 322)]}
    monkeypatch.setattr("kalshi_bot.main.time.time", lambda: 321.5)
    app._handle_ticker({
        "market_ticker": state.ticker, "yes_bid_dollars": "0.4", "yes_ask_dollars": "0.5",
        "yes_bid_size_fp": "100", "yes_ask_size_fp": "1", "ts_ms": 321000,
        "price_dollars": "0.45", "volume_fp": "1", "open_interest_fp": "1",
    })
    app.store.upsert_active_market(state.ticker, "BRTI", 100, 700000, 321000)
    monkeypatch.setattr("kalshi_bot.main.asyncio.sleep", AsyncMock(side_effect=[None, None, asyncio.CancelledError()]))
    if shadow_fails:
        def fail(*args):
            raise ValueError("shadow unavailable")
        monkeypatch.setattr(app, "_build_shadow_snapshot", fail)
    with pytest.raises(asyncio.CancelledError):
        await app.prediction_loop()
    live = app.store.closed_markets_history()
    assert live == []
    decision = app.store.dashboard_markets(300)[0]
    assert decision["decision_recommendation"] == "BUY_YES"
    assert decision["decision_model_p_yes"] > 0.8
    assert app.predictor.imbalance_weight == 0.05
    shadow_live = app.store.shadow_dashboard_markets(300)
    assert len(shadow_live) == 1
    assert shadow_live[0]["ticker"] == state.ticker
    assert 0.0 <= shadow_live[0]["model_p_yes"] <= 1.0
    assert shadow_live[0]["recommendation"] in {"BUY_YES", "BUY_NO", "NO_EDGE"}
    audit_row = app.store._query("SELECT snapshot_json FROM decision_snapshots WHERE ticker = ?", (state.ticker,)).fetchone()
    assert audit_row is not None
    audit_snapshot = json.loads(audit_row[0])
    assert audit_snapshot["live"]["recommendation"] == "BUY_YES"
    assert audit_snapshot["quality_flags"] == []
    assert audit_snapshot["parameters"]["recommendation_version"] == "purchase-price-v2"
    rows = app.store.shadow_decisions()
    if shadow_fails:
        assert rows == []
    else:
        assert len(rows) == 1
        snapshot = rows[0]["snapshot"]
        assert snapshot["parameters"]["recommendation_version"] == "purchase-price-v2"
        assert snapshot["parameters"]["shadow_model_version"] == "regularized-settlement-v3"
        assert snapshot["parameters"]["shadow_min_history_sec"] == 240
        assert rows[0]["experiment_id"].startswith("regularized-settlement-v3-")
        features = Features(**snapshot["features"])
        assert snapshot["live"]["model_p_yes"] == decision["decision_model_p_yes"]
        assert snapshot["live"]["model_p_yes"] == app.predictor.predict(features)
        assert snapshot["shadow"]["model_p_yes"] == RegularizedSettlementPredictor().predict(features)
        assert snapshot["shadow"]["model_p_yes"] < snapshot["live"]["model_p_yes"]
        assert snapshot["quotes"]["age_ms"] == 500
        assert snapshot["quotes"]["received_at_ms"] == 321500
        assert snapshot["quotes"]["no_ask_dollars"] == 0.6
        assert snapshot["index_tick_age_ms"] == 500
        assert snapshot["index_ticks"] == [list(tick) for tick in app.index_ticks["BRTI"]]
        assert rows[0]["ts_ms"] == decision["decision_ts_ms"]
    app.store.close()


@pytest.mark.asyncio
async def test_jetson_8m30_review_runs_during_early_quality_abstention(tmp_path, monkeypatch):
    class Reviewer:
        enabled = True

        async def review(self, **kwargs):
            assert kwargs["stage"] == "review_8m30"
            return {"decision": "ALLOW", "confidence_adjustment": 0.0, "reason": "Review complete."}

    app = BotApp.__new__(BotApp)
    app.store = Store(str(tmp_path / "reviews.db"))
    app.settings = SimpleNamespace(
        poll_interval_sec=2, decision_lead_sec=390, edge_threshold=0.05,
        min_quote_size=1.0, min_index_history_sec=240.0,
        min_index_history_ticks=1_000, max_input_age_ms=5_000,
        fee_multiplier=1.0, slippage_per_contract=0.0,
    )
    app.predictor = SettlementAwarePredictor()
    app.llm_reviewer = Reviewer()
    state = MarketState("BTC-REVIEW", 100.0, 700_000, "BRTI")
    app.markets = {state.ticker: state}
    app.index_ticks = {"BRTI": [(timestamp * 1000, 100.0) for timestamp in range(1, 301)]}
    app._handle_ticker({
        "market_ticker": state.ticker, "yes_bid_dollars": "0.4", "yes_ask_dollars": "0.5",
        "yes_bid_size_fp": "100", "yes_ask_size_fp": "100", "ts_ms": 195_000,
        "price_dollars": "0.45", "volume_fp": "1", "open_interest_fp": "1",
    })
    monkeypatch.setattr("kalshi_bot.main.time.time", lambda: 195.0)
    monkeypatch.setattr("kalshi_bot.main.asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError()]))

    with pytest.raises(asyncio.CancelledError):
        await app.prediction_loop()

    assert app.store.has_llm_review(state.ticker, "review_8m30")
    assert not app.store.has_decision(state.ticker)
    app.store.close()


def test_shadow_api_scores_only_matched_settled_records_and_separates_experiments(tmp_path):
    store = Store(str(tmp_path / "api.db"))
    client = TestClient(create_app(store))
    shadow_page = client.get("/shadow")
    assert shadow_page.status_code == 200
    assert 'data-page="shadow"' in shadow_page.text
    assert "MODEL UNDER DEVELOPMENT" in shadow_page.text
    assert client.get("/api/shadow-comparison").json()["groups"] == []
    assert client.get("/api/shadow-decisions?limit=0").status_code == 422
    assert client.get("/api/shadow-comparison?limit=10001").status_code == 422
    for ticker, coin, experiment, result in (
        ("BTC-YES", "BRTI", "v1", "yes"),
        ("BTC-NO", "BRTI", "v1", "no"),
        ("BTC-PENDING", "BRTI", "v1", None),
        ("SOL-YES", "SOLUSD_RTI", "v1", "yes"),
        ("BTC-OTHER", "BRTI", "v2", "yes"),
    ):
        store.upsert_active_market(ticker, coin, 100, 391000, 1000)
        store.record_decision(
            ticker=ticker, ts_ms=1000, seconds_to_expiry=390, index_price=101,
            strike=100, model_p_yes=0.9, market_p_yes=0.6, edge=0.3,
            recommendation="BUY_YES", confidence=0.9,
            shadow_snapshot={
                "index_id": coin, "experiment_id": experiment, "market_p_yes": 0.6,
                "live": {"model_p_yes": 0.9, "recommendation": "BUY_YES"},
                "shadow": {"model_p_yes": 0.7, "recommendation": "NO_EDGE"},
            },
        )
        if result:
            store.record_outcome(ticker, result)
    response = client.get("/api/shadow-comparison").json()
    assert response["recorded"] == 5
    assert len(response["groups"]) == 3
    group = next(group for group in response["groups"] if group["index_id"] == "BRTI" and group["experiment_id"] == "v1")
    assert group["recorded"] == 3
    assert group["pending"] == 1
    assert group["scores"]["live"]["n"] == 2
    assert group["scores"]["live"]["accuracy"] == 0.5
    assert group["scores"]["live"]["brier"] == pytest.approx(0.41)
    assert group["scores"]["shadow"]["brier"] == pytest.approx(0.29)
    assert group["scores"]["market"]["brier"] == pytest.approx(0.26)
    assert group["scores"]["live"]["actionable_n"] == 2
    assert group["scores"]["live"]["actionable_correct"] == 1
    assert group["scores"]["shadow"]["actionable_n"] == 0
    assert group["scores"]["live"]["high_confidence_accuracy"] == 0.5
    assert len(client.get("/api/shadow-comparison?index_id=SOLUSD_RTI").json()["groups"]) == 1
    assert len(client.get("/api/shadow-decisions?limit=1").json()) == 1
    assert len(client.get("/api/shadow-decisions?index_id=SOLUSD_RTI").json()) == 1
    store.close()