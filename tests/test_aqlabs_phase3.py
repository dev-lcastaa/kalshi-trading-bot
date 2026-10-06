import numpy as np
import pytest

for _mod in ("numpy", "scipy", "duckdb", "pyarrow"):
    pytest.importorskip(_mod)

from aqlabs.research import phase3 as P3  # noqa: E402
from aqlabs.research import registry as REG  # noqa: E402
from aqlabs.research import replay as R  # noqa: E402


# ------------------------------------------------------------------ H3: final-minute settlement average
def _final_minute_setup(strike, sigma_1min=0.0003, level=100.0, close=1000):
    grid = {"s0": 0, "V": np.full(close, level), "valid": np.ones(close, dtype=bool),
            "sigma": np.full(close, sigma_1min)}
    m = {"s": np.arange(close - 840, close), "close_s": close, "strike": strike}
    return grid, m


def test_final_minute_probability_matches_a_monte_carlo_simulation():
    grid, m = _final_minute_setup(strike=100.005)
    p, ok = P3.final_minute_p(grid, m)
    q = 29  # 30 seconds observed (all at 100.0), r = 30 still to come
    i = 840 - 60 + q
    assert ok[i]
    rng = np.random.default_rng(1)
    r = 59 - q
    sigma_p = 100.0 * 0.0003 / np.sqrt(60.0)
    future = 100.0 + sigma_p * np.cumsum(rng.standard_normal((200_000, r)), axis=1)
    settle = (30 * 100.0 + future.sum(axis=1)) / 60.0
    assert p[i] == pytest.approx(float(np.mean(settle >= 100.005)), abs=0.01)


def test_final_minute_probability_becomes_certain_as_the_average_locks_in():
    above, m = _final_minute_setup(strike=99.9999)  # the settlement average (100.0) sits just above the strike
    p, _ = P3.final_minute_p(above, m)
    assert p[839] == pytest.approx(P3.H3_CLIP[1])  # no time left: S is known, clipped to the ceiling
    assert 0.5 < p[780 + 5] < p[839]  # more certain late in the minute than early
    below, m2 = _final_minute_setup(strike=100.0001)  # just below the strike: the mirror image
    p2, _ = P3.final_minute_p(below, m2)
    assert p2[839] == pytest.approx(P3.H3_CLIP[0])
    assert p2[839] < p2[780 + 5] < 0.5
    far, m3 = _final_minute_setup(strike=100.5)
    assert P3.final_minute_p(far, m3)[0][839] == pytest.approx(P3.H3_CLIP[0])


def test_final_minute_needs_every_observed_second_to_be_present_and_stays_off_before_the_last_minute():
    grid, m = _final_minute_setup(strike=100.0)
    grid["valid"][1000 - 60 + 10] = False  # one missing second inside the minute
    p, ok = P3.final_minute_p(grid, m)
    assert not ok[840 - 60 + 9 + 2:].any() and ok[840 - 60 + 0]
    assert not ok[:780].any() and np.isnan(p[:780]).all()


def test_final_minute_rule_buys_the_mispriced_side_once_and_pays_fee_and_price():
    grid, m0 = _final_minute_setup(strike=99.99)  # average well above strike: YES is almost certain
    close = 1000
    n = len(m0["s"])
    q = np.zeros((n, 7))
    m = dict(m0, ticker="T", coin="BRTI", y=1.0, day="2026-09-20", split="val",
             qok=np.ones(n, dtype=bool), bid=np.full(n, 0.90), ask=np.full(n, 0.92), bsz=np.full(n, 5.0),
             asz=np.full(n, 5.0), q=q)
    p, ok = P3.final_minute_p(grid, m)
    trades = P3.run_final_minute([m], [(p, ok)], thr=0.02, delay=2)
    assert len(trades) == 1 and trades[0]["side"] == "yes"
    assert trades[0]["pnl"] == pytest.approx(1.0 - 0.92 - float(P3.fee(0.92)))
    assert 10 <= trades[0]["ml"] * 60 <= 55
    m["qok"][:] = False
    assert P3.run_final_minute([m], [(p, ok)]) == []


# ------------------------------------------------------------------ H1: lead-lag
def _ticks(prices, t0_s):
    return (t0_s + np.arange(len(prices))) * 1000, np.asarray(prices, float)


