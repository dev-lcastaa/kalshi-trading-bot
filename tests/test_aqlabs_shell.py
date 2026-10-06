import collections

import numpy as np
import pytest

for _mod in ("numpy", "scipy", "duckdb", "pyarrow"):
    pytest.importorskip(_mod)

from aqlabs.costs import taker_fee  # noqa: E402
from aqlabs.research import phase3_shell as SH  # noqa: E402
from aqlabs.research import registry as REG  # noqa: E402

CLOSE = 1_790_003_000
N = 840


def _market(close=CLOSE, day="2026-09-20", split="val", y=1.0, ticker="T", **over):
    s = np.arange(close - N, close)
    m = dict(ticker=ticker, coin="BRTI", strike=100.0, close_s=close, y=y, day=day, split=split, s=s,
             bid=np.full(N, 0.51), ask=np.full(N, 0.52), bsz=np.full(N, 10.0), asz=np.full(N, 10.0),
             qok=np.ones(N, dtype=bool), qage=np.zeros(N))
    m["mid"] = (m["bid"] + m["ask"]) / 2
    m.update(over)
    return m


def _p(value=0.62):
    return np.full(N, value)


NO_CHOP = np.zeros(N, dtype=int)


def _run(markets, probs, chops=None, delay=2):
    return SH.run_shell(markets, probs, chops or [NO_CHOP] * len(markets), delay)


# ------------------------------------------------------------------ the validator
def test_a_candidate_needs_the_edge_the_price_band_the_spread_liquidity_freshness_and_no_chop():
    m = _market()
    ok = SH.candidate_masks(m, _p(), NO_CHOP)
    assert ok["yes"][0] and not ok["no"][0]  # YES: 0.62 - 0.52 = 10c edge
    low = SH.candidate_masks(m, _p(0.58), NO_CHOP)  # 6c edge
    assert not low["yes"][0]
    wide = SH.candidate_masks(_market(ask=np.full(N, 0.55)), _p(), NO_CHOP)  # 4c spread
    assert not wide["yes"][0]
    stale = SH.candidate_masks(_market(qage=np.full(N, 1500.0)), _p(), NO_CHOP)
    assert not stale["yes"][0]
    thin = SH.candidate_masks(_market(asz=np.zeros(N)), _p(), NO_CHOP)
    assert not thin["yes"][0]
    assert not SH.candidate_masks(m, _p(), np.full(N, 3))["yes"][0]  # chop
    rich = SH.candidate_masks(_market(bid=np.full(N, 0.84), ask=np.full(N, 0.85)), _p(0.99), NO_CHOP)
    assert not rich["yes"][0]  # 14c edge but an 85c price is outside the 20-80c band
    assert not SH.candidate_masks(m, np.full(N, np.nan), NO_CHOP)["yes"][0]
    window = SH.candidate_masks(m, _p(), NO_CHOP)["yes"]
    assert window[0] and window[780] and not window[781:].any()  # nothing inside the last 59 seconds


def test_no_side_is_evaluated_separately_and_priced_at_one_minus_the_bid():
    m = _market(bid=np.full(N, 0.40), ask=np.full(N, 0.41))
    c = SH.candidate_masks(m, _p(0.20), NO_CHOP)  # P(NO) = 0.80, NO price = 0.60 -> 20c edge
    assert c["no"][0] and not c["yes"][0]
    out, _ = _run([m], [_p(0.20)])
    assert out and out[0]["side"] == "no" and out[0]["price"] == pytest.approx(0.60)


# ------------------------------------------------------------------ exits
def test_take_profit_exits_two_seconds_after_the_trigger_at_the_bid_and_pays_both_fees():
    bid, ask = np.full(N, 0.51), np.full(N, 0.52)
    bid[100:105], ask[100:105] = 0.60, 0.61  # +8c over the 0.52 entry
    bid[105:], ask[105:] = 0.62, 0.63  # no new edge afterwards (model says 0.62)
    trades, _ = _run([_market(bid=bid, ask=ask)], [_p()])
    assert len(trades) == 1 and trades[0]["exit"] == "tp"
    assert trades[0]["pnl"] == pytest.approx(0.60 - taker_fee(0.60) - 0.52 - taker_fee(0.52))  # filled at the 0.60 bid


