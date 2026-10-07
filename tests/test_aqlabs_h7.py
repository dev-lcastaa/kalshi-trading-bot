import numpy as np
import pytest

for _mod in ("numpy", "scipy", "duckdb", "pyarrow"):
    pytest.importorskip(_mod)

from aqlabs.costs import taker_fee  # noqa: E402
from aqlabs.research import phase3_h7 as H7  # noqa: E402
from aqlabs.research import replay as R  # noqa: E402

CLOSE = 1_790_003_000
N = 840


def _market(bid, ask, y=1.0, split="val", ticker="T", close=CLOSE, coin="BRTI", asz=10.0, bsz=10.0):
    return dict(ticker=ticker, coin=coin, close_s=close, y=y, day="2026-09-20", split=split,
                s=np.arange(close - N, close), bid=np.full(N, bid), ask=np.full(N, ask), mid=np.full(N, (bid + ask) / 2),
                asz=np.full(N, asz), bsz=np.full(N, bsz), qok=np.ones(N, dtype=bool))


def test_platt_fit_recovers_a_known_favorite_longshot_relationship():
    rng = np.random.default_rng(0)
    markets = []
    for k in range(1500):
        mid = float(rng.choice([0.1, 0.2, 0.35, 0.5, 0.65, 0.8, 0.9, 0.95]))
        true_p = 1 / (1 + np.exp(-(0.1 + 1.2 * np.log(mid / (1 - mid)))))
        markets.append(_market(mid - 0.005, mid + 0.005, y=float(rng.random() < true_p), split="train", ticker=f"M{k}",
                               close=CLOSE + k * 900))
    a, b, n = H7.fit_platt(markets)
    assert b == pytest.approx(1.2, abs=0.15) and a == pytest.approx(0.1, abs=0.1) and n == 1500 * 14
    assert H7.fit_platt([dict(markets[0], split="val")] + markets)[2] == n  # only train rows are used


def test_only_the_favorite_side_at_85c_or_more_with_a_non_negative_after_fee_edge_is_bought_once():
    m = _market(0.89, 0.90)
    trades = H7.run_h7([m], [np.full(N, 0.95)])
    assert len(trades) == 1 and trades[0]["side"] == "yes" and trades[0]["price"] == pytest.approx(0.90)
    assert trades[0]["pnl"] == pytest.approx(1.0 - 0.90 - taker_fee(0.90))  # one entry per market though every second qualifies
    assert H7.run_h7([m], [np.full(N, 0.89)]) == []  # probability below price + fee: no edge
    assert H7.run_h7([_market(0.59, 0.60)], [np.full(N, 0.80)]) == []  # not a favorite price
    assert H7.run_h7([_market(0.59, 0.60)], [np.full(N, 0.80)], min_price=0.0)  # the unrestricted reference form trades


def test_the_no_side_is_the_favorite_when_the_yes_price_is_low():
    m = _market(0.08, 0.09, y=0.0)
    trades = H7.run_h7([m], [np.full(N, 0.02)])  # P(NO) = 0.98 against a 0.92 price
    assert len(trades) == 1 and trades[0]["side"] == "no" and trades[0]["price"] == pytest.approx(0.92)
    assert trades[0]["pnl"] == pytest.approx(1.0 - 0.92 - taker_fee(0.92))


def test_an_unfillable_second_is_skipped_for_the_next_qualifying_one_and_the_window_is_respected():
    m = _market(0.89, 0.90)
    m["asz"][2] = 0.0  # the order arriving at second 2 finds nothing to buy
    trades = H7.run_h7([m], [np.full(N, 0.95)])
    assert len(trades) == 1 and trades[0]["ml"] == pytest.approx((N - 1) / 60)  # the decision at second 1 instead
    late = _market(0.89, 0.90)
    late["qok"][:780] = False  # quotes only for the final 60 s; the model window starts exactly 60 s before close
    only = H7.run_h7([late], [np.full(N, 0.95)])
    assert len(only) == 1 and only[0]["ml"] == pytest.approx(1.0)


def test_calibration_table_reports_win_rate_against_price_by_bin():
    markets = [_market(0.91, 0.92, y=1.0, ticker="A"), _market(0.91, 0.92, y=0.0, ticker="B", close=CLOSE + 900)]
    row = next(r for r in H7.calibration_table(markets) if r["bin"] == "0.90-0.95")
    assert row["n"] == 28 and row["win_rate"] == pytest.approx(0.5) and row["mean_price"] == pytest.approx(0.92)
    assert row["gross_c"] == pytest.approx(-42.0) and row["fee_c"] == pytest.approx(taker_fee(0.92) * 100)
    assert H7.calibration_table([_market(0.49, 0.50)]) == []
