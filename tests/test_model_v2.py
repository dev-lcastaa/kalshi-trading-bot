from kalshi_bot.features.engine import Features
from kalshi_bot.prediction.model import SettlementAwarePredictor


def _features(
    index_price,
    strike,
    seconds_to_expiry,
    vol=0.001,
    momentum_ols=0.0,
    book_imbalance=None,
    window_ticks_observed=0,
    window_avg_so_far=None,
):
    return Features(
        index_price=index_price,
        strike=strike,
        seconds_to_expiry=seconds_to_expiry,
        realized_vol_per_sqrt_sec=vol,
        momentum_per_sec=0.0,  # unused by v2
        book_imbalance=book_imbalance,
        momentum_ols_per_sec=momentum_ols,
        window_ticks_observed=window_ticks_observed,
        window_avg_so_far=window_avg_so_far,
    )


def test_at_the_money_far_from_window_is_near_half():
    predictor = SettlementAwarePredictor()
    p = predictor.predict(_features(100.0, 100.0, 300.0))
    assert 0.4 < p < 0.6


def test_deep_in_the_money_approaches_one():
    predictor = SettlementAwarePredictor()
    p = predictor.predict(_features(150.0, 100.0, 300.0, vol=0.0005))
    assert p > 0.95


def test_deep_out_of_the_money_approaches_zero():
    predictor = SettlementAwarePredictor()
    p = predictor.predict(_features(50.0, 100.0, 300.0, vol=0.0005))
    assert p < 0.05


def test_fully_observed_window_is_deterministic_regardless_of_time():
    predictor = SettlementAwarePredictor()
    above = _features(
        101.0, 100.0, 0.0, window_ticks_observed=60, window_avg_so_far=105.0
    )
    below = _features(
        101.0, 100.0, 0.0, window_ticks_observed=60, window_avg_so_far=95.0
    )
    assert predictor.predict(above) == 1.0
    assert predictor.predict(below) == 0.0


def test_uncertainty_shrinks_as_settlement_window_fills():
    predictor = SettlementAwarePredictor()
    # avg_so_far sits just above strike; as more of the window is observed
    # (fewer ticks remaining), the prediction should move closer to 1.0.
    early = _features(
        100.0, 99.5, seconds_to_expiry=50, window_ticks_observed=10, window_avg_so_far=100.0
    )
    late = _features(
        100.0, 99.5, seconds_to_expiry=5, window_ticks_observed=55, window_avg_so_far=100.0
    )
    p_early = predictor.predict(early)
    p_late = predictor.predict(late)
    assert p_late > p_early
    assert 0.0 <= p_early <= 1.0
    assert 0.0 <= p_late <= 1.0


def test_positive_book_imbalance_increases_probability():
    predictor = SettlementAwarePredictor()
    base = predictor.predict(_features(100.0, 100.0, 300.0, vol=0.001, book_imbalance=None))
    tilted = predictor.predict(_features(100.0, 100.0, 300.0, vol=0.001, book_imbalance=1.0))
    assert tilted > base


def test_output_always_within_unit_interval():
    predictor = SettlementAwarePredictor()
    cases = [
        _features(100.0, 100.0, 900.0, vol=0.01, momentum_ols=0.01),
        _features(0.01, 100.0, 900.0, vol=0.01),
        _features(100.0, 100.0, 30.0, window_ticks_observed=45, window_avg_so_far=99.0),
    ]
    for f in cases:
        p = predictor.predict(f)
        assert 0.0 <= p <= 1.0