def test_stop_loss_exits_at_minus_eight_cents_of_price_and_the_exit_can_be_worse_than_the_trigger():
    bid, ask = np.full(N, 0.51), np.full(N, 0.52)
    bid[100:], ask[100:] = 0.44, 0.60  # price falls 8c; the spread is now too wide for any new entry
    bid[102:], ask[102:] = 0.40, 0.60  # by the time the 2 s delayed exit lands the bid is lower still
    trades, _ = _run([_market(bid=bid, ask=ask)], [_p()])
    assert len(trades) == 1 and trades[0]["exit"] == "stop"
    assert trades[0]["pnl"] == pytest.approx(0.40 - taker_fee(0.40) - 0.52 - taker_fee(0.52))


def test_invalidation_exits_when_the_model_no_longer_values_the_position_above_the_exit_price():
    p = _p()
    p[100:] = 0.45  # below the 0.51 bid we could sell at
    trades, _ = _run([_market()], [p])
    assert len(trades) == 1 and trades[0]["exit"] == "inval"
    assert trades[0]["pnl"] == pytest.approx(0.51 - taker_fee(0.51) - 0.52 - taker_fee(0.52))


def test_a_position_that_never_triggers_settles_at_its_outcome_without_an_exit_fee():
    for y, expected in ((1.0, 1.0 - 0.52 - taker_fee(0.52)), (0.0, 0.0 - 0.52 - taker_fee(0.52))):
        trades, _ = _run([_market(y=y)], [_p()])
        assert len(trades) == 1 and trades[0]["exit"] == "settle" and trades[0]["pnl"] == pytest.approx(expected)


def test_invalidation_is_off_inside_the_last_minute_where_the_model_is_undefined():
    p = _p()
    p[790:] = np.nan  # fewer than 60 s left: no probability
    bid = np.full(N, 0.51)
    bid[790:] = 0.30  # the price collapses but only a -8c stop can react to it
    trades, _ = _run([_market(bid=bid, ask=np.maximum(bid + 0.01, 0.52))], [p])
    assert trades[0]["exit"] == "stop"


# ------------------------------------------------------------------ position and risk rules
def test_one_position_at_a_time_and_a_cooldown_before_the_next_entry():
    bid, ask = np.full(N, 0.51), np.full(N, 0.52)
    bid[100:105], ask[100:105] = 0.60, 0.61  # take profit triggered at 100, exit fills at 102
    trades, _ = _run([_market(bid=bid, ask=ask)], [_p()])
    assert [t["exit"] for t in trades] == ["tp", "settle"]
    # the edge is back from second 105, but the 20 s cooldown after the exit at second 102 allows a new decision at 122
    # only, whose order arrives at 124
    assert trades[1]["hold_s"] == N - 124


def test_the_entry_is_cancelled_when_the_edge_is_gone_by_the_time_the_order_arrives():
    p = _p()
    p[2] = 0.55  # the decision at second 0 no longer passes at second 2
    trades, _ = _run([_market()], [p])
    assert len(trades) == 1 and trades[0]["hold_s"] == N - 3  # the next decision (second 1) filled at second 3


def _losing_market(close, day, ticker):
    bid, ask = np.full(N, 0.51), np.full(N, 0.52)
    bid[100:], ask[100:] = 0.43, 0.60
    return _market(close=close, day=day, ticker=ticker, bid=bid, ask=ask)


