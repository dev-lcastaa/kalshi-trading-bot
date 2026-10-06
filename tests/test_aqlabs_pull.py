import gzip

import pytest

for _mod in ("numpy", "duckdb", "pyarrow"):
    pytest.importorskip(_mod)

from aqlabs.store import EventStore  # noqa: E402
from aqlabs.store import pull  # noqa: E402

T0 = 1_790_000_000_000


def _quote(ts, ticker="KXBTC15M-A"):
    return {"market_ticker": ticker, "ts_ms": ts, "price_dollars": 0.5, "yes_bid_dollars": 0.49, "yes_ask_dollars": 0.51,
            "yes_bid_size": 5.0, "yes_ask_size": 5.0, "volume": 1.0, "open_interest": 1.0, "received_at_ms": ts}


def _index(ts):
    return {"index_id": "BRTI", "ts_ms": ts, "value": 100.0, "exchange_ts_ms": ts, "received_at_ms": ts, "avg_60s": 100.0}


def _external(ts):
    return {"source": "coinbase", "symbol": "BTC-USD", "index_id": "BRTI", "ts_ms": ts, "received_at_ms": ts,
            "price": 100.0, "bid": 99.9, "ask": 100.1, "volume_24h": 1.0}


def _gz(path, text):
    with gzip.open(path, "wt") as fh:
        fh.write(text)


def _fixture(tmp_path, gap_rows_ms):
    legacy, collector, gap = tmp_path / "legacy", tmp_path / "collector", tmp_path / "gap"
    gap.mkdir()
    le, co = EventStore(legacy), EventStore(collector)
    le.append("market_ticks", [_quote(T0 + i * 1000) for i in range(3)])
    le.append("index_ticks", [_index(T0 + i * 1000) for i in range(3)])
    le.append("external_ticks", [_external(T0 + i * 1000) for i in range(3)])
    co.append("market_ticks", [_quote(T0 + 100_000 + i * 1000) for i in range(2)])
    co.append("index_ticks", [_index(T0 + 100_000 + i * 1000) for i in range(2)])
    co.append("external_ticks", [_external(T0 + 100_000 + i * 1000) for i in range(2)])
    co.append("trades", [{"trade_id": "a", "market_ticker": "M", "ts_ms": T0 + 100_500, "received_at_ms": T0 + 100_500,
                          "yes_price_dollars": 0.5, "no_price_dollars": 0.5, "count": 1.0, "taker_side": "yes",
                          "is_block_trade": 0}])
    (legacy / "registry.jsonl").write_text('{"hypothesis": "H1", "stage": "screen", "passed": false}\n')
    q_header = "market_ticker,ts_ms,price_dollars,yes_bid_dollars,yes_ask_dollars,yes_bid_size,yes_ask_size,volume,open_interest"
    _gz(gap / "market_ticks.csv.gz", q_header + "\n" + "".join(f"KXBTC15M-A,{ts},0.5,0.49,0.51,5,5,1,1\n" for ts in gap_rows_ms))
    _gz(gap / "index_ticks.csv.gz", "index_id,ts_ms,value\n" + "".join(f"BRTI,{ts},100\n" for ts in gap_rows_ms))
    _gz(gap / "external_ticks.csv.gz", "source,symbol,index_id,ts_ms,received_at_ms,price,bid,ask,volume_24h\n"
        + "".join(f"coinbase,BTC-USD,BRTI,{ts},{ts},100,99.9,100.1,1\n" for ts in gap_rows_ms))
    _gz(gap / "markets.csv.gz", "ticker,index_id,strike,close_ts_ms,first_seen_ts_ms,last_seen_ts_ms,status,closed_at_ms,"
        f"result,outcome_checked_at_ms\nKXBTC15M-A,BRTI,100.0,{T0 + 900_000},{T0},{T0},closed,{T0},yes,{T0}\n")
    return legacy, collector, gap


def test_combined_store_is_legacy_plus_collector_plus_only_the_gap_rows(tmp_path):
    # rows at +10 s and +20 s are in the gap; +1 s (already in legacy) and +100 s (already in the collector) are not
    legacy, collector, gap = _fixture(tmp_path, [T0 + 1_000, T0 + 10_000, T0 + 20_000, T0 + 100_000])
    work = tmp_path / "work"
    work.mkdir()
    new = pull.build_combined(legacy, collector, gap, work, previous=None)
    store = EventStore(new)
    assert store.count("market_ticks") == 3 + 2 + 2
    assert store.count("index_ticks") == 7 and store.count("external_ticks") == 7
    assert store.count("trades") == 1 and store.count("markets") == 1
    assert pull.duplicate_report(store) == {"market_ticks": 0, "index_ticks": 0, "external_ticks": 0, "trades": 0}
    assert (new / "registry.jsonl").read_text() == (legacy / "registry.jsonl").read_text()  # the record is carried over
    assert EventStore(legacy).count("market_ticks") == 3  # the legacy store is never modified


def test_the_rebuild_prefers_the_previous_registry_and_refuses_to_publish_duplicates(tmp_path):
    legacy, collector, gap = _fixture(tmp_path, [T0 + 10_000])
    work = tmp_path / "work"
    previous = work / "combined.prev"
    previous.mkdir(parents=True)
    (previous / "registry.jsonl").write_text('{"hypothesis": "H4", "stage": "validate", "passed": true}\n')
    new = pull.build_combined(legacy, collector, gap, work, previous=previous)
    assert "H4" in (new / "registry.jsonl").read_text()
    # a collector file that repeats a legacy row (a wrong bound) must be caught
    co = EventStore(collector)
    co.append("market_ticks", [_quote(T0 + 1_000)])
    with pytest.raises(RuntimeError, match="duplicate rows"):
        pull.build_combined(legacy, collector, gap, work, previous=None)


def test_merge_tree_copies_each_parquet_file_once(tmp_path):
    store = EventStore(tmp_path / "src")
    store.append("market_ticks", [_quote(T0)])
    dst = tmp_path / "dst"
    assert pull.merge_tree(tmp_path / "src", dst) == 1
    assert pull.merge_tree(tmp_path / "src", dst) == 0  # nothing is copied or overwritten twice
    assert EventStore(dst).count("market_ticks") == 1


def test_an_until_bound_with_no_collector_data_imports_everything_after_the_legacy_end(tmp_path):
    legacy, _, gap = _fixture(tmp_path, [T0 + 10_000, T0 + 20_000])
    empty_collector = tmp_path / "empty_collector"
    empty_collector.mkdir()
    (empty_collector / "market_ticks").mkdir()  # an extracted but empty store
    work = tmp_path / "work"
    work.mkdir()
    store = EventStore(pull.build_combined(legacy, empty_collector, gap, work, previous=None))
    assert store.count("market_ticks") == 3 + 2
