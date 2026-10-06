import numpy as np
import pytest

for _mod in ("numpy", "scipy", "duckdb", "pyarrow"):
    pytest.importorskip(_mod)

from aqlabs.costs import RULES, first_fills, maker_fee, taker_fee  # noqa: E402
from aqlabs.research import maker as MK  # noqa: E402
from aqlabs.research import replay as R  # noqa: E402


def _stream(price, volume, bid=0.40, ask=0.42, bsz=10.0, asz=10.0):
    n = len(price)
    return dict(ts_ms=np.arange(n) * 1000, price=np.array(price, float), volume=np.array(volume, float),
                bid=np.full(n, bid), ask=np.full(n, ask), bid_size=np.full(n, bsz), ask_size=np.full(n, asz))


def _fills(side, limit, s, start=500, end=5000):
    return first_fills(side, limit, s["ts_ms"], s["price"], s["volume"], s["bid"], s["ask"], s["bid_size"],
                       s["ask_size"], start, end)


def test_yes_order_fills_at_the_level_optimistically_but_only_on_a_trade_through_pessimistically():
    s = _stream(price=[0.41, 0.41, 0.40, 0.40, 0.39], volume=[100, 100, 103, 103, 110])
    f = _fills("yes", 0.40, s)
    assert f["optimistic"] == 2000  # 3 contracts traded at our price
    assert f["pessimistic"] == 4000  # first trade strictly below our price
    assert f["queue"] == 4000  # 10 ahead of us; only 3 traded at the level before the trade-through


def test_queue_rule_fills_once_the_displayed_size_ahead_has_traded():
    s = _stream(price=[0.41, 0.41, 0.40, 0.40, 0.39], volume=[100, 100, 103, 103, 110], bsz=2.0)
    assert _fills("yes", 0.40, s)["queue"] == 2000  # 3 traded >= 2 ahead + our 1


def test_no_order_is_the_mirror_image():
    s = _stream(price=[0.41, 0.41, 0.42, 0.42, 0.43], volume=[100, 100, 103, 103, 110], asz=2.0)
    f = _fills("no", 0.42, s)
    assert (f["optimistic"], f["queue"], f["pessimistic"]) == (2000, 2000, 4000)


def test_no_trade_at_or_through_our_price_means_no_fill():
    s = _stream(price=[0.41] * 6, volume=[100, 101, 102, 103, 104, 105])
    assert _fills("yes", 0.40, s) == {r: None for r in RULES}


def test_quote_changes_without_volume_are_not_trades():
    s = _stream(price=[0.41, 0.30, 0.30, 0.30], volume=[100, 100, 100, 100])  # stale price field, no traded volume
    assert _fills("yes", 0.40, s) == {r: None for r in RULES}


def test_a_better_bid_ahead_of_us_means_only_a_trade_through_can_fill_us():
    s = _stream(price=[0.41, 0.41, 0.40, 0.40, 0.39], volume=[100, 100, 103, 103, 110], bid=0.41, bsz=1.0)
    f = _fills("yes", 0.40, s)  # best bid 0.41 sits above our 0.40, so the queue rule cannot fire early
    assert f["queue"] == f["pessimistic"] == 4000


def test_an_order_that_improves_the_book_has_nobody_ahead():
    s = _stream(price=[0.41, 0.41, 0.40, 0.40, 0.39], volume=[100, 100, 100.5, 101.5, 110], bid=0.38)
    assert _fills("yes", 0.40, s)["queue"] == 3000  # first >=1 contract traded at/through our price


def test_orders_only_see_ticks_inside_their_window_and_nan_volume_is_ignored():
    s = _stream(price=[0.41, 0.41, 0.39, 0.39], volume=[100, 100, 110, 120])
    assert _fills("yes", 0.40, s, start=500, end=1500)["pessimistic"] is None  # trade at 2000 is after the window
    s["volume"][2] = np.nan
    assert _fills("yes", 0.40, s)["pessimistic"] == 3000


def test_fill_times_are_always_ordered_optimistic_then_queue_then_pessimistic():
    rng = np.random.default_rng(0)
    for _ in range(300):
        n = 40
        price = np.round(0.40 + rng.integers(-3, 4, n) / 100, 2)
        volume = np.cumsum(rng.random(n) * rng.integers(0, 3, n))
        s = _stream(price, volume, bsz=float(rng.integers(0, 15)))
        for side, limit in (("yes", 0.40), ("no", 0.42)):
            f = _fills(side, limit, s, start=int(rng.integers(0, 10)) * 1000, end=40_000)
            if f["pessimistic"] is not None:
                assert f["queue"] is not None and f["queue"] <= f["pessimistic"]
            if f["queue"] is not None:
                assert f["optimistic"] is not None and f["optimistic"] <= f["queue"]


def test_maker_fee_is_zero_by_default_and_a_share_of_the_taker_fee_when_stressed():
    assert float(maker_fee(0.5)) == 0.0
    assert float(maker_fee(0.5, scale=0.25)) == pytest.approx(0.01)
    assert float(maker_fee(0.5, scale=1.0)) == taker_fee(0.5)


# ------------------------------------------------------------------ strategy
CLOSE_S = 1_790_003_000


