"""Coin-price fair value: where the coin is versus the strike, blended with the Kalshi price.

Kalshi's 15-minute crypto prices lag the underlying index by seconds to a minute.
This model turns "how far the index is from the strike, measured in expected
remaining moves" into a probability, then blends it with the market price using
previously fitted weights. Historical replay of fixed coefficients is not proof
of future profitability; execution costs and independent forward results matter.
"""
from __future__ import annotations

import bisect
import math
from collections.abc import Sequence

# logit(p) = c0 + c1*L(mid) + c2*L(pz) + c3*L(pz)*is_btc + c4*L(mid)*m/15 + c5*L(pz)*m/15,
# where pz = Phi(z) and m = minutes left. Coin information matters more early on.
COEFFICIENTS = (-0.0199, 0.9142, 0.0294, -0.0268, -0.2871, 0.5018)
VOL_LOOKBACK_MIN = 30
MIN_VOL_SAMPLES = 10
MAX_TICK_GAP_MS = 5_000
# The model is undefined inside the final-minute settlement average.
MIN_MINUTES_LEFT = 1.0
HISTORY_MS = (VOL_LOOKBACK_MIN + 1) * 60_000


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _value_at(ts: Sequence[int], values: Sequence[float], at_ms: int) -> tuple[int, float] | None:
    i = bisect.bisect_right(ts, at_ms) - 1
    return (ts[i], values[i]) if i >= 0 else None


def coin_z_score(
    ticks: Sequence[tuple[int, float]], strike: float, close_ts_ms: int, now_ms: int
) -> tuple[float, float] | None:
    """(z, minutes_left): index distance from the strike in standard deviations of the
    remaining move, using 1-minute volatility over the last 30 minutes."""
    if not ticks or not math.isfinite(strike) or strike <= 0:
        return None
    if any(not math.isfinite(ts) or not math.isfinite(value) or value <= 0 for ts, value in ticks):
        return None
    if any(right[0] <= left[0] for left, right in zip(ticks, ticks[1:])):
        return None
    ts = [t for t, _ in ticks]
    values = [v for _, v in ticks]
    latest = _value_at(ts, values, now_ms)
    if latest is None or now_ms - latest[0] > MAX_TICK_GAP_MS or latest[1] <= 0:
        return None
    minutes_left = (close_ts_ms - now_ms) / 60_000
    if not math.isfinite(minutes_left) or not MIN_MINUTES_LEFT <= minutes_left <= 15:
        return None
    returns = []
    for n in range(VOL_LOOKBACK_MIN):
        start_ms = now_ms - 60_000 * (n + 1)
        a = _value_at(ts, values, start_ms)
        b = _value_at(ts, values, now_ms - 60_000 * n)
        if a and b and start_ms - a[0] <= MAX_TICK_GAP_MS and now_ms - 60_000 * n - b[0] <= MAX_TICK_GAP_MS:
            returns.append(math.log(b[1] / a[1]))
    if len(returns) < MIN_VOL_SAMPLES:
        return None
    sigma = math.sqrt(sum(r * r for r in returns) / len(returns))
    # Settlement is the 60s average, so the last minute contributes a third of its variance.
    sd = latest[1] * sigma * math.sqrt(minutes_left - 1 + 1 / 3)
    if sd <= 0:
        return None
    return (latest[1] - strike) / sd, minutes_left


def fair_value_p_yes(market_p: float, z: float, minutes_left: float, is_btc: bool) -> float:
    if (not all(math.isfinite(value) for value in (market_p, z, minutes_left))
            or not 0 <= market_p <= 1 or not MIN_MINUTES_LEFT <= minutes_left <= 15):
        raise ValueError("Fair-value inputs must be finite probabilities and a 1-15 minute horizon")
    lm, lz = _logit(market_p), _logit(_phi(z))
    m = min(minutes_left, 15.0) / 15
    c0, c1, c2, c3, c4, c5 = COEFFICIENTS
    x = c0 + c1 * lm + c2 * lz + c3 * lz * is_btc + c4 * lm * m + c5 * lz * m
    return 1 / (1 + math.exp(-x))


def fair_value_from_ticks(
    ticks: Sequence[tuple[int, float]], strike: float, close_ts_ms: int, now_ms: int,
    market_p: float, index_id: str,
) -> float | None:
    scored = coin_z_score(ticks, strike, close_ts_ms, now_ms)
    if scored is None:
        return None
    z, minutes_left = scored
    return fair_value_p_yes(market_p, z, minutes_left, index_id == "BRTI")