def test_three_consecutive_losses_stop_new_entries_until_the_next_day(monkeypatch):
    monkeypatch.setattr(SH, "MAX_DAILY_LOSS", 10.0)  # isolate the consecutive-loss rule
    markets = [_losing_market(CLOSE + k * 900, "2026-09-20", f"A{k}") for k in range(4)] + \
              [_losing_market(CLOSE + 4 * 900, "2026-09-21", "B0")]
    trades, _ = _run(markets, [_p()] * 5)
    assert [t["market"] for t in trades] == ["A0", "A1", "A2", "B0"]  # the 4th market is skipped, the new day resumes


def test_the_daily_loss_cap_stops_new_entries_for_the_rest_of_the_day(monkeypatch):
    monkeypatch.setattr(SH, "MAX_CONSECUTIVE_LOSSES", 99)  # isolate the daily cap
    monkeypatch.setattr(SH, "MAX_DAILY_LOSS", 0.20)
    markets = [_losing_market(CLOSE + k * 900, "2026-09-20", f"A{k}") for k in range(4)]
    trades, _ = _run(markets, [_p()] * 4)
    assert len(trades) == 2 and sum(t["pnl"] for t in trades) <= -0.20  # two losses of about 12c each


def test_a_win_resets_the_consecutive_loss_counter(monkeypatch):
    monkeypatch.setattr(SH, "MAX_DAILY_LOSS", 10.0)
    win = _market(close=CLOSE + 2 * 900, ticker="W")  # settles at 1.0: a win
    markets = [_losing_market(CLOSE, "2026-09-20", "L0"), _losing_market(CLOSE + 900, "2026-09-20", "L1"), win,
               _losing_market(CLOSE + 3 * 900, "2026-09-20", "L2"), _losing_market(CLOSE + 4 * 900, "2026-09-20", "L3")]
    trades, _ = _run(markets, [_p()] * 5)
    assert [t["market"] for t in trades] == ["L0", "L1", "W", "L2", "L3"]


# ------------------------------------------------------------------ indicators and reporting
def test_chop_counts_strike_crossings_in_the_last_minute():
    close = 1000
    V = np.where(np.arange(1000) < 500, 101.0, 99.0)
    V[700:761] = np.where(np.arange(700, 761) % 2 == 0, 101.0, 99.0)  # oscillating around the strike
    grid = {"s0": 0, "V": V, "valid": np.ones(1000, dtype=bool)}
    m = {"s": np.arange(close - N, close), "strike": 100.0}
    chop = SH.chop_counts(grid, m)
    first = 500 - (close - N)
    assert chop[first] == 1 and chop[first - 1] == 0 and chop[first + 60] == 0
    assert chop[760 - (close - N)] >= SH.CHOP_REJECT_AT


def test_rejection_reasons_report_the_first_failing_rule_in_the_specs_order():
    p = _p()
    p[:10] = np.nan
    ask = np.full(N, 0.52)
    ask[10:15] = 0.56
    chop = np.zeros(N, dtype=int)
    chop[15:20] = 3
    asz = np.full(N, 10.0)
    asz[20:25] = 0.0
    p[25:30] = 0.55
    bid, ask2 = np.full(N, 0.51), ask.copy()
    bid[30:35], ask2[30:35], p[30:35] = 0.84, 0.85, 0.99
    qage = np.zeros(N)
    qage[35:40] = 2000.0
    m = _market(bid=bid, ask=ask2, asz=asz, qage=qage)
    counts = SH.rejection_counts(m, p, chop)
    assert counts == collections.Counter({"NO_MODEL": 10, "SPREAD_TOO_LARGE": 5, "CHOP_DETECTED": 5,
                                          "INSUFFICIENT_LIQUIDITY": 5, "EDGE_TOO_LOW": 5, "PRICE_OUT_OF_BAND": 5,
                                          "STALE_MARKET_DATA": 5, "CANDIDATE": 781 - 40})


