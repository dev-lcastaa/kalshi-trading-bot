import math
import random

from kalshi_bot.features.engine import build_features
from kalshi_bot.prediction.market_recal import MarketRecalibrator
from kalshi_bot.prediction.model import SettlementAwarePredictor
from kalshi_bot.signals.generator import generate_signal


def biased_pairs(n=3000, slope=1.3, seed=7):
    rng = random.Random(seed)
    pairs = []
    for _ in range(n):
        p = rng.uniform(0.05, 0.95)
        z = slope * math.log(p / (1 - p))
        pairs.append((p, 1.0 if rng.random() < 1 / (1 + math.exp(-z)) else 0.0))
    return pairs


def test_identity_until_enough_samples():
    recal = MarketRecalibrator(min_samples=300)
    recal.fit(biased_pairs(n=100))
    assert not recal.is_fitted
    assert recal.predict(0.8) == 0.8


def test_recovers_favorite_longshot_bias():
    recal = MarketRecalibrator(min_samples=300)
    recal.fit(biased_pairs())
    assert recal.is_fitted
    assert 1.15 < recal.slope < 1.45
    assert abs(recal.intercept) < 0.15
    assert recal.predict(0.8) > 0.8
    assert recal.predict(0.2) < 0.2
    assert abs(recal.predict(0.5) - 0.5) < 0.04


def test_ignores_invalid_pairs():
    recal = MarketRecalibrator(min_samples=3)
    recal.fit([(float("nan"), 1.0), (2.0, 1.0), (0.5, 0.5)])
    assert not recal.is_fitted


def test_generator_uses_recalibrated_market_and_keeps_blend_for_history():
    ticks = [(i * 1000, 100.0 + 0.01 * i) for i in range(300)]
    features = build_features(ticks, strike=100.0, seconds_to_expiry=390, yes_bid_size=10, yes_ask_size=10,
                              close_ts_ms=700_000)
    recal = MarketRecalibrator(min_samples=300)
    recal.fit(biased_pairs())
    signal = generate_signal(
        ticker="T", index_id="BRTI", features=features, predictor=SettlementAwarePredictor(),
        yes_bid_dollars=0.79, yes_ask_dollars=0.80, edge_threshold=0.0, market_blend_weight=0.8,
        market_recalibrator=recal,
    )
    assert signal.model_p_yes == recal.predict(signal.market_p_yes)
    assert signal.pre_calibration_model_p_yes != signal.model_p_yes
    assert signal.recommendation == "BUY_YES"
