import gzip

import pytest

pytest.importorskip("numpy")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from aqlabs.store import EventStore  # noqa: E402
from aqlabs.store.quality import feed_gaps, feed_report, quote_report, uptime  # noqa: E402

DAY1 = 1_790_000_000_000  # 2026-09-21 UTC
DAY2 = DAY1 + 86_400_000


def _quote(ts, bid=0.4, ask=0.42, ticker="KXBTC15M-X"):
    return {"market_ticker": ticker, "ts_ms": ts, "price_dollars": 0.41, "yes_bid_dollars": bid, "yes_ask_dollars": ask,
            "yes_bid_size": 5.0, "yes_ask_size": 7.0, "volume": 1.0, "open_interest": 2.0}


def test_append_partitions_by_utc_date_and_is_queryable(tmp_path):
    store = EventStore(tmp_path)
    files = store.append("market_ticks", [_quote(DAY1), _quote(DAY1 + 1000), _quote(DAY2)])
    assert len(files) == 2
    assert {f.parent.name for f in files} == {"date=2026-09-21", "date=2026-09-22"}
    assert store.count("market_ticks") == 3
    data = store.query("select ts_ms from market_ticks order by ts_ms")
    assert list(data["ts_ms"]) == [DAY1, DAY1 + 1000, DAY2]


def test_append_never_rewrites_existing_files(tmp_path):
    store = EventStore(tmp_path)
    first = store.append("market_ticks", [_quote(DAY1)])[0]
    stamp = first.stat().st_mtime_ns
    second = store.append("market_ticks", [_quote(DAY1 + 5)])[0]
    assert first != second and first.stat().st_mtime_ns == stamp
    assert store.count("market_ticks") == 2


def test_import_csv_keeps_nulls_and_verifies_row_count(tmp_path):
    csv = tmp_path / "market_ticks.csv.gz"
    with gzip.open(csv, "wt") as fh:
        fh.write("market_ticker,ts_ms,price_dollars,yes_bid_dollars,yes_ask_dollars,yes_bid_size,yes_ask_size,volume,open_interest\n")
        fh.write(f"A,{DAY1},0.5,0.49,0.51,3,4,10,20\n")
        fh.write(f"A,{DAY2},,0.48,0.5,,4,10,20\n")
    store = EventStore(tmp_path / "store")
    result = store.import_csv("market_ticks", csv)
    assert result == {"table": "market_ticks", "csv_rows": 2, "store_rows": 2}
    row = store.query("select price_dollars, yes_bid_size from market_ticks where ts_ms = ?", [DAY2])
    assert row["price_dollars"].mask[0] and row["yes_bid_size"].mask[0]


def test_replace_tables_are_replaced_not_duplicated(tmp_path):
    csv = tmp_path / "markets.csv"
    csv.write_text("ticker,index_id,strike,close_ts_ms,first_seen_ts_ms,last_seen_ts_ms,status,closed_at_ms,result,outcome_checked_at_ms\n"
                   f"M1,BRTI,100.0,{DAY1},{DAY1},{DAY1},closed,{DAY1},yes,{DAY1}\n")
    store = EventStore(tmp_path / "store")
    store.import_csv("markets", csv)
    store.import_csv("markets", csv)
    assert store.count("markets") == 1


def test_manifest_fingerprint_ignores_file_layout_but_detects_changes(tmp_path):
    a, b = EventStore(tmp_path / "a"), EventStore(tmp_path / "b")
    rows = [_quote(DAY1), _quote(DAY1 + 1000)]
    a.append("market_ticks", rows)
    for r in rows:
        b.append("market_ticks", [r])
    assert a.manifest()["market_ticks"]["fingerprint"] == b.manifest()["market_ticks"]["fingerprint"]
    b.append("market_ticks", [_quote(DAY1 + 2000)])
    assert a.manifest()["market_ticks"]["fingerprint"] != b.manifest()["market_ticks"]["fingerprint"]


def test_feed_gaps_and_uptime():
    ts = [0, 1_000, 2_000, 100_000, 101_000]
    assert feed_gaps(ts, max_gap_ms=30_000) == [(2_000, 98_000)]
    assert uptime(ts, 30_000) == pytest.approx(1 - 98_000 / 101_000)
    assert feed_gaps([5], 1) == []


def test_feed_report_flags_a_feed_that_never_ticked(tmp_path):
    store = EventStore(tmp_path)
    store.append("external_ticks", [
        {"source": "coinbase", "symbol": "BTC-USD", "index_id": "BRTI", "ts_ms": DAY1 + i * 1000,
         "received_at_ms": DAY1 + i * 1000, "price": 100.0, "bid": 99.0, "ask": 101.0, "volume_24h": 1.0}
        for i in range(3)])
    report = {r["feed"]: r for r in feed_report(store)}
    assert report["coinbase/BRTI"]["ticks"] == 3
    assert report["kraken/BRTI"]["ticks"] == 0 and report["kraken/BRTI"]["uptime"] == 0.0


def test_quote_report_counts_crossed_null_and_bound_quotes(tmp_path):
    store = EventStore(tmp_path)
    store.append("market_ticks", [_quote(DAY1), _quote(DAY1 + 1, bid=0.6, ask=0.5), _quote(DAY1 + 2, bid=0.0, ask=1.0),
                                  {**_quote(DAY1 + 3), "yes_bid_dollars": None}])
    r = quote_report(store)
    assert (r["rows"], r["null_quotes"], r["crossed"], r["at_bounds"], r["markets"]) == (4, 1, 1, 1, 1)
