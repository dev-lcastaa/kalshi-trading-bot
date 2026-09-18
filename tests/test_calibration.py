from kalshi_bot.prediction.calibration import CalibratedPredictor, IsotonicCalibrator


def test_identity_before_enough_samples():
    calibrator = IsotonicCalibrator(min_samples=10)
    calibrator.fit([(0.5, 1.0), (0.6, 0.0)])
    assert not calibrator.is_fitted
    assert calibrator.predict(0.73) == 0.73


def test_fits_monotonic_correction_from_miscalibrated_model():
    # Model says 0.9 but only wins ~50% of the time; says 0.1 and always loses.
    pairs = [(0.9, 1.0), (0.9, 0.0)] * 20 + [(0.1, 0.0)] * 20
    calibrator = IsotonicCalibrator(min_samples=10)
    calibrator.fit(pairs)
    assert calibrator.is_fitted
    assert calibrator.predict(0.9) == 0.5
    assert calibrator.predict(0.1) == 0.0


def test_output_is_monotonic_nondecreasing():
    pairs = [(0.1, 0.0), (0.3, 1.0), (0.3, 0.0), (0.3, 0.0), (0.7, 1.0), (0.9, 1.0)] * 5
    calibrator = IsotonicCalibrator(min_samples=5)
    calibrator.fit(pairs)
    xs = [0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.8, 0.9, 0.95]
    ys = [calibrator.predict(x) for x in xs]
    assert ys == sorted(ys)


def test_calibrated_predictor_delegates_attributes_and_applies_mapping():
    class DummyPredictor:
        momentum_weight = 0.42

        def predict(self, features):
            return 0.8

    calibrator = IsotonicCalibrator(min_samples=1)
    calibrator.fit([(0.8, 0.0)] * 5)
    wrapped = CalibratedPredictor(DummyPredictor(), calibrator)
    assert wrapped.momentum_weight == 0.42
    assert wrapped.predict(features=None) == 0.0
