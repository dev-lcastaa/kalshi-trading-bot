import math

from kalshi_bot.features.engine import Features
from kalshi_bot.prediction.logistic import (
    FEATURE_NAMES,
    LogisticRegressionModel,
    LogisticSignalPredictor,
    extract_feature_vector,
    fit_logistic_model,
)


def _features(**overrides) -> Features:
    defaults = dict(
        index_price=100.0,
        strike=100.0,
        seconds_to_expiry=300.0,
        realized_vol_per_sqrt_sec=0.001,
        momentum_per_sec=0.0,
        book_imbalance=0.0,
        momentum_ols_per_sec=0.0,
        momentum_short_per_sec=0.0,
        window_ticks_observed=0,
        window_avg_so_far=None,
        history_span_sec=300.0,
        history_tick_count=300,
    )
    defaults.update(overrides)
    return Features(**defaults)


def test_falls_back_to_base_predictor_before_fit():
    model = LogisticRegressionModel(min_samples=5)
    assert not model.is_fitted

    class DummyBase:
        def predict(self, features):
            return 0.73

    wrapped = LogisticSignalPredictor(DummyBase(), model)
    assert wrapped.predict(_features()) == 0.73


def test_fit_requires_min_samples():
    model = LogisticRegressionModel(min_samples=50)
    model.fit([(extract_feature_vector(_features(), 0.5), 1.0)] * 10)
    assert not model.is_fitted
    assert model.fitted_n == 10


def test_fit_learns_separation_on_synthetic_data():
    model = LogisticRegressionModel(min_samples=20, epochs=300, learning_rate=0.5)
    rows = []
    for i in range(100):
        # base_model_p is the dominant, well-separated signal; outcome follows it.
        p = 0.9 if i % 2 == 0 else 0.1
        y = 1.0 if i % 2 == 0 else 0.0
        rows.append(([p, 0.0, 0.0, 0.0, 0.001, 0.0], y))
    model.fit(rows)
    assert model.is_fitted
    assert model.predict_proba([0.9, 0.0, 0.0, 0.0, 0.001, 0.0]) > 0.7
    assert model.predict_proba([0.1, 0.0, 0.0, 0.0, 0.001, 0.0]) < 0.3


def test_extract_feature_vector_matches_expected_order_and_scaling():
    features = _features(
        momentum_ols_per_sec=0.01,
        momentum_short_per_sec=-0.02,
        book_imbalance=0.4,
        realized_vol_per_sqrt_sec=0.002,
        window_ticks_observed=30,
        seconds_to_expiry=99.0,
    )
    vector = extract_feature_vector(features, base_model_p=0.6)
    assert len(vector) == len(FEATURE_NAMES) == 6
    time_scale = math.sqrt(100.0)
    assert vector[0] == 0.6
    assert vector[1] == 0.01 * time_scale
    assert vector[2] == -0.02 * time_scale
    assert vector[3] == 0.4
    assert vector[4] == 0.002
    assert vector[5] == 30 / 60


def test_extract_feature_vector_defaults_missing_imbalance_to_zero():
    features = _features(book_imbalance=None)
    vector = extract_feature_vector(features, base_model_p=0.5)
    assert vector[3] == 0.0


def test_fit_logistic_model_reconstructs_features_and_skips_malformed_rows():
    class ConstantBase:
        def predict(self, features):
            return 0.5

    good_row = (
        {
            "index_price": 100.0, "strike": 100.0, "seconds_to_expiry": 100.0,
            "realized_vol_per_sqrt_sec": 0.001, "momentum_per_sec": 0.0,
            "book_imbalance": 0.0, "momentum_ols_per_sec": 0.0,
            "momentum_short_per_sec": 0.0, "window_ticks_observed": 0,
            "window_avg_so_far": None, "history_span_sec": 100.0,
            "history_tick_count": 100,
        },
        1.0,
    )
    malformed_row = ({"unexpected_field": 1}, 0.0)
    model = LogisticRegressionModel(min_samples=1)
    fit_logistic_model(model, [good_row, malformed_row], ConstantBase())
    assert model.fitted_n == 1
