"""Logistic-regression "stacking" model: learns to combine the rule-based
predictor's own output with a few raw signals (momentum, order-book
imbalance, volatility, settlement-window progress) into a single
probability, fit by gradient descent on the bot's own settled-decision
history.

This is the real "online learning" piece referenced in the isotonic
calibrator: unlike calibration (a monotonic correction of one number),
this is an actual trainable classifier with weighted inputs, refit
periodically as more settled markets accumulate. It runs only as a shadow
challenger until there's enough history to trust it (see `min_samples`);
until then it silently falls back to the wrapped base predictor's raw
output, so it's always safe to wire in ahead of having real data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..features.engine import SETTLEMENT_WINDOW_SEC, Features
from .model import Predictor

# Order matters: this is the exact column order used for both training and
# live prediction (see `extract_feature_vector`).
FEATURE_NAMES = [
    "base_model_p",
    "momentum_ols_scaled",
    "momentum_short_scaled",
    "book_imbalance",
    "realized_vol",
    "window_progress",
    "whale_net_flow_usd",
    "external_price_divergence",
]


def extract_feature_vector(features: Features, base_model_p: float) -> list[float]:
    """Build the logistic model's input row from `Features` plus the base
    predictor's own probability (features it can't cheaply relearn on its
    own from ~thousands of rows, like the settlement-window math)."""
    t = max(features.seconds_to_expiry, 0.0)
    time_scale = math.sqrt(t + 1.0)
    return [
        base_model_p,
        features.momentum_ols_per_sec * time_scale,
        features.momentum_short_per_sec * time_scale,
        features.book_imbalance if features.book_imbalance is not None else 0.0,
        features.realized_vol_per_sqrt_sec,
        features.window_ticks_observed / SETTLEMENT_WINDOW_SEC,
        features.whale_net_flow_usd,
        features.external_price_divergence,
    ]


def _sigmoid(z: float) -> float:
    z = max(min(z, 700.0), -700.0)
    return 1.0 / (1.0 + math.exp(-z))


@dataclass
class LogisticRegressionModel:
    """Standardized-feature logistic regression, fit via full-batch gradient
    descent with L2 regularization. No numpy dependency - the feature count
    (6) and row count (thousands, not millions) make plain Python fast enough.
    """

    feature_names: list[str] = field(default_factory=lambda: list(FEATURE_NAMES))
    min_samples: int = 300
    l2: float = 1.0
    learning_rate: float = 0.5
    epochs: int = 400
    weights: list[float] = field(default_factory=list)
    intercept: float = 0.0
    feature_means: list[float] = field(default_factory=list)
    feature_stds: list[float] = field(default_factory=list)
    fitted_n: int = 0

    @property
    def is_fitted(self) -> bool:
        return bool(self.weights)

    def fit(self, rows: list[tuple[list[float], float]]) -> None:
        """Refit from scratch on `rows` of (feature_vector, outcome in {0, 1})."""
        n_features = len(self.feature_names)
        clean = [
            (x, y)
            for x, y in rows
            if y in (0.0, 1.0) and len(x) == n_features and all(v == v for v in x)  # v == v filters NaN
        ]
        self.fitted_n = len(clean)
        if len(clean) < self.min_samples:
            self.weights = []
            self.intercept = 0.0
            return

        means = [0.0] * n_features
        for x, _ in clean:
            for i, v in enumerate(x):
                means[i] += v
        means = [m / len(clean) for m in means]

        stds = [0.0] * n_features
        for x, _ in clean:
            for i, v in enumerate(x):
                stds[i] += (v - means[i]) ** 2
        stds = [math.sqrt(s / len(clean)) for s in stds]
        stds = [s if s > 1e-9 else 1.0 for s in stds]

        standardized = [
            ([(v - means[i]) / stds[i] for i, v in enumerate(x)], y)
            for x, y in clean
        ]

        weights = [0.0] * n_features
        intercept = 0.0
        n = len(standardized)
        for _ in range(self.epochs):
            grad_w = [0.0] * n_features
            grad_b = 0.0
            for x, y in standardized:
                p = _sigmoid(intercept + sum(w * v for w, v in zip(weights, x)))
                error = p - y
                for i, v in enumerate(x):
                    grad_w[i] += error * v
                grad_b += error
            for i in range(n_features):
                grad_w[i] = grad_w[i] / n + self.l2 * weights[i] / n
                weights[i] -= self.learning_rate * grad_w[i]
            intercept -= self.learning_rate * (grad_b / n)

        self.weights = weights
        self.intercept = intercept
        self.feature_means = means
        self.feature_stds = stds

    def predict_proba(self, x: list[float]) -> float:
        if not self.is_fitted:
            raise RuntimeError("LogisticRegressionModel.predict_proba called before fit()")
        z = self.intercept + sum(
            w * ((v - m) / s)
            for w, v, m, s in zip(self.weights, x, self.feature_means, self.feature_stds)
        )
        return _sigmoid(z)


class LogisticSignalPredictor:
    """Predictor that defers to a `LogisticRegressionModel`, falling back to
    the wrapped base predictor's raw output until the model has fit on
    `min_samples` settled decisions.
    """

    def __init__(self, base_predictor: Predictor, model: LogisticRegressionModel):
        self.base_predictor = base_predictor
        self.model = model

    def predict(self, features: Features) -> float:
        base_p = self.base_predictor.predict(features)
        if not self.model.is_fitted:
            return base_p
        return self.model.predict_proba(extract_feature_vector(features, base_p))

    def __getattr__(self, name: str):
        return getattr(self.base_predictor, name)


def fit_logistic_model(
    model: LogisticRegressionModel,
    rows: list[tuple[dict, float]],
    base_predictor: Predictor,
) -> None:
    """Fit `model` from raw (features_dict, outcome) pairs, e.g. as returned
    by `Store.decision_feature_outcome_pairs` - `features_dict` is the
    `asdict(Features)` blob recorded in each decision's snapshot.
    """
    training_rows: list[tuple[list[float], float]] = []
    for features_dict, y in rows:
        try:
            features = Features(**features_dict)
        except TypeError:
            continue
        base_p = base_predictor.predict(features)
        training_rows.append((extract_feature_vector(features, base_p), y))
    model.fit(training_rows)