def test_exchange_basis_predicts_the_index_when_the_index_lags_the_exchange():
    rng = np.random.default_rng(3)
    days, t0 = 3, P3.day_number("2026-09-24") * 86400
    n = days * 86400
    cb = 100.0 * np.exp(np.cumsum(rng.normal(0, 1e-4, n + 5)))
    idx = cb[:n]  # the index shows the exchange price 5 seconds late
    cb_live = cb[5:n + 5]
    g_cb = R.build_coin_grid(*_ticks(cb_live, t0))
    g_idx = R.build_coin_grid(*_ticks(idx, t0))
    basis = P3.basis_series(g_idx, [g_cb])
    out = P3.h1_measurement({("idx", "BRTI"): g_idx}, {"BRTI": basis}, "train", ("val",))["BRTI"]
    assert out["beta"][0] > 0.5 and out["t"][0] > 10  # the basis carries the future index move
    assert out["r2"]["val"] > 0.2  # and it holds out of sample (day 3 is in the val split)


def test_basis_is_nan_where_no_exchange_is_fresh_and_averages_the_ones_that_are():
    g_idx = {"s0": 0, "V": np.full(10, 100.0), "valid": np.ones(10, dtype=bool)}
    a = {"s0": 0, "V": np.full(10, 101.0), "valid": np.array([1] * 5 + [0] * 5, dtype=bool)}
    b = {"s0": 0, "V": np.full(10, 102.0), "valid": np.array([0] * 3 + [1] * 7, dtype=bool)}
    out = P3.basis_series(g_idx, [a, b])
    assert out[0] == pytest.approx(np.log(1.01)) and out[4] == pytest.approx((np.log(1.01) + np.log(1.02)) / 2)
    assert out[7] == pytest.approx(np.log(1.02))
    only_a = {"s0": 0, "V": np.full(10, 101.0), "valid": np.array([0] * 10, dtype=bool)}
    assert np.isnan(P3.basis_series(g_idx, [only_a])).all()


def test_adjusted_grid_applies_beta_times_basis_and_the_placebo_lag_shifts_it():
    grid = {"s0": 0, "V": np.full(6, 100.0), "valid": np.ones(6, dtype=bool), "sigma": np.full(6, 1e-4)}
    basis = np.array([0.01, 0.02, np.nan, 0.0, 0.0, 0.0])
    adj = P3.adjusted_grid(grid, basis, beta=2.0)
    assert adj["V"][0] == pytest.approx(100.0 * np.exp(0.02)) and adj["V"][2] == pytest.approx(100.0)
    stale = P3.adjusted_grid(grid, basis, beta=2.0, lag_s=1)
    assert stale["V"][1] == pytest.approx(100.0 * np.exp(0.02)) and stale["V"][0] == pytest.approx(100.0)


def test_clustered_ols_recovers_coefficients_and_reports_a_small_se_for_real_signal():
    rng = np.random.default_rng(0)
    day = np.repeat(np.arange(10), 500)
    X = rng.normal(size=(5000, 2))
    y = X @ np.array([0.7, -0.2]) + rng.normal(0, 0.5, 5000)
    beta, se = P3.ols_clustered(X, y, day)
    assert beta == pytest.approx([0.7, -0.2], abs=0.05) and (np.abs(beta / se) > 5).all()
    assert P3.oos_r2(X, y, beta) > 0.3 and P3.oos_r2(X, y, np.zeros(2)) == pytest.approx(0.0)


# ------------------------------------------------------------------ H2: faster volatility
def test_ewma_volatility_reacts_to_recent_calm_but_the_flat_estimate_remembers_old_turbulence():
    rng = np.random.default_rng(5)
    minutes = 40
    scale = np.where(np.arange(minutes * 60) < 20 * 60, 4e-4, 1e-4)  # turbulent first half, calm second half
    price = 100.0 * np.exp(np.cumsum(rng.normal(0, 1, minutes * 60) * scale))
    ts, val = _ticks(price, 1_790_000_000)
    flat = R.build_coin_grid(ts, val)["sigma"][-1]
    fast = R.build_coin_grid(ts, val, half_life_min=8.0)["sigma"][-1]
    assert fast < flat
    # with no half-life the weighting must equal the original flat estimate
    assert R.build_coin_grid(ts, val, half_life_min=None)["sigma"][-1] == flat


