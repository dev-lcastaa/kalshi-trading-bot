import datetime as dt

import numpy as np
import pytest

for _mod in ("numpy", "scipy", "duckdb", "pyarrow"):
    pytest.importorskip(_mod)

from aqlabs.research import phase3_h8 as H8  # noqa: E402
from aqlabs.research import registry as REG  # noqa: E402
from aqlabs.research import replay as R  # noqa: E402
from aqlabs.store import EventStore  # noqa: E402


def _close_ms(day: str, hh: int, mm: int) -> int:
    return int(dt.datetime.fromisoformat(f"{day}T{hh:02d}:{mm:02d}:00+00:00").timestamp() * 1000)


def _build(root, first_day="2026-10-07", last_day="2026-10-21", coins=("ETHUSD_RTI", "XRPUSD_RTI", "BRTI"), seed=0):
    """Two markets a day per coin. The index sits 0.3% above the 100.0 strike while the quote stays at 0.50, so the
    fair-value rule has a clear (and correct: every market settles YES) reason to buy."""
    rng = np.random.default_rng(seed)
    store = EventStore(root)
    quotes, index, meta = [], [], []
    d = dt.date.fromisoformat(first_day)
    end = dt.date.fromisoformat(last_day)
    while d <= end:
        day = d.isoformat()
        for coin in coins:
            start_ms = _close_ms(day, 11, 15)
            noise = rng.normal(0, 1e-4, 3600)
            index += [{"index_id": coin, "ts_ms": start_ms + k * 1000, "value": 100.3 * float(np.exp(noise[k])),
                       "exchange_ts_ms": None, "received_at_ms": None, "avg_60s": None} for k in range(3600)]
            for hh, mm in ((12, 0), (12, 15)):
                close = _close_ms(day, hh, mm)
                ticker = f"KX{coin}15M-{day}-{hh:02d}{mm:02d}"
                scale = 100.0 if coin == "BRTI" else 1.0
                quotes += [{"market_ticker": ticker, "ts_ms": close - (900 - k) * 1000, "price_dollars": 0.5,
                            "yes_bid_dollars": 0.49, "yes_ask_dollars": 0.51, "yes_bid_size": 10.0, "yes_ask_size": 10.0,
                            "volume": scale * k, "open_interest": 1.0, "received_at_ms": None} for k in range(900)]
                meta.append({"ticker": ticker, "index_id": coin, "strike": 100.0, "close_ts_ms": close,
                             "observed_ms": close + 60_000, "status": "finalized", "result": "yes", "expiration_value": 100.3})
        d += dt.timedelta(days=1)
    for table, rows in (("market_ticks", quotes), ("index_ticks", index), ("market_meta", meta)):
        store.append(table, rows)
    return store


@pytest.fixture(scope="module")
def loaded(tmp_path_factory):
    store = _build(tmp_path_factory.mktemp("h8"))
    grids, markets = R.load_all(store)
    return store, grids, markets


def test_d0_is_the_day_after_the_first_alt_market_and_stage_windows_are_14_days_each(loaded):
    _, _, markets = loaded
    d0, w2, w3 = H8.windows(markets)
    assert d0 == dt.date(2026, 10, 8)
    assert w2 == (dt.date(2026, 10, 8), dt.date(2026, 10, 21)) and w3 == (dt.date(2026, 10, 22), dt.date(2026, 11, 4))


def test_a_store_without_alt_markets_has_no_h8_data_yet(tmp_path):
    store = _build(tmp_path, first_day="2026-10-07", last_day="2026-10-08", coins=("BRTI",))
    _, markets = R.load_all(store)
    with pytest.raises(REG.RegistryError, match="no alt-coin markets"):
        H8.windows(markets)


def test_the_pooled_test_uses_only_alt_coins_and_finds_the_planted_edge(loaded):
    _, grids, markets = loaded
    lines = []
    chk, complete = H8.evaluate(markets, grids, "validate", dt.date(2026, 10, 25), False, lines.append)
    assert complete and chk["d0"] == "2026-10-08" and chk["window"] == ["2026-10-08", "2026-10-21"]
    d0, w2, _ = H8.windows(markets)
    alts = [m for m in markets if m["coin"] in H8.ALT_INDEX and H8._in(m, w2)]
    trades = H8.evaluate_rule(grids, alts)["trades"]
    assert trades and {t["coin"] for t in trades} <= set(H8.ALT_INDEX)  # BTC never enters the pooled result
    assert chk["mean_c"] > 0 and chk["n"] == len(trades)
    per_coin = chk["descriptive"]["per_coin"]
    assert {"ETHUSD_RTI", "XRPUSD_RTI", "BRTI"} <= set(per_coin)  # BTC is reported next to them, not pooled
    assert chk["descriptive"]["volume_per_market"]["BRTI"] > chk["descriptive"]["volume_per_market"]["ETHUSD_RTI"]


def test_an_unfinished_window_is_refused_unless_it_is_an_unlogged_dry_run(loaded):
    _, grids, markets = loaded
    with pytest.raises(REG.RegistryError, match="not complete"):
        H8.evaluate(markets, grids, "validate", dt.date(2026, 10, 15), False, lambda *_: None)  # window still running
    with pytest.raises(REG.RegistryError, match="not complete"):
        H8.evaluate(markets, grids, "validate", dt.date(2026, 10, 21), False, lambda *_: None)  # last day not over yet
    lines = []
    chk, complete = H8.evaluate(markets, grids, "validate", dt.date(2026, 10, 15), True, lines.append)
    assert not complete and any("DRY RUN" in x for x in lines)


def test_missing_days_inside_the_window_block_a_logged_run(tmp_path):
    store = _build(tmp_path, last_day="2026-10-18")  # data stops three days before the window ends
    grids, markets = R.load_all(store)
    with pytest.raises(REG.RegistryError, match="not complete"):
        H8.evaluate(markets, grids, "validate", dt.date(2026, 10, 30), False, lambda *_: None)


def test_stage_three_is_a_separate_fresh_window_and_needs_a_passed_stage_two(loaded, tmp_path):
    _, grids, markets = loaded
    with pytest.raises(REG.RegistryError, match="no alt markets inside the window"):
        H8.evaluate(markets, grids, "confirm", dt.date(2026, 11, 10), True, lambda *_: None)  # D0+14.. has no data yet
    with pytest.raises(REG.RegistryError):
        REG.require_prerequisite(tmp_path, "H8", "confirm")
    REG.log_run(tmp_path, "H8", "validate", ["alts"], {}, {}, True)
    REG.require_prerequisite(tmp_path, "H8", "confirm")


def test_the_other_hypotheses_stay_on_their_registered_coins_once_alts_exist(loaded, tmp_path):
    _, _, markets = loaded
    from aqlabs.research import phase3_h4 as H4
    core = [m for m in markets if m["coin"] in R.CORE_COINS]
    assert core and all(m["coin"] not in H8.ALT_INDEX for m in core)
    # H4 filters to the core coins before it looks for days, so alt markets alone cannot satisfy it
    alts_only = [m for m in markets if m["coin"] in H8.ALT_INDEX]
    with pytest.raises(REG.RegistryError, match="more than 7 days"):
        H4.evaluate(alts_only, "validate", EventStore(tmp_path), None, True, lambda *_: None)
