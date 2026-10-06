import datetime as dt

import numpy as np
import pytest

for _mod in ("numpy", "scipy", "duckdb", "pyarrow"):
    pytest.importorskip(_mod)

from aqlabs.research import phase3_h4 as H4  # noqa: E402
from aqlabs.research import registry as REG  # noqa: E402
from aqlabs.research import replay as R  # noqa: E402
from aqlabs.store import EventStore  # noqa: E402


def _ms(day: str, hh: int, mm: int) -> int:
    d = dt.datetime.fromisoformat(f"{day}T{hh:02d}:{mm:02d}:00+00:00")
    return int(d.timestamp() * 1000)


def _synthetic_store(root, days, effect, seed=0):
    """Two coins x two markets a day, 15 minutes of 1 Hz quotes, depth and trades. With `effect` > 0 the mid
    10 seconds later follows the depth imbalance (a planted signal); with 0 it is unrelated noise."""
    rng = np.random.default_rng(seed)
    store = EventStore(root)
    quotes, depth, trades, meta, index = [], [], [], [], []
    n = 0
    for day in days:
        for coin, series, iid in (("BTC", "KXBTC15M", "BRTI"), ("SOL", "KXSOL15M", "SOLUSD_RTI")):
            for hh, mm in ((12, 0), (12, 15)):
                close_ms = _ms(day, hh, mm)
                ticker = f"{series}-{day}-{hh:02d}{mm:02d}"
                steps = 910
                blocks = rng.choice([-0.8, 0.0, 0.8], size=steps // 20 + 2)
                imb = np.repeat(blocks, 20)[:steps]
                noise = np.repeat(rng.choice([-0.8, 0.0, 0.8], size=steps // 20 + 2), 20)[:steps]
                for k in range(900):
                    t_ms = close_ms - (900 - k) * 1000
                    lagged = imb[k - 10] if k >= 10 else 0.0
                    mid = 0.5 + 0.2 * (effect * lagged + (1 - min(effect, 1)) * noise[k] * (1 if effect == 0 else 0))
                    bid, ask = round(mid - 0.01, 2), round(mid + 0.01, 2)
                    quotes.append({"market_ticker": ticker, "ts_ms": t_ms, "price_dollars": mid, "yes_bid_dollars": bid,
                                   "yes_ask_dollars": ask, "yes_bid_size": 50.0, "yes_ask_size": 50.0,
                                   "volume": float(k), "open_interest": 10.0, "received_at_ms": t_ms})
                    y_tot, n_tot = 100 * (1 + imb[k]), 100 * (1 - imb[k])
                    depth.append({"market_ticker": ticker, "ts_ms": t_ms, "seq": k,
                                  "yes_bids": f"[[{bid},{y_tot}]]", "no_bids": f"[[{round(1 - ask, 2)},{n_tot}]]",
                                  "yes_total": y_tot, "no_total": n_tot})
                    side = "yes" if (imb[k] > 0) == (effect > 0) and imb[k] != 0 else "no"
                    n += 1
                    trades.append({"trade_id": f"t{n}", "market_ticker": ticker, "ts_ms": t_ms + 500,
                                   "received_at_ms": t_ms + 500, "yes_price_dollars": mid, "no_price_dollars": 1 - mid,
                                   "count": 3.0, "taker_side": side, "is_block_trade": 0})
                    index.append({"index_id": iid, "ts_ms": t_ms, "value": 100.0, "exchange_ts_ms": t_ms,
                                  "received_at_ms": t_ms, "avg_60s": 100.0})
                meta.append({"ticker": ticker, "index_id": iid, "strike": 100.0, "close_ts_ms": close_ms,
                             "observed_ms": close_ms + 60_000, "status": "finalized",
                             "result": "yes" if rng.random() < 0.5 else "no", "expiration_value": 100.0})
    for table, rows in (("market_ticks", quotes), ("orderbook_depth", depth), ("trades", trades),
                        ("market_meta", meta), ("index_ticks", index)):
        store.append(table, rows)
    return store


FWD_DAYS = [f"2026-10-{d:02d}" for d in range(6, 20)]


def _run(store, allow_partial=False):
    lines = []
    _, markets = R.load_all(store)
    out = H4.evaluate(markets, "validate", store, None, allow_partial, lines.append)
    return out, lines


def test_depth_imbalance_uses_the_top_levels_and_rejects_bad_input():
    assert H4.depth_imbalance("[[0.4,30],[0.39,10]]", "[[0.58,10]]") == pytest.approx((40 - 10) / 50)
    assert H4.depth_imbalance("[[0.4,30],[0.39,10],[0.38,5],[0.37,5],[0.36,5],[0.35,999]]", "[[0.58,40]]") == \
        pytest.approx((55 - 40) / 95)  # only the top 5 levels count
    assert np.isnan(H4.depth_imbalance("[]", "[]"))
    assert np.isnan(H4.depth_imbalance(None, "[[0.5,1]]")) and np.isnan(H4.depth_imbalance("oops", "[]"))
    assert H4.signed_flow_feature([0.0, 3.0, -3.0]).tolist() == pytest.approx([0.0, np.log(4), -np.log(4)])


def test_features_never_use_the_current_or_a_later_second(tmp_path):
    store = _synthetic_store(tmp_path, ["2026-10-06"], effect=1.0)
    _, markets = R.load_all(store)
    m = next(x for x in markets if x["coin"] == "BRTI")
    # a huge one-sided trade lands at second T (its timestamp is T*1000 + 500)
    T = int(m["s"][400])
    store.append("trades", [{"trade_id": "big", "market_ticker": m["ticker"], "ts_ms": T * 1000 + 500,
                             "received_at_ms": T * 1000 + 500, "yes_price_dollars": 0.5, "no_price_dollars": 0.5,
                             "count": 1e6, "taker_side": "yes", "is_block_trade": 0}])
    H4.attach_features(store, [m])
    before = float(m["flow"][400])  # decision at second T: the trade is inside second T, so it must not be visible
    after = float(m["flow"][401])
    assert before < np.log1p(1e5) and after > np.log1p(1e5)
    assert m["flow"][400 + H4.FLOW_WINDOW_S] > np.log1p(1e5) and m["flow"][401 + H4.FLOW_WINDOW_S] < np.log1p(1e5)  # leaves the window


def test_a_planted_signal_is_found_and_traded_profitably_end_to_end(tmp_path):
    store = _synthetic_store(tmp_path, FWD_DAYS, effect=1.0)
    (chk, split, complete), lines = _run(store)
    assert split == "fwd" and complete
    assert chk["oos_r2"] > 0.05 and chk["beta"][0] > 0
    assert chk["coverage"] == 1.0 and chk["mean_c"] > 0  # clear of spread and both fees
    assert chk["placebo_c"] < chk["mean_c"]  # shuffled features lose the effect
    assert any("H4 trades" in x for x in lines)


def test_unrelated_features_do_not_produce_a_pass(tmp_path):
    store = _synthetic_store(tmp_path, FWD_DAYS, effect=0.0, seed=2)
    (chk, _, _), _ = _run(store)
    assert not chk["passed"] and abs(chk["oos_r2"]) < 0.05  # nothing to find, and the gates say so


def test_an_incomplete_period_is_refused_unless_it_is_an_unlogged_dry_run(tmp_path):
    store = _synthetic_store(tmp_path, FWD_DAYS[:10], effect=1.0)  # Oct 6..15: the period ends Oct 19
    with pytest.raises(REG.RegistryError, match="incomplete"):
        _run(store)
    (chk, _, complete), lines = _run(store, allow_partial=True)
    assert not complete and any("DRY RUN" in x for x in lines)


def test_too_few_days_to_fit_and_evaluate_is_an_error(tmp_path):
    store = _synthetic_store(tmp_path, FWD_DAYS[:6], effect=1.0)
    with pytest.raises(REG.RegistryError, match="more than 7 days"):
        _run(store, allow_partial=True)


def test_missing_collector_tables_give_a_clear_error(tmp_path):
    store = EventStore(tmp_path)
    with pytest.raises(REG.RegistryError, match="collector data needed"):
        H4.attach_features(store, [])