# ------------------------------------------------------------------ stage rules and registry
def _trades(mean, n, split, day="2026-09-27", seed=0):
    rng = np.random.default_rng(seed)
    return [dict(pnl=float(mean + rng.normal(0, 0.05)), market=f"{split}{i}", day=day, split=split) for i in range(n)]


def test_screen_requires_every_criterion():
    good = _trades(0.02, 200, "val") + _trades(0.015, 200, "test", day="2026-10-03", seed=1)
    placebo = _trades(-0.02, 100, "val", seed=2)
    assert P3.screen_check(good, placebo)["passed"]
    assert not P3.screen_check(_trades(0.02, 100, "val") + _trades(0.02, 100, "test"), placebo)["passed"]  # n < 300
    assert not P3.screen_check(good, _trades(0.01, 100, "val"))["passed"]  # placebo made money
    tiny = _trades(0.002, 200, "val") + _trades(0.002, 200, "test", day="2026-10-03")
    assert not P3.screen_check(tiny, placebo)["criteria"]["pooled_mean_ge_0.5c"]
    one_sided = _trades(0.04, 200, "val") + _trades(-0.001, 200, "test", day="2026-10-03")
    assert not P3.screen_check(one_sided, placebo)["criteria"]["test_mean_gt_0"]


def test_validation_needs_a_positive_lower_bound_both_halves_and_survival_under_delay():
    days = [f"2026-10-{d:02d}" for d in range(6, 20)]
    trades = [t for i, d in enumerate(days) for t in _trades(0.03, 40, "fwd", day=d, seed=i)]
    placebo = _trades(-0.01, 100, "fwd")
    ok = P3.validation_check(trades, placebo, trades)
    assert ok["passed"] and ok["ci90_c"][0] > 0
    assert not P3.validation_check(trades, placebo, _trades(-0.01, 50, "fwd"))["passed"]  # fails with a 5 s delay
    assert not P3.validation_check(trades[:100], placebo, trades)["passed"]  # fewer than 300 trades
    assert not P3.validation_check(trades, _trades(0.01, 100, "fwd"), trades)["passed"]  # the placebo made money
    losing_second_half = [t for t in trades if t["day"] < "2026-10-13"] + \
        [dict(t, pnl=-0.05) for t in trades if t["day"] >= "2026-10-13"]
    assert not P3.validation_check(losing_second_half, placebo, trades)["criteria"]["both_halves_positive"]


def test_registry_logs_runs_and_enforces_the_stage_order(tmp_path):
    with pytest.raises(REG.RegistryError):
        REG.require_prerequisite(tmp_path, "H3", "validate")  # no screen on record
    REG.log_run(tmp_path, "H3", "screen", ["train", "val", "test"], {"thr": 0.02}, {"n": 5}, False)
    with pytest.raises(REG.RegistryError):
        REG.require_prerequisite(tmp_path, "H3", "validate")  # screen failed
    REG.log_run(tmp_path, "H3", "screen", ["val"], {"thr": 0.02}, {"n": 6}, True)
    REG.require_prerequisite(tmp_path, "H3", "validate")  # the latest screen passed
    REG.require_prerequisite(tmp_path, "H4", "validate")  # H4 has no screen
    with pytest.raises(REG.RegistryError):
        REG.require_prerequisite(tmp_path, "H3", "confirm")  # needs a passed validation
    REG.log_run(tmp_path, "H3", "validate", ["fwd"], {"thr": 0.02}, {}, True)
    assert not REG.holdout_looks(tmp_path)
    REG.log_run(tmp_path, "H3", "confirm", ["holdout"], {"thr": 0.02}, {}, None)
    assert len(REG.holdout_looks(tmp_path)) == 1
    assert [r["stage"] for r in REG.load(tmp_path)] == ["screen", "screen", "validate", "confirm"]
    with pytest.raises(ValueError):
        REG.log_run(tmp_path, "H3", "bogus", [], {}, {}, None)


def test_h1b_is_gated_by_the_h1_screen_and_spec_hash_is_stable():
    assert REG.PREREQUISITE[("H1b", "validate")] == ("H1", "screen")
    assert REG.spec_hash({"a": 1, "b": [1, 2]}) == REG.spec_hash({"b": [1, 2], "a": 1})
    assert REG.spec_hash({"a": 1}) != REG.spec_hash({"a": 2})