def test_summary_reports_profit_factor_drawdown_and_exit_counts():
    trades = [dict(pnl=p, close_s=i, exit=e) for i, (p, e) in enumerate([(0.05, "tp"), (-0.12, "stop"), (0.05, "tp"),
                                                                           (-0.05, "inval")])]
    s = SH.summary(trades)
    assert s["n"] == 4 and s["win_rate"] == 0.5 and s["profit_factor"] == pytest.approx(0.10 / 0.17)
    assert s["max_drawdown_c"] == pytest.approx(12.0) and s["exits"] == {"tp": 2, "stop": 1, "inval": 1}


# ------------------------------------------------------------------ H6: model D
def _feature_markets(n_markets=120, seed=0):
    rng = np.random.default_rng(seed)
    markets, feats = [], []
    for k in range(n_markets):
        y = float(k % 2)
        split = "train" if k < 80 else "val"
        m = _market(close=CLOSE + k * 900, split=split, y=y, ticker=f"M{k}")
        X = np.column_stack([rng.normal(0, 0.3, N), (2 * y - 1) + rng.normal(0, 0.5, N), rng.normal(0, 1, N),
                             rng.uniform(-1, 1, N)])  # only momentum carries the outcome
        markets.append(m)
        feats.append((X, np.ones(N, dtype=bool)))
    return markets, feats


def test_model_d_learns_the_feature_that_carries_the_outcome_and_ablating_it_hurts():
    markets, feats = _feature_markets()
    w = SH.fit_d(markets, feats)
    assert w[2] > 2 and w[2] > 3 * max(abs(w[3]), abs(w[4]))  # the momentum coefficient dominates the noise features
    p = SH.predict_d(feats, w)
    cols = (0, 2, 3)
    ablated = SH.predict_d(feats, SH.fit_d(markets, feats, cols), cols)
    full_ll, cut_ll = SH.logloss_common(markets, [p, ablated])
    assert cut_ll > full_ll  # dropping the informative feature makes held-out loss worse


def test_log_loss_is_compared_on_the_seconds_where_every_model_is_defined():
    markets, _ = _feature_markets(n_markets=100)
    a = [np.full(N, 0.5) for _ in markets]
    b = [np.where(np.arange(N) < 400, 0.9, np.nan) for _ in markets]
    only_b_defined = [np.where(np.arange(N) < 400, 0.5, np.nan) for _ in markets]
    la, lb = SH.logloss_common(markets, [a, b])
    lc, _ = SH.logloss_common(markets, [only_b_defined, b])
    assert la == pytest.approx(lc)  # the NaN seconds of b are excluded from a's average too


def test_the_screen_needs_trades_in_each_split_a_positive_pooled_mean_and_a_losing_placebo():
    def t(split, pnl, n):
        return [dict(pnl=pnl, split=split, market=f"{split}{i}", day="2026-09-27", coin="BRTI") for i in range(n)]
    good = t("train", 0.01, 60) + t("val", 0.03, 25) + t("test", 0.02, 20)
    placebo = t("val", -0.02, 30)
    assert SH.shell_screen_check(good, placebo)["passed"]
    assert not SH.shell_screen_check(t("train", 0.01, 60) + t("val", 0.03, 5) + t("test", 0.02, 5), placebo)["passed"]
    assert not SH.shell_screen_check(good, t("val", 0.01, 30))["passed"]
    assert not SH.shell_screen_check(t("train", 0.0, 60) + t("val", 0.002, 25) + t("test", 0.002, 20), placebo)["passed"]
    assert not SH.shell_screen_check(t("train", 0.0, 60) + t("val", 0.05, 25) + t("test", -0.01, 20), placebo)["passed"]


def test_h5_and_h6_follow_the_same_stage_order_as_the_other_hypotheses(tmp_path):
    with pytest.raises(REG.RegistryError):
        REG.require_prerequisite(tmp_path, "H5", "validate")
    REG.log_run(tmp_path, "H5", "screen", ["val"], {}, {}, True)
    REG.require_prerequisite(tmp_path, "H5", "validate")
    with pytest.raises(REG.RegistryError):
        REG.require_prerequisite(tmp_path, "H6", "confirm")
