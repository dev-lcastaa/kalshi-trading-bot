import time

import pytest

from kalshi_bot.features.engine import Features
from kalshi_bot.prediction.model import RandomWalkPredictor
from kalshi_bot.signals.generator import generate_signal


def _features(index_price=100.0, strike=100.0, seconds_to_expiry=60.0):
    return Features(
        index_price=index_price,
        strike=strike,
        seconds_to_expiry=seconds_to_expiry,
        realized_vol_per_sqrt_sec=0.001,
        momentum_per_sec=0.0,
        book_imbalance=None,
    )


def test_generate_signal_defaults_ts_ms_to_now():
    before = int(time.time() * 1000)
    signal = generate_signal(
        ticker="T", index_id="BRTI", features=_features(), predictor=RandomWalkPredictor(),
        yes_bid_dollars=0.4, yes_ask_dollars=0.6, edge_threshold=0.05,
    )
    after = int(time.time() * 1000) + 1  # +1ms tolerance for truncation/rounding
    assert before <= signal.ts_ms <= after


def test_generate_signal_accepts_explicit_ts_ms_for_pinning_final_calls():
    signal = generate_signal(
        ticker="T", index_id="BRTI", features=_features(), predictor=RandomWalkPredictor(),
        yes_bid_dollars=0.4, yes_ask_dollars=0.6, edge_threshold=0.05, ts_ms=1_700_000_000_000,
    )
    assert signal.ts_ms == 1_700_000_000_000


def test_generate_signal_recommendation_thresholds():
    above = generate_signal(
        ticker="T", index_id="BRTI", features=_features(index_price=150.0, strike=100.0, seconds_to_expiry=300.0),
        predictor=RandomWalkPredictor(), yes_bid_dollars=0.1, yes_ask_dollars=0.1, edge_threshold=0.05,
    )
    assert above.recommendation == "BUY_YES"

    no_edge = generate_signal(
        ticker="T", index_id="BRTI", features=_features(), predictor=RandomWalkPredictor(),
        yes_bid_dollars=0.49, yes_ask_dollars=0.51, edge_threshold=0.05,
    )
    assert no_edge.recommendation == "NO_EDGE"


@pytest.mark.parametrize(
    "probability,bid,ask,expected",
    [
        (0.7, 0.3, 0.9, "NO_EDGE"),
        (0.3, 0.1, 0.7, "NO_EDGE"),
        (0.7, 0.58, 0.6, "BUY_YES"),
        (0.3, 0.4, 0.42, "BUY_NO"),
        (0.7, 0.4, 0.68, "NO_EDGE"),
        (0.3, 0.32, 0.6, "NO_EDGE"),
    ],
)
def test_recommendation_requires_edge_at_purchase_price(probability, bid, ask, expected):
    class FixedPredictor:
        def predict(self, features):
            return probability

    signal = generate_signal(
        ticker="T", index_id="BRTI", features=_features(), predictor=FixedPredictor(),
        yes_bid_dollars=bid, yes_ask_dollars=ask, edge_threshold=0.05,
    )

    assert signal.recommendation == expected
    assert signal.market_p_yes == pytest.approx((bid + ask) / 2)
    assert signal.edge == pytest.approx(probability - (bid + ask) / 2)
