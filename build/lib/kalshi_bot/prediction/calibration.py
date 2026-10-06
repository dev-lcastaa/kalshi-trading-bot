"""Isotonic recalibration of model probabilities against realized outcomes.

Rule-based predictors (see `model.py`) are internally consistent but not
guaranteed to be well-calibrated: e.g. among all markets where the model says
p=0.65, the true settlement rate might actually be 0.55. This fits a
monotonic (isotonic) correction curve from historical (predicted_p, outcome)
pairs via pool-adjacent-violators (PAVA), then maps new predictions through
it - a from-scratch implementation to avoid adding a numpy/sklearn
dependency for one small algorithm.

This is "online learning" in the practical sense used here: the mapping is
refit periodically (see `main.py`'s calibration_loop) on a rolling window of
recent settled decisions, so it adapts as the bot accumulates a track record,
without ever touching the underlying rule-based predictors.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import Predictor


def _pool_adjacent_violators(sorted_pairs: list[tuple[float, float]]) -> list[tuple[float, float, int]]:
    """Fit a monotonically non-decreasing step function via PAVA.

    `sorted_pairs` must be (x, y) sorted ascending by x. Returns blocks of
    (x_max, fitted_y, weight) representing the isotonic fit.
    """
    # Each block starts as its own single point: (sum_y, count, x_max).
    blocks: list[list[float]] = [[y, 1.0, x] for x, y in sorted_pairs]
    i = 0
    while i < len(blocks) - 1:
        mean_i = blocks[i][0] / blocks[i][1]
        mean_next = blocks[i + 1][0] / blocks[i + 1][1]
        if mean_i > mean_next:
            merged = [blocks[i][0] + blocks[i + 1][0], blocks[i][1] + blocks[i + 1][1], blocks[i + 1][2]]
            blocks[i:i + 2] = [merged]
            i = max(i - 1, 0)
        else:
            i += 1
    return [(x_max, total_y / weight, int(weight)) for total_y, weight, x_max in blocks]


@dataclass
class IsotonicCalibrator:
    """Maps a raw probability through a fitted isotonic correction curve.

    Falls back to the identity mapping (returns the input unchanged) until
    `fit()` has seen at least `min_samples` (p, outcome) pairs, so a cold
    start never distorts predictions on sparse data.
    """

    min_samples: int = 200
    _knots: list[tuple[float, float]] = field(default_factory=list)
    fitted_n: int = 0

    def fit(self, pairs: list[tuple[float, float]]) -> None:
        """Refit from scratch on `pairs` of (predicted_p, outcome in {0, 1})."""
        clean = [
            (float(p), float(y))
            for p, y in pairs
            if p == p and y in (0.0, 1.0)  # p == p filters NaN
        ]
        self.fitted_n = len(clean)
        if len(clean) < self.min_samples:
            self._knots = []
            return
        clean.sort(key=lambda pair: pair[0])
        blocks = _pool_adjacent_violators(clean)
        self._knots = [(x_max, fitted_y) for x_max, fitted_y, _weight in blocks]

    @property
    def is_fitted(self) -> bool:
        return bool(self._knots)

    def predict(self, p: float) -> float:
        """Map a raw probability through the fitted curve (identity if unfit)."""
        if not self._knots or p != p:
            return p
        # Step function: find the first knot whose x is >= p, else use the last.
        for x_max, fitted_y in self._knots:
            if p <= x_max:
                return fitted_y
        return self._knots[-1][1]


class CalibratedPredictor:
    """Wraps a `Predictor`, passing its output through an `IsotonicCalibrator`.

    Delegates attribute access to the wrapped predictor so code that inspects
    predictor-specific fields (e.g. `momentum_weight`) keeps working.
    """

    def __init__(self, base: Predictor, calibrator: IsotonicCalibrator):
        self._base = base
        self.calibrator = calibrator

    def predict(self, features) -> float:  # noqa: ANN001 - matches Predictor protocol
        return self.calibrator.predict(self._base.predict(features))

    def __getattr__(self, name: str):
        return getattr(self._base, name)
