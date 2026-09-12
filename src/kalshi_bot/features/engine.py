"""Feature computation from raw index/market ticks."""
from __future__ import annotations

import math
from dataclasses import dataclass

# Kalshi settles these markets on the average of the last 60 one-second RTI
# ticks before expiry (see KXBTC15M/KXSOL15M product metadata).
SETTLEMENT_WINDOW_SEC = 60

# Trailing window for the "short" momentum reading used to confirm decisions -
# independent of the full-lookback momentum, to catch recent reversals.
SHORT_MOMENTUM_WINDOW_SEC = 60


@dataclass(frozen=True)
class Features:
    index_price: float
    strike: float
    seconds_to_expiry: float
    realized_vol_per_sqrt_sec: float  # sigma, per sqrt(second)
    momentum_per_sec: float  # drift estimate, log-return per second (2-point)
    book_imbalance: float | None  # (-1..1), positive = more bid pressure
    momentum_ols_per_sec: float = 0.0  # drift estimate via least-squares slope (full window)
    momentum_short_per_sec: float = 0.0  # same, restricted to the trailing SHORT_MOMENTUM_WINDOW_SEC
    window_ticks_observed: int = 0  # ticks already inside the settlement-averaging window
    window_avg_so_far: float | None = None  # running average of those ticks


def realized_vol_per_sqrt_sec(ticks: list[tuple[int, float]]) -> float:
    """Rolling volatility of log returns, expressed per sqrt(second).

    `ticks` is a list of (ts_ms, value) sorted ascending, ideally covering the
    last few minutes of index observations.
    """
    if len(ticks) < 3:
        return 0.0
    log_returns = []
    dt_seconds = []
    for (t0, v0), (t1, v1) in zip(ticks, ticks[1:]):
        if v0 <= 0 or v1 <= 0:
            continue
        dt = (t1 - t0) / 1000.0
        if dt <= 0:
            continue
        log_returns.append(math.log(v1 / v0))
        dt_seconds.append(dt)
    if len(log_returns) < 2:
        return 0.0
    mean_dt = sum(dt_seconds) / len(dt_seconds)
    mean_r = sum(log_returns) / len(log_returns)
    variance = sum((r - mean_r) ** 2 for r in log_returns) / (len(log_returns) - 1)
    # Normalize the per-tick variance to a per-second rate, then take sqrt.
    if mean_dt <= 0:
        return 0.0
    variance_per_sec = variance / mean_dt
    return math.sqrt(max(variance_per_sec, 0.0))


def momentum_per_sec(ticks: list[tuple[int, float]]) -> float:
    """Average log-return drift per second over the window (simple linear fit)."""
    if len(ticks) < 2:
        return 0.0
    t0_ms, v0 = ticks[0]
    t1_ms, v1 = ticks[-1]
    if v0 <= 0 or v1 <= 0:
        return 0.0
    dt = (t1_ms - t0_ms) / 1000.0
    if dt <= 0:
        return 0.0
    return math.log(v1 / v0) / dt


def momentum_ols_per_sec(ticks: list[tuple[int, float]]) -> float:
    """Least-squares slope of ln(price) vs time (seconds), per second.

    More robust than the 2-point `momentum_per_sec`: uses every tick in the
    window instead of just the endpoints, so a single noisy tick can't
    dominate the drift estimate.
    """
    points = [(t / 1000.0, math.log(v)) for t, v in ticks if v > 0]
    if len(points) < 3:
        return 0.0
    n = len(points)
    mean_t = sum(t for t, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    denom = sum((t - mean_t) ** 2 for t, _ in points)
    if denom <= 0:
        return 0.0
    numer = sum((t - mean_t) * (y - mean_y) for t, y in points)
    return numer / denom


def book_imbalance(yes_bid_size: float, yes_ask_size: float) -> float | None:
    total = yes_bid_size + yes_ask_size
    if total <= 0:
        return None
    return (yes_bid_size - yes_ask_size) / total


def settlement_window_stats(
    ticks: list[tuple[int, float]], close_ts_ms: int, window_sec: int = SETTLEMENT_WINDOW_SEC
) -> tuple[int, float | None]:
    """Ticks observed so far inside the final settlement-averaging window.

    Kalshi's window is `(close_ts - window_sec, close_ts]` (start excluded,
    close included). Returns (ticks_observed, average_so_far).
    """
    window_start_ms = close_ts_ms - window_sec * 1000
    in_window = [v for t, v in ticks if window_start_ms < t <= close_ts_ms]
    if not in_window:
        return 0, None
    return len(in_window), sum(in_window) / len(in_window)


def build_features(
    index_ticks: list[tuple[int, float]],
    strike: float,
    seconds_to_expiry: float,
    yes_bid_size: float | None = None,
    yes_ask_size: float | None = None,
    close_ts_ms: int | None = None,
) -> Features:
    if not index_ticks:
        raise ValueError("index_ticks must not be empty")
    current_price = index_ticks[-1][1]
    imbalance = (
        book_imbalance(yes_bid_size, yes_ask_size)
        if yes_bid_size is not None and yes_ask_size is not None
        else None
    )

    window_ticks_observed, window_avg_so_far = (0, None)
    if close_ts_ms is not None:
        window_ticks_observed, window_avg_so_far = settlement_window_stats(index_ticks, close_ts_ms)

    last_ts_ms = index_ticks[-1][0]
    short_window_start_ms = last_ts_ms - SHORT_MOMENTUM_WINDOW_SEC * 1000
    short_ticks = [t for t in index_ticks if t[0] >= short_window_start_ms]

    return Features(
        index_price=current_price,
        strike=strike,
        seconds_to_expiry=max(seconds_to_expiry, 0.0),
        realized_vol_per_sqrt_sec=realized_vol_per_sqrt_sec(index_ticks),
        momentum_per_sec=momentum_per_sec(index_ticks),
        book_imbalance=imbalance,
        momentum_ols_per_sec=momentum_ols_per_sec(index_ticks),
        momentum_short_per_sec=momentum_ols_per_sec(short_ticks),
        window_ticks_observed=window_ticks_observed,
        window_avg_so_far=window_avg_so_far,
    )
