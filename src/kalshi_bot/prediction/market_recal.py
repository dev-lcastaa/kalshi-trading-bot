"""Market-anchored probability: Platt scaling of the market's own price.

Walk-forward testing on ~2,500 settled 15-minute markets showed the market
mid-price out-predicts every momentum/book model the bot has, but it carries a
favorite-longshot bias: favorites settle in-the-money a bit more often than
their price implies (fitted slope ~1.1 on the logit scale). Recalibrating the
market price against realized outcomes is the only variant that beat the
market *after fees* in both halves of the history and on both coins.

The fit uses (market_p_yes, outcome) pairs only, so it is independent of the
decision model and cannot feed back on its own recommendations.
"""
from __future__ import annotations

import math
from collections.abc import Iterable

_EPS = 1e-4


def _logit(p: float) -> float:
    p = min(max(p, _EPS), 1 - _EPS)
    return math.log(p / (1 - p))


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1 / (1 + math.exp(-z))
    e = math.exp(z)
    return e / (1 + e)


class MarketRecalibrator:
    """p = sigmoid(intercept + slope * logit(market_p)), fit by Newton's method."""

    def __init__(self, min_samples: int = 300, l2: float = 1.0):
        self.min_samples = min_samples
        self.l2 = l2
        self.intercept = 0.0
        self.slope = 1.0
        self.fitted_n = 0

    @property
    def is_fitted(self) -> bool:
        return self.fitted_n >= self.min_samples

    def fit(self, pairs: Iterable[tuple[float, float]]) -> None:
        data = [(_logit(p), y) for p, y in pairs
                if isinstance(p, (int, float)) and math.isfinite(p) and 0 <= p <= 1 and y in (0.0, 1.0)]
        if len(data) < self.min_samples:
            self.intercept, self.slope, self.fitted_n = 0.0, 1.0, 0
            return
        # Ridge-penalize toward identity (a=0, b=1) so thin data trusts the market.
        a, b = 0.0, 1.0
        for _ in range(50):
            ga = self.l2 * a
            gb = self.l2 * (b - 1)
            haa = hbb = self.l2
            hab = 0.0
            for x, y in data:
                p = _sigmoid(a + b * x)
                w = p * (1 - p)
                ga += p - y
                gb += (p - y) * x
                haa += w
                hab += w * x
                hbb += w * x * x
            det = haa * hbb - hab * hab
            if det <= 0:
                break
            da = (hbb * ga - hab * gb) / det
            db = (haa * gb - hab * ga) / det
            a -= da
            b -= db
            if abs(da) < 1e-9 and abs(db) < 1e-9:
                break
        if not (math.isfinite(a) and math.isfinite(b)) or b <= 0:
            self.intercept, self.slope, self.fitted_n = 0.0, 1.0, 0
            return
        self.intercept, self.slope, self.fitted_n = a, b, len(data)

    def predict(self, market_p: float) -> float:
        if not self.is_fitted:
            return market_p
        return _sigmoid(self.intercept + self.slope * _logit(market_p))
