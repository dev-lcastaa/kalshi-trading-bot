import numpy as np
import pytest

pytest.importorskip("duckdb")
pytest.importorskip("scipy")

from aqlabs.research import replay as R  # noqa: E402
from aqlabs.costs import taker_fee  # noqa: E402
from kalshi_bot.prediction.fair_value import coin_z_score, fair_value_from_ticks  # noqa: E402


def _walk(seconds, start_s, seed=1):
    rng = np.random.default_rng(seed)
    px = 100.0 * np.exp(np.cumsum(rng.normal(0, 3e-5, seconds)))
    ts = (start_s + np.arange(seconds)) * 1000
    return ts.astype(np.int64), px


def _market(close_s, strike, bid=0.45, ask=0.47, y=1.0, coin="BRTI"):
    q_ts = (np.arange(close_s - 900, close_s) * 1000).astype(float)
    n = len(q_ts)
    q = np.column_stack([q_ts, np.full(n, bid), np.full(n, ask), np.full(n, 10.0), np.full(n, 10.0)])
    m = dict(ticker="T", coin=coin, strike=strike, close_s=close_s, y=y, day="2026-09-20", split="train", q=q)
    R.quote_arrays(m)
    return m


def test_vectorised_fair_value_matches_the_live_bots_scalar_function():
    start_s = 1_790_000_000
    ts, px = _walk(3000, start_s)
    close_s = start_s + 2900
    grid = R.build_coin_grid(ts, px)
    m = _market(close_s, strike=float(px[2300]))
    z, ok, ml = R.signal_arrays(grid, m)
    prob = R.frozen_prob()(m, z, ml)
    ticks = list(zip(ts.tolist(), px.tolist()))
    checked = 0
    for i in range(0, 600, 37):
        if not ok[i]:
            continue
        now_ms = int(m["s"][i]) * 1000
        window = [t for t in ticks if now_ms - 31 * 60_000 <= t[0] <= now_ms]
        ref_z = coin_z_score(window, m["strike"], close_s * 1000, now_ms)
        ref_p = fair_value_from_ticks(window, m["strike"], close_s * 1000, now_ms, float(m["mid"][i]), "BRTI")
        assert ref_z[0] == pytest.approx(z[i], abs=1e-6)
        assert ref_p == pytest.approx(prob[i], abs=1e-6)
        checked += 1
    assert checked >= 5


def test_run_fair_value_pnl_is_settlement_minus_ask_minus_fee_and_respects_entry_rules():
    close_s = 1_790_003_000
    m = _market(close_s, strike=100.0, bid=0.45, ask=0.50, y=1.0)
    z = np.zeros(len(m["s"]))
    ones = np.ones(len(m["s"]), dtype=bool)
    ml = (close_s - m["s"]) / 60.0
    always_yes = lambda market, zz, mm: np.full(len(zz), 0.9)
    trades, st = R.run_fair_value([m], [(z, ones, ml)], always_yes, thr=0.03, delay=2, gap=60, max_entries=5)
    assert len(trades) == 5  # max buys, spaced at least 60 seconds apart
    expected = 1.0 - 0.50 - taker_fee(0.50)
    assert all(t["pnl"] == pytest.approx(expected) for t in trades)
    assert all(t["gross"] == pytest.approx(0.50) and t["fee"] == taker_fee(0.50) for t in trades)
    assert all(4.0 <= t["ml"] <= 14.0 for t in trades)


def test_run_fair_value_abstains_when_edge_does_not_cover_the_fee():
    close_s = 1_790_003_000
    m = _market(close_s, strike=100.0, bid=0.49, ask=0.51, y=1.0)
    ones = np.ones(len(m["s"]), dtype=bool)
    ml = (close_s - m["s"]) / 60.0
    p = lambda market, zz, mm: np.full(len(zz), 0.52)  # 1c gross edge, fee is 2c
    trades, _ = R.run_fair_value([m], [(np.zeros(len(ml)), ones, ml)], p, thr=0.0)
    assert trades == []


def test_run_fair_value_never_trades_without_a_valid_quote():
    close_s = 1_790_003_000
    m = _market(close_s, strike=100.0)
    m["qok"][:] = False
    ones = np.ones(len(m["s"]), dtype=bool)
    ml = (close_s - m["s"]) / 60.0
    trades, _ = R.run_fair_value([m], [(np.zeros(len(ml)), ones, ml)], lambda *_: np.full(len(ml), 0.95), thr=0.0)
    assert trades == []


def test_split_assignment_reserves_the_holdout():
    assert R.split_of("2026-09-20") == "train"
    assert R.split_of("2026-09-28") == "val"
    assert R.split_of("2026-10-03") == "test"
    assert R.split_of("2026-10-06") == "holdout"
    assert R.split_of("2026-09-12") == "x"
