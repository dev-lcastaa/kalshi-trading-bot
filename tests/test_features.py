import math

from kalshi_bot.features.engine import (
    book_imbalance,
    build_features,
    momentum_ols_per_sec,
    momentum_per_sec,
    realized_vol_per_sqrt_sec,
    settlement_window_stats,
)


def test_realized_vol_zero_for_constant_price():
    ticks = [(i * 1000, 100.0) for i in range(10)]
    assert realized_vol_per_sqrt_sec(ticks) == 0.0


def test_realized_vol_positive_for_noisy_price():
    ticks = [(i * 1000, 100.0 + (5 if i % 2 == 0 else -5)) for i in range(20)]
    assert realized_vol_per_sqrt_sec(ticks) > 0.0


def test_momentum_positive_for_uptrend():
    ticks = [(i * 1000, 100.0 * (1.001**i)) for i in range(10)]
    assert momentum_per_sec(ticks) > 0.0


def test_momentum_negative_for_downtrend():
    ticks = [(i * 1000, 100.0 * (0.999**i)) for i in range(10)]
    assert momentum_per_sec(ticks) < 0.0


def test_book_imbalance_bounds():
    assert book_imbalance(100, 0) == 1.0
    assert book_imbalance(0, 100) == -1.0
    assert book_imbalance(0, 0) is None


def test_build_features_uses_latest_price_as_index_price():
    ticks = [(0, 100.0), (1000, 101.0), (2000, 102.0)]
    features = build_features(ticks, strike=100.0, seconds_to_expiry=60.0)
    assert features.index_price == 102.0


def test_momentum_ols_more_robust_than_2point_to_endpoint_noise():
    # A clean uptrend with one noisy final tick: with enough points, OLS uses
    # the whole trend so a single endpoint outlier can't flip its sign, unlike
    # a naive 2-point estimate which depends only on the first and last tick.
    ticks = [(i * 1000, 100.0 * (1.001**i)) for i in range(30)]
    ticks[-1] = (ticks[-1][0], ticks[-1][1] * 0.9)  # noisy outlier drags the endpoint down
    assert momentum_per_sec(ticks) < 0.0  # 2-point estimate is flipped by the outlier
    assert momentum_ols_per_sec(ticks) > 0.0  # OLS still sees the underlying uptrend


def test_momentum_ols_requires_at_least_three_points():
    assert momentum_ols_per_sec([(0, 100.0), (1000, 101.0)]) == 0.0


def test_settlement_window_stats_empty_before_window_opens():
    ticks = [(i * 1000, 100.0) for i in range(10)]
    close_ts_ms = 120_000  # window opens at 60_000, no ticks reach that far
    k, avg = settlement_window_stats(ticks, close_ts_ms)
    assert k == 0
    assert avg is None


def test_settlement_window_stats_averages_ticks_in_window():
    # Ticks every second from t=0..119s; close at t=119s -> window is (59s, 119s].
    ticks = [(i * 1000, float(i)) for i in range(120)]
    close_ts_ms = 119_000
    k, avg = settlement_window_stats(ticks, close_ts_ms)
    assert k == 60
    assert avg == sum(range(60, 120)) / 60


def test_build_features_populates_window_stats_when_close_ts_given():
    ticks = [(i * 1000, 100.0 + i * 0.01) for i in range(70)]
    close_ts_ms = 69_000
    features = build_features(ticks, strike=100.0, seconds_to_expiry=0.0, close_ts_ms=close_ts_ms)
    assert features.window_ticks_observed == 60
    assert features.window_avg_so_far is not None


def test_build_features_window_stats_default_when_close_ts_omitted():
    ticks = [(0, 100.0), (1000, 101.0), (2000, 102.0)]
    features = build_features(ticks, strike=100.0, seconds_to_expiry=60.0)
    assert features.window_ticks_observed == 0
    assert features.window_avg_so_far is None
    assert math.isclose(features.strike, 100.0)


def test_momentum_short_per_sec_only_uses_trailing_window():
    # Flat for the first 5 minutes, then a clear uptrend in the last 60s.
    flat = [(i * 1000, 100.0) for i in range(240)]
    uptrend = [((240 + i) * 1000, 100.0 * (1.001**i)) for i in range(61)]
    ticks = flat + uptrend
    features = build_features(ticks, strike=100.0, seconds_to_expiry=60.0)
    assert features.momentum_short_per_sec > 0.0
    # Full-window OLS momentum is much smaller since it's dominated by the flat period.
    assert features.momentum_short_per_sec > features.momentum_ols_per_sec
