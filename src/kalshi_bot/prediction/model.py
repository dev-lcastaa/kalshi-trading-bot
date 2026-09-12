"""Rule-based probability model: random-walk (GBM) + momentum tilt.

Predicts P(index price finishes above the market's strike at expiry).
Kept behind a small protocol so it can be swapped for an ML model later
without touching callers.
"""
from __future__ import annotations

import math
from typing import Protocol

from ..features.engine import SETTLEMENT_WINDOW_SEC, Features


class Predictor(Protocol):
    def predict(self, features: Features) -> float: ...


def _normal_cdf(z: float) -> float:
    """Standard normal CDF via math.erf (avoids a scipy/numpy native dependency)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


class RandomWalkPredictor:
    """P(yes) via GBM: ln(S_T/S) ~ N(mu*T, sigma^2*T).

    `momentum_weight` dampens the momentum-derived drift term (raw rolling
    momentum is noisy over short windows); 0 disables it, 1 uses it as-is.
    """

    def __init__(self, momentum_weight: float = 0.3):
        self.momentum_weight = momentum_weight

    def predict(self, features: Features) -> float:
        s = features.index_price
        k = features.strike
        t = features.seconds_to_expiry

        if s <= 0 or k <= 0:
            return 0.5
        if t <= 0:
            if s > k:
                return 1.0
            if s < k:
                return 0.0
            return 0.5

        sigma = features.realized_vol_per_sqrt_sec * math.sqrt(t)
        mu = self.momentum_weight * features.momentum_per_sec * t

        if sigma <= 1e-9:
            # No observed volatility: fall back to a near-deterministic call.
            log_diff = math.log(s / k) + mu
            return 1.0 if log_diff > 0 else (0.0 if log_diff < 0 else 0.5)

        z = (math.log(s / k) + mu) / sigma
        return _normal_cdf(z)


class SettlementAwarePredictor:
    """P(yes) that accounts for Kalshi's actual settlement mechanism.

    Kalshi settles these 15-min crypto markets on the *average* of the last
    60 one-second index ticks before close, not the terminal price. An
    average has much lower variance than a point estimate, so treating the
    final minute like a plain terminal-price GBM (as `RandomWalkPredictor`
    does) overstates uncertainty exactly when it matters most.

    Before the averaging window opens, this uses the classic Asian-option
    result Var(time-average of Brownian motion over duration T) = sigma^2*T/3
    to shrink the variance contribution of the window versus a point
    estimate. Once inside the window, it conditions on the ticks already
    observed (`window_avg_so_far`) and only treats the remaining ticks as
    uncertain. It also uses an OLS momentum estimate and a small
    order-book-imbalance tilt, both ignored by `RandomWalkPredictor`.
    """

    def __init__(
        self,
        momentum_weight: float = 0.3,
        imbalance_weight: float = 0.05,
        window_sec: int = SETTLEMENT_WINDOW_SEC,
    ):
        self.momentum_weight = momentum_weight
        self.imbalance_weight = imbalance_weight
        self.window_sec = window_sec

    def predict(self, features: Features) -> float:
        s = features.index_price
        k = features.strike
        t = features.seconds_to_expiry
        w = self.window_sec

        if s <= 0 or k <= 0:
            return 0.5

        mu = self.momentum_weight * features.momentum_ols_per_sec
        if features.book_imbalance is not None:
            # Imbalance nudges the drift slightly; it never dominates vol/momentum.
            mu += self.imbalance_weight * features.book_imbalance * features.realized_vol_per_sqrt_sec

        if t <= 0 or (features.window_ticks_observed >= w and features.window_avg_so_far is not None):
            # Window fully observed (or already closed): settlement is known.
            settlement = features.window_avg_so_far if features.window_avg_so_far is not None else s
            if settlement > k:
                return 1.0
            if settlement < k:
                return 0.0
            return 0.5

        if features.window_ticks_observed > 0 and features.window_avg_so_far is not None:
            # Inside the averaging window: condition on ticks already observed.
            n_remaining = max(w - features.window_ticks_observed, 0)
            t2 = min(t, n_remaining)
            weight_future = n_remaining / w
            mean = (features.window_ticks_observed * features.window_avg_so_far + n_remaining * s) / w
            mean += weight_future * s * (mu * t2)
            var_per_dollar2 = (features.realized_vol_per_sqrt_sec**2) * (t2 / 3.0)
            variance = (weight_future**2) * (s**2) * var_per_dollar2
            std = math.sqrt(max(variance, 0.0))
            if std <= 1e-9:
                return 1.0 if mean > k else (0.0 if mean < k else 0.5)
            return _normal_cdf((mean - k) / std)

        # Not yet inside the averaging window: project to the window's start,
        # then apply the shrunken (sigma^2*T/3) variance for the window itself.
        t1 = max(t - w, 0.0)
        t2 = t - t1
        sigma_sq = features.realized_vol_per_sqrt_sec**2 * (t1 + t2 / 3.0)
        mu_total = mu * (t1 + t2)

        if sigma_sq <= 1e-12:
            log_diff = math.log(s / k) + mu_total
            return 1.0 if log_diff > 0 else (0.0 if log_diff < 0 else 0.5)

        z = (math.log(s / k) + mu_total) / math.sqrt(sigma_sq)
        return _normal_cdf(z)


def market_implied_probability(yes_bid_dollars: float, yes_ask_dollars: float) -> float:
    """Mid-price of the yes side as the market's implied P(yes)."""
    return (yes_bid_dollars + yes_ask_dollars) / 2.0

