import random

from kalshi_bot.backtest.runner import BacktestCase, run_backtest
from kalshi_bot.prediction.model import RandomWalkPredictor, SettlementAwarePredictor


def _make_case(ticker: str, start_price: float, strike: float, drift_per_tick: float, seed: int) -> BacktestCase:
    rng = random.Random(seed)
    ticks = []
    price = start_price
    start_ms = 0
    for i in range(90):  # 90 ticks over 15 minutes, 10s apart
        ts_ms = start_ms + i * 10_000
        price *= 1 + drift_per_tick + rng.uniform(-0.0002, 0.0002)
        ticks.append((ts_ms, price))
    close_ts_ms = ticks[-1][0]
    outcome_yes = ticks[-1][1] > strike
    return BacktestCase(
        ticker=ticker, strike=strike, close_ts_ms=close_ts_ms, index_ticks=ticks, outcome_yes=outcome_yes
    )


def _make_fine_grained_case(
    ticker: str, start_price: float, strike: float, drift_per_tick: float, seed: int
) -> BacktestCase:
    """1-second ticks over 15 minutes, so the 60-tick settlement window actually fills."""
    rng = random.Random(seed)
    ticks = []
    price = start_price
    for i in range(900):
        ts_ms = i * 1000
        price *= 1 + drift_per_tick + rng.uniform(-0.0002, 0.0002)
        ticks.append((ts_ms, price))
    close_ts_ms = ticks[-1][0]
    settlement = sum(v for _t, v in ticks[-60:]) / 60
    return BacktestCase(
        ticker=ticker, strike=strike, close_ts_ms=close_ts_ms, index_ticks=ticks, outcome_yes=settlement > strike
    )



def test_backtest_beats_naive_baseline_on_trending_cases():
    cases = [
        _make_case(f"CASE-{i}", start_price=100.0, strike=100.0, drift_per_tick=0.002, seed=i)
        for i in range(20)
    ] + [
        _make_case(f"CASE-DOWN-{i}", start_price=100.0, strike=100.0, drift_per_tick=-0.002, seed=100 + i)
        for i in range(20)
    ]
    predictor = RandomWalkPredictor()

    result = run_backtest(cases, predictor)

    assert result.n > 0
    assert result.brier_score <= result.baseline_brier_score


def test_backtest_handles_empty_cases():
    result = run_backtest([], RandomWalkPredictor())
    assert result.n == 0


def test_v2_settlement_aware_predictor_is_not_worse_than_v1():
    """Regression guard: v2 must not degrade calibration vs v1 on the same cases."""
    cases = [
        _make_fine_grained_case(f"FINE-UP-{i}", start_price=100.0, strike=100.0, drift_per_tick=0.0015, seed=i)
        for i in range(10)
    ] + [
        _make_fine_grained_case(f"FINE-DOWN-{i}", start_price=100.0, strike=100.0, drift_per_tick=-0.0015, seed=100 + i)
        for i in range(10)
    ] + [
        _make_fine_grained_case(f"FINE-FLAT-{i}", start_price=100.0, strike=100.0, drift_per_tick=0.0, seed=200 + i)
        for i in range(10)
    ]

    v1_result = run_backtest(cases, RandomWalkPredictor())
    v2_result = run_backtest(cases, SettlementAwarePredictor())

    assert v2_result.n > 0
    # Allow a small tolerance for noise, but v2 shouldn't be meaningfully worse.
    assert v2_result.brier_score <= v1_result.brier_score * 1.05

