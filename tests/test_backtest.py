import random
from dataclasses import asdict

import pytest

from kalshi_bot.backtest.runner import (
    BacktestCase,
    evaluate_snapshots,
    grid_search_market_blend_weight,
    run_backtest,
)
from kalshi_bot.features.engine import build_features
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


def _snapshot(ticker="BTC", close=600000, result="yes", experiment="test"):
    ticks = [(second * 1000, 100 + second * 0.001) for second in range(301)]
    return {
        "ticker": ticker, "experiment_id": experiment, "index_id": ticker,
        "ts_ms": 300000, "close_ts_ms": close, "result": result,
        "snapshot": {
            "features": asdict(build_features(ticks, 100, (close - 300000) / 1000)),
            "index_ticks": ticks, "parameters": {"edge_threshold": 0.05},
            "quotes": {"yes_bid_dollars": 0.4, "yes_ask_dollars": 0.6, "ts_ms": 299000},
            "live": {"model_p_yes": 0.8, "recommendation": "BUY_YES"},
            "shadow": {"model_p_yes": 0.3, "recommendation": "BUY_NO"},
            "market_p_yes": 0.5,
        },
    }


def test_snapshot_evaluator_scores_decisions_and_purchase_prices():
    result = evaluate_snapshots([_snapshot(), _snapshot(close=900000, experiment="other")], RandomWalkPredictor())
    assert len(result["groups"]) == 2
    scores = result["groups"][0]["scores"]["all"]
    assert scores["live"]["n"] == 1
    assert scores["live"]["brier"] == pytest.approx(0.04)
    assert scores["live"]["ask_gross_pnl"] == pytest.approx(0.4)
    assert scores["recorded_shadow"]["ask_gross_pnl"] == pytest.approx(-0.6)
    assert scores["market"]["trades"] == 0


def test_snapshot_evaluator_keeps_paired_windows_together_and_excludes_duplicates():
    rows = [_snapshot(ticker=coin, close=close) for coin in ("BTC", "SOL") for close in (600000, 900000)]
    rows[1]["ticker"] = "BTC-LATER"
    rows[3]["ticker"] = "SOL-LATER"
    result = evaluate_snapshots(rows + [rows[0]], RandomWalkPredictor())
    assert result["excluded_rows"] == 1
    for group in result["groups"]:
        assert group["split_close_ts_ms"] == 900000
        assert group["scores"]["earlier"]["live"]["n"] == 1
        assert group["scores"]["later"]["live"]["n"] == 1


@pytest.mark.parametrize("defect", ["pending", "future_tick", "future_quote", "closed", "horizon", "crossed"])
def test_snapshot_evaluator_excludes_invalid_or_future_inputs(defect):
    row = _snapshot()
    if defect == "pending":
        row["result"] = None
    elif defect == "future_tick":
        row["snapshot"]["index_ticks"].append((301000, 100))
    elif defect == "future_quote":
        row["snapshot"]["quotes"]["ts_ms"] = 301000
    elif defect == "closed":
        row["ts_ms"] = row["close_ts_ms"]
    elif defect == "horizon":
        row["snapshot"]["features"]["seconds_to_expiry"] = 0
    else:
        row["snapshot"]["quotes"]["yes_bid_dollars"] = 0.9
    result = evaluate_snapshots([row], RandomWalkPredictor())
    assert result["excluded_rows"] == 1
    assert result["groups"] == []


def test_snapshot_evaluator_handles_empty_input():
    assert evaluate_snapshots([], RandomWalkPredictor())["groups"] == []


def test_market_blend_weight_shifts_candidate_probability_toward_market():
    # market_p_yes=0.5 (mid of 0.4/0.6 quotes); a fully model-driven candidate and a
    # fully market-driven one should score the row differently since the row's
    # actual model probability isn't 0.5.
    row = _snapshot()
    pure_model = evaluate_snapshots([row], RandomWalkPredictor(), market_blend_weight=0.0)
    pure_market = evaluate_snapshots([row], RandomWalkPredictor(), market_blend_weight=1.0)
    assert pure_model["market_blend_weight"] == 0.0
    assert pure_market["market_blend_weight"] == 1.0
    model_score = pure_model["groups"][0]["scores"]["all"]["candidate"]
    market_score = pure_market["groups"][0]["scores"]["all"]["candidate"]
    assert model_score["n"] == market_score["n"] == 1
    assert model_score["brier"] != market_score["brier"]


def test_grid_search_market_blend_weight_ranks_by_combined_brier():
    rows = [_snapshot(ticker="BTC"), _snapshot(ticker="SOL", result="no")]

    results = grid_search_market_blend_weight(rows, RandomWalkPredictor, weights=(0.0, 0.5, 1.0))

    assert [r["market_blend_weight"] for r in results] == sorted(
        (0.0, 0.5, 1.0), key=lambda w: next(r["combined_brier"] for r in results if r["market_blend_weight"] == w)
    )
    assert all(r["n"] == 2 for r in results)
    assert all(r["combined_brier"] is not None for r in results)
    assert set(results[0]["per_coin"]) == {"BTC", "SOL"}


def test_grid_search_market_blend_weight_handles_no_scorable_rows():
    results = grid_search_market_blend_weight([], RandomWalkPredictor, weights=(0.0, 1.0))
    assert [r["combined_brier"] for r in results] == [None, None]

