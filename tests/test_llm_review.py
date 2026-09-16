import json

import pytest

from kalshi_bot.features.engine import Features
from kalshi_bot.llm_review import LlmReviewError, LlmReviewer


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Client:
    def __init__(self, response):
        self.response = response
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, _url):
        return _Response({"data": [{"id": "test-model"}]})

    async def post(self, _url, json):
        self.posts.append(json)
        return _Response(self.response)


def _features():
    return Features(
        index_price=100.0,
        strike=100.2,
        seconds_to_expiry=390.0,
        realized_vol_per_sqrt_sec=0.0001,
        momentum_per_sec=0.0,
        book_imbalance=0.2,
        momentum_ols_per_sec=0.000001,
        momentum_short_per_sec=-0.000001,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["review_8m30", "early", "review_4m30", "late", "review_1m"])
async def test_llm_review_uses_discovered_model_and_strict_prompt(monkeypatch, stage):
    client = _Client({
        "choices": [{"message": {"content": json.dumps({
            "decision": "REDUCE_CONFIDENCE",
            "confidence_adjustment": -0.15,
            "reason": "Signals conflict.",
        })}}]
    })
    monkeypatch.setattr("kalshi_bot.llm_review.httpx.AsyncClient", lambda **_kwargs: client)

    result = await LlmReviewer("http://jetson:8080").review(
        stage=stage, ticker="BTC-1", index_id="BRTI", seconds_to_expiry=390,
        features=_features(), model_p_yes=0.62, market_p_yes=0.55,
        recommendation="BUY_YES", quality_flags=[], quote_age_ms=100,
        index_tick_age_ms=200,
        yes_bid_dollars=0.54, yes_ask_dollars=0.56,
        yes_bid_size=100, yes_ask_size=80,
        confirmation_agree=2, confirmation_total=3,
    )

    assert result["decision"] == "REDUCE_CONFIDENCE"
    assert result["confidence_adjustment"] == -0.15
    assert client.posts[0]["model"] == "test-model"
    assert client.posts[0]["temperature"] == 0
    assert "exactly one JSON object" in client.posts[0]["messages"][0]["content"]
    sent = json.loads(client.posts[0]["messages"][1]["content"])
    assert sent["distance_from_strike"] == pytest.approx(-0.2)
    assert sent["expected_move_dollars_1sigma"] > 0
    assert sent["spread_dollars"] == pytest.approx(0.02)
    assert sent["yes_bid_size"] == 100
    assert sent["confirmation_agree"] == 2


@pytest.mark.asyncio
async def test_llm_review_rejects_positive_reduction(monkeypatch):
    client = _Client({
        "choices": [{"message": {"content": json.dumps({
            "decision": "REDUCE_CONFIDENCE",
            "confidence_adjustment": 0.2,
            "reason": "Bad sign.",
        })}}]
    })
    monkeypatch.setattr("kalshi_bot.llm_review.httpx.AsyncClient", lambda **_kwargs: client)

    with pytest.raises(LlmReviewError):
        await LlmReviewer("http://jetson:8080", model="test-model").review(
            stage="late", ticker="SOL-1", index_id="SOLUSD_RTI", seconds_to_expiry=150,
            features=_features(), model_p_yes=0.62, market_p_yes=0.55,
            recommendation="BUY_YES", quality_flags=[], quote_age_ms=100,
            index_tick_age_ms=200,
        )
