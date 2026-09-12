from kalshi_bot.features.engine import Features
from kalshi_bot.prediction.model import RandomWalkPredictor, market_implied_probability


def _features(index_price, strike, seconds_to_expiry, vol=0.001, momentum=0.0):
    return Features(
        index_price=index_price,
        strike=strike,
        seconds_to_expiry=seconds_to_expiry,
        realized_vol_per_sqrt_sec=vol,
        momentum_per_sec=momentum,
        book_imbalance=None,
    )


def test_at_the_money_is_near_half():
    predictor = RandomWalkPredictor()
    p = predictor.predict(_features(100.0, 100.0, 300.0))
    assert 0.4 < p < 0.6


def test_deep_in_the_money_approaches_one():
    predictor = RandomWalkPredictor()
    p = predictor.predict(_features(150.0, 100.0, 300.0, vol=0.0005))
    assert p > 0.95


def test_deep_out_of_the_money_approaches_zero():
    predictor = RandomWalkPredictor()
    p = predictor.predict(_features(50.0, 100.0, 300.0, vol=0.0005))
    assert p < 0.05


def test_zero_time_to_expiry_is_deterministic():
    predictor = RandomWalkPredictor()
    assert predictor.predict(_features(101.0, 100.0, 0.0)) == 1.0
    assert predictor.predict(_features(99.0, 100.0, 0.0)) == 0.0


def test_positive_momentum_increases_probability():
    predictor = RandomWalkPredictor()
    base = predictor.predict(_features(100.0, 100.0, 300.0, vol=0.001, momentum=0.0))
    with_momentum = predictor.predict(_features(100.0, 100.0, 300.0, vol=0.001, momentum=0.001))
    assert with_momentum > base


def test_market_implied_probability_is_midpoint():
    assert market_implied_probability(0.40, 0.60) == 0.5
