import math
from dataclasses import replace

import pytest

from kalshi_bot.features.engine import Features, build_features
from kalshi_bot.prediction.model import RegularizedSettlementPredictor, SettlementAwarePredictor


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


def _ready_features(**changes):
    features = replace(
        _features(100.0, 100.0, 300.0),
        history_span_sec=300.0,
        history_tick_count=301,
    )
    return replace(features, **changes)


def test_v3_projects_drift_to_average_time_with_estimation_uncertainty():
    features = _ready_features(momentum_ols_per_sec=0.00001)
    mean_time = 300 - 60 / 2
    variance_time = 300 - 60 + 60 / 3
    drift_variance = (0.3 ** 2) * (6 / 5) * (mean_time ** 2) / 300
    expected_z = (0.3 * 0.00001 * mean_time) / (0.001 * math.sqrt(variance_time + drift_variance))
    expected = 0.5 * (1 + math.erf(expected_z / math.sqrt(2)))
    assert RegularizedSettlementPredictor().predict(features) == pytest.approx(expected)


def test_v3_ignores_order_book_tilt():
    predictor = RegularizedSettlementPredictor()
    assert predictor.predict(_ready_features(book_imbalance=1.0)) == 0.5
    assert predictor.predict(_ready_features(book_imbalance=-1.0)) == 0.5


@pytest.mark.parametrize("changes", [
    {"history_span_sec": 29.0, "history_tick_count": 30},
    {"history_span_sec": 300.0, "history_tick_count": 2},
    {"realized_vol_per_sqrt_sec": 0.0},
    {"realized_vol_per_sqrt_sec": float("nan")},
    {"index_price": float("inf")},
])
def test_v3_is_neutral_without_reliable_forecast_inputs(changes):
    assert RegularizedSettlementPredictor().predict(_ready_features(**changes)) == 0.5


def test_v3_direction_is_consistent_for_falling_price_below_goal():
    probability = RegularizedSettlementPredictor().predict(_ready_features(
        index_price=99.9, momentum_ols_per_sec=-0.00001,
    ))
    assert probability < 0.5


def test_v3_partial_window_uses_half_remaining_duration_for_drift():
    features = _ready_features(
        seconds_to_expiry=30, window_ticks_observed=30, window_avg_so_far=100.0,
        momentum_ols_per_sec=0.00001,
    )
    mean_time = 0.5 * 30 / 2
    variance_time = 0.5 ** 2 * 30 / 3
    drift_variance = 0.3 ** 2 * (6 / 5) * mean_time ** 2 / 300
    expected_z = 0.3 * 0.00001 * mean_time / (0.001 * math.sqrt(variance_time + drift_variance))
    assert RegularizedSettlementPredictor().predict(features) == pytest.approx(
        0.5 * (1 + math.erf(expected_z / math.sqrt(2)))
    )


def test_v3_requires_full_observation_to_call_settlement_known():
    predictor = RegularizedSettlementPredictor()
    features = _ready_features(seconds_to_expiry=0, index_price=110)
    assert predictor.predict(features) == 0.5
    assert predictor.predict(replace(features, window_ticks_observed=60, window_avg_so_far=99)) == 0.0
    assert predictor.predict(replace(features, window_ticks_observed=60, window_avg_so_far=101)) == 1.0


def test_features_record_history_coverage_for_v3():
    ticks = [(second * 1000, 100 + second * 0.001) for second in range(301)]
    features = build_features(ticks, strike=100, seconds_to_expiry=300)
    assert features.history_span_sec == 300
    assert features.history_tick_count == 301
