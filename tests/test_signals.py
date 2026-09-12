import time

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
