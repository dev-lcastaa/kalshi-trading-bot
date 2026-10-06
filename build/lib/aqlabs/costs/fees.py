"""Kalshi taker fee: ceil(multiplier * 0.07 * P * (1 - P) * 100) / 100 dollars per contract.

Matches `kalshi_bot.signals.generator.kalshi_taker_fee_per_contract` (verified by a test). The
fee is quadratic in price, so it is about 2c near 50c and about 1c at 85c or more.
"""
from __future__ import annotations

import math

import numpy as np

_EPS = 1e-9  # guards float noise such as 0.07 * 0.5 * 0.5 * 100 = 1.7500000000000002


def taker_fee(price: float, multiplier: float = 1.0) -> float:
    if not 0 <= price <= 1 or multiplier < 0:
        raise ValueError("price must be in [0, 1] and multiplier must be non-negative")
    return math.ceil(multiplier * 0.07 * price * (1 - price) * 100 - _EPS) / 100


def taker_fee_array(price, multiplier: float = 1.0) -> np.ndarray:
    p = np.asarray(price, dtype=float)
    return np.ceil(multiplier * 0.07 * p * (1 - p) * 100 - _EPS) / 100
