"""Offline calibration check: replay historical index ticks through the model.

Computes Brier score and log loss vs a naive 0.5 baseline, to validate the
model before trusting its live signals.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..features.engine import build_features
from ..prediction.model import Predictor

_EPS = 1e-6
_LOOKBACK_MS = 5 * 60 * 1000  # trailing window used to estimate vol/momentum


@dataclass(frozen=True)
class BacktestCase:
    """One historical 15-minute market: its index ticks and final outcome."""

    ticker: str
    strike: float
    close_ts_ms: int
    index_ticks: list[tuple[int, float]]  # (ts_ms, value), ascending, spanning the window
    outcome_yes: bool  # whether index_price > strike at close_ts_ms


@dataclass(frozen=True)
class ScoreResult:
    n: int
    brier_score: float
    log_loss: float
    baseline_brier_score: float  # naive p=0.5 for comparison


def _predictions_for_case(case: BacktestCase, predictor: Predictor) -> list[tuple[float, float]]:
    """Return (model_p, outcome) pairs sampled at every tick in the case."""
    pairs: list[tuple[float, float]] = []
    outcome = 1.0 if case.outcome_yes else 0.0
    for i, (ts_ms, _value) in enumerate(case.index_ticks):
        seconds_to_expiry = (case.close_ts_ms - ts_ms) / 1000.0
        if seconds_to_expiry < 0:
            continue
        window_start = ts_ms - _LOOKBACK_MS
        window = [t for t in case.index_ticks[: i + 1] if t[0] >= window_start]
        if len(window) < 2:
            continue
        features = build_features(
            window, strike=case.strike, seconds_to_expiry=seconds_to_expiry, close_ts_ms=case.close_ts_ms
        )
        p = predictor.predict(features)
        pairs.append((p, outcome))
    return pairs


def run_backtest(cases: list[BacktestCase], predictor: Predictor) -> ScoreResult:
    all_pairs: list[tuple[float, float]] = []
    for case in cases:
        all_pairs.extend(_predictions_for_case(case, predictor))

    if not all_pairs:
        return ScoreResult(n=0, brier_score=float("nan"), log_loss=float("nan"), baseline_brier_score=float("nan"))

    n = len(all_pairs)
    brier = sum((p - y) ** 2 for p, y in all_pairs) / n
    baseline_brier = sum((0.5 - y) ** 2 for _p, y in all_pairs) / n
    log_loss = (
        -sum(
            y * math.log(max(p, _EPS)) + (1 - y) * math.log(max(1 - p, _EPS))
            for p, y in all_pairs
        )
        / n
    )
    return ScoreResult(n=n, brier_score=brier, log_loss=log_loss, baseline_brier_score=baseline_brier)