def _market(trade_at_s=None, y=1.0):
    t0 = CLOSE_S - 900
    ts = np.arange(900) * 1000 + t0 * 1000
    price = np.full(900, 0.46)
    volume = np.zeros(900)
    if trade_at_s is not None:
        k = trade_at_s - t0
        price[k:] = 0.44  # a trade through our 0.45 bid
        volume[k:] = 5.0
    q = np.column_stack([ts.astype(float), np.full(900, 0.45), np.full(900, 0.47), np.full(900, 10.0),
                         np.full(900, 10.0), price, volume])
    m = dict(ticker="T", coin="BRTI", strike=100.0, close_s=CLOSE_S, y=y, day="2026-09-20", split="train", q=q)
    R.quote_arrays(m)
    return m


def _inputs(m):
    ones = np.ones(len(m["s"]), dtype=bool)
    ml = (CLOSE_S - m["s"]) / 60.0
    return [(np.zeros(len(ml)), ones, ml)]


def test_a_filled_maker_order_pays_the_limit_price_and_no_fee():
    m = _market(trade_at_s=CLOSE_S - 830, y=1.0)
    orders = MK.run_maker([m], _inputs(m), lambda *_: np.full(len(m["s"]), 0.9), thr=0.03, window_s=60, gap=60)
    first = orders[0]
    assert first["side"] == "yes" and first["limit"] == 0.45
    assert first["fill_ms"]["pessimistic"] is not None and first["fill_ms"]["optimistic"] is not None
    assert first["maker_pnl"] == pytest.approx(1.0 - 0.45)  # no fee
    assert first["taker_pnl"] == pytest.approx(1.0 - 0.47 - taker_fee(0.47))  # crossing pays the ask and the fee
    assert MK.fill_rate(orders, "pessimistic") > 0


def test_unfilled_orders_earn_nothing_and_count_in_the_per_signal_view():
    m = _market(trade_at_s=None, y=1.0)  # nobody ever trades through us
    orders = MK.run_maker([m], _inputs(m), lambda *_: np.full(len(m["s"]), 0.9), thr=0.03)
    assert orders and all(o["fill_ms"][r] is None for o in orders for r in RULES)
    assert MK.as_trades(orders, "pessimistic", per_signal=False) == []
    per_signal = MK.as_trades(orders, "pessimistic", per_signal=True)
    assert len(per_signal) == len(orders) and all(t["pnl"] == 0.0 for t in per_signal)


def test_decisions_are_spaced_capped_and_independent_of_whether_they_fill():
    m = _market(trade_at_s=None)
    orders = MK.run_maker([m], _inputs(m), lambda *_: np.full(len(m["s"]), 0.9), thr=0.03, gap=60, max_attempts=3)
    assert len(orders) == 3
    starts = [o["start_ms"] for o in orders]
    assert all(b - a >= 60_000 for a, b in zip(starts, starts[1:]))


def test_no_decision_when_the_edge_over_the_limit_price_is_too_small():
    m = _market()
    assert MK.run_maker([m], _inputs(m), lambda *_: np.full(len(m["s"]), 0.47), thr=0.03) == []


def test_adverse_selection_report_separates_filled_from_unfilled_decisions():
    orders = [dict(taker_pnl=-0.05, fill_ms={r: 1 for r in RULES}), dict(taker_pnl=0.10, fill_ms={r: None for r in RULES}),
              dict(taker_pnl=None, fill_ms={r: None for r in RULES})]
    out = MK.adverse_selection(orders, "queue")
    assert (out["filled_n"], out["unfilled_n"]) == (1, 1)
    assert out["filled_taker_mean"] == pytest.approx(-0.05) and out["unfilled_taker_mean"] == pytest.approx(0.10)


def test_cancel_on_a_weakening_signal_pulls_the_order_before_a_later_trade_through():
    m = _market(trade_at_s=CLOSE_S - 800, y=1.0)  # a trade strictly through our 0.45 bid at 800 s before close
    weakens_at = CLOSE_S - 820

    def prob(market, z, ml):
        return np.where(market["s"] < weakens_at, 0.9, 0.46)  # edge over the 0.45 limit falls from 45c to 1c

    held = MK.run_maker([m], _inputs(m), prob, thr=0.03, window_s=60)[0]
    pulled = MK.run_maker([m], _inputs(m), prob, thr=0.03, window_s=60, cancel_edge=0.02)[0]
    assert held["fill_ms"]["pessimistic"] is not None and not held["cancelled"]
    assert pulled["cancelled"] and all(pulled["fill_ms"][r] is None for r in RULES)


def test_cancel_latency_lets_a_trade_that_lands_before_the_cancel_arrives_still_fill():
    m = _market(trade_at_s=CLOSE_S - 819, y=1.0)  # trade one second after the signal weakens, inside the 2 s latency
    prob = lambda market, z, ml: np.where(market["s"] < CLOSE_S - 820, 0.9, 0.46)  # noqa: E731
    order = MK.run_maker([m], _inputs(m), prob, thr=0.03, window_s=60, cancel_edge=0.02)[0]
    assert order["cancelled"] and order["fill_ms"]["pessimistic"] is not None
