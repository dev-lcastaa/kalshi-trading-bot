import asyncio
import json

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from aqlabs.collector import parsers as P  # noqa: E402
from aqlabs.collector.book import BookTracker  # noqa: E402
from aqlabs.collector.health import FeedHealth  # noqa: E402
from aqlabs.collector.healthcheck import check  # noqa: E402
from aqlabs.collector.sink import BatchSink  # noqa: E402
from aqlabs.store import EventStore  # noqa: E402

NOW = 1_791_257_412_100
DAY = 1_791_257_412_000

# Real messages captured from Kalshi / exchange public feeds on 2026-10-06.
KALSHI_TICKER = {"market_id": "01a1", "market_ticker": "KXBTC15M-26OCT052345-45", "price_dollars": "0.4200",
                 "yes_bid_dollars": "0.4100", "yes_ask_dollars": "0.4200", "volume_fp": "83606.28",
                 "open_interest_fp": "56756.10", "yes_bid_size_fp": "11426.13", "yes_ask_size_fp": "2634.60",
                 "ts_ms": 1791257412026}
KALSHI_TRADE = {"trade_id": "07253249-1cd1", "market_ticker": "KXBTC15M-26OCT052345-45", "yes_price_dollars": "0.4100",
                "no_price_dollars": "0.5900", "count_fp": "1.64", "taker_side": "no", "is_block_trade": False,
                "ts_ms": 1791257411082}
KALSHI_INDEX = {"index_id": "BRTI", "received_at": 1791257412077,
                "data": "{\"type\":\"value\",\"time\":1791257412000,\"id\":\"BRTI\",\"value\":\"85447.77\"}",
                "avg_60s_data": {"value": "85430.50000000", "window_size": 0}}


def test_parse_quote_trade_and_index():
    q = P.parse_quote(KALSHI_TICKER, NOW)
    assert (q["yes_bid_dollars"], q["yes_ask_dollars"], q["yes_bid_size"], q["received_at_ms"]) == (0.41, 0.42, 11426.13, NOW)
    t = P.parse_trade(KALSHI_TRADE, NOW)
    assert (t["count"], t["taker_side"], t["is_block_trade"], t["no_price_dollars"]) == (1.64, "no", 0, 0.59)
    i = P.parse_index(KALSHI_INDEX, NOW)
    assert (i["value"], i["avg_60s"], i["exchange_ts_ms"], i["ts_ms"]) == (85447.77, 85430.5, 1791257412000, 1791257412077)


def test_parsers_reject_incomplete_messages():
    assert P.parse_quote({"market_ticker": "X"}, NOW) is None
    assert P.parse_trade({"market_ticker": "X", "ts_ms": 1}, NOW) is None
    assert P.parse_index({"index_id": "BRTI"}, NOW) is None


def test_index_falls_back_to_the_60s_average_when_the_value_payload_is_bad():
    msg = {**KALSHI_INDEX, "data": "not json"}
    row = P.parse_index(msg, NOW)
    assert row["value"] == 85430.5 and row["exchange_ts_ms"] is None


def test_market_meta_result_only_when_settled():
    market = {"ticker": "KXBTC15M-X", "floor_strike": 85623.37, "close_time": "2026-10-06T04:00:00Z", "status": "active",
              "result": ""}
    assert P.parse_market_meta(market, "BRTI", NOW)["result"] is None
    done = {**market, "status": "finalized", "result": "no", "expiration_value": "85515.99"}
    row = P.parse_market_meta(done, "BRTI", NOW)
    assert (row["result"], row["expiration_value"], row["close_ts_ms"]) == ("no", 85515.99, 1791259200000)
    assert P.parse_market_meta({**market, "floor_strike": None}, "BRTI", NOW) is None


def test_coinbase_kraken_and_bitstamp_parsing():
    cb = {"channel": "ticker", "events": [{"tickers": [{"product_id": "BTC-USD", "price": "85596.81",
          "best_bid": "85596.81", "best_ask": "85596.82", "volume_24_h": "5091.8", "time": "2026-10-06T03:30:00Z"}]}]}
    assert P.parse_coinbase(cb, NOW)[0]["index_id"] == "BRTI"
    kr = {"channel": "ticker", "data": [{"symbol": "BTC/USD", "last": 85541.1, "bid": 85541.0, "ask": 85541.2,
          "volume": 10.0, "timestamp": "2026-10-06T03:30:00.000000Z"}]}
    assert P.parse_kraken(kr, NOW)[0]["symbol"] == "BTC/USD"
    xbt = {"channel": "ticker", "data": [{"symbol": "XBT/USD", "last": 1, "bid": 1, "ask": 1}]}
    assert P.parse_kraken(xbt, NOW) == []  # the legacy symbol that Kraken rejects must never be treated as BTC
    bs = {"event": "trade", "channel": "live_trades_solusd", "data": {"price": 119.897, "timestamp": "1791257508",
          "microtimestamp": "1791257508194000"}}
    row = P.parse_bitstamp(bs, NOW)[0]
    assert (row["index_id"], row["ts_ms"], row["bid"]) == ("SOLUSD_RTI", 1791257508194, None)
    assert P.parse_bitstamp({"event": "bts:subscription_succeeded"}, NOW) == []


def test_crossed_exchange_quotes_are_dropped():
    cb = {"channel": "ticker", "events": [{"tickers": [{"product_id": "BTC-USD", "price": "1", "best_bid": "2", "best_ask": "1"}]}]}
    assert P.parse_coinbase(cb, NOW) == []


def test_book_applies_snapshot_then_deltas_and_reports_best_levels_first():
    b = BookTracker()
    b.snapshot({"market_ticker": "M", "yes_dollars_fp": [["0.40", "10"], ["0.41", "5"]], "no_dollars_fp": [["0.58", "7"]]}, 1)
    assert b.delta({"market_ticker": "M", "side": "yes", "price_dollars": "0.41", "delta_fp": "-5"}, 2)  # level removed
    assert b.delta({"market_ticker": "M", "side": "no", "price_dollars": "0.59", "delta_fp": "3"}, 3)
    row = b.depth_row("M", 1000)
    assert json.loads(row["yes_bids"]) == [[0.4, 10.0]]
    assert json.loads(row["no_bids"]) == [[0.59, 3.0], [0.58, 7.0]]
    assert (row["yes_total"], row["no_total"], row["seq"]) == (10.0, 10.0, 3)


def test_book_ignores_deltas_without_a_snapshot_and_after_invalidation():
    b = BookTracker()
    assert not b.delta({"market_ticker": "M", "side": "yes", "price_dollars": "0.4", "delta_fp": "1"}, 1)
    b.snapshot({"market_ticker": "M"}, 1)
    assert b.depth_row("M", 1)["yes_bids"] == "[]"  # empty snapshot is a valid empty book
    b.invalidate()
    assert b.depth_row("M", 1) is None


def test_sink_flushes_rows_to_the_store_and_ignores_none(tmp_path):
    store = EventStore(tmp_path)
    sink = BatchSink(store)
    sink.put("trades", P.parse_trade(KALSHI_TRADE, NOW))
    sink.put("trades", None)
    assert sink.buffered == 1
    assert asyncio.run(sink.flush()) == 1
    assert store.count("trades") == 1 and sink.buffered == 0 and sink.written == {"trades": 1}


def test_sink_keeps_rows_when_a_write_fails_and_retries(tmp_path):
    class Flaky(EventStore):
        calls = 0

        def append(self, table, rows):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise OSError("disk full")
            return super().append(table, rows)

    sink = BatchSink(Flaky(tmp_path))
    sink.put("trades", P.parse_trade(KALSHI_TRADE, NOW))
    assert asyncio.run(sink.flush()) == 0 and sink.failures == 1 and sink.buffered == 1
    assert asyncio.run(sink.flush()) == 1 and sink.buffered == 0


def test_sink_drops_and_counts_rows_when_the_buffer_is_full(tmp_path):
    sink = BatchSink(EventStore(tmp_path), max_buffer_rows=2)
    for _ in range(5):
        sink.put("trades", P.parse_trade(KALSHI_TRADE, NOW))
    assert sink.buffered == 2 and sink.dropped == 3


def test_health_alerts_once_per_outage_and_on_recovery():
    h = FeedHealth(started_ms=0)
    h.register("coinbase/BRTI", max_silent_ms=30_000)
    assert h.evaluate(10_000) == []
    down = h.evaluate(40_000)
    assert [a["state"] for a in down] == ["down"]
    assert h.evaluate(50_000) == []  # no repeat alert during the same outage
    h.beat("coinbase/BRTI", now_ms=55_000)
    assert [a["state"] for a in h.evaluate(56_000)] == ["recovered"]


def test_a_feed_that_never_ticks_alerts_and_inactive_feeds_do_not():
    h = FeedHealth(started_ms=0)
    h.register("kraken/BRTI", max_silent_ms=60_000)
    h.register("kalshi/quotes", max_silent_ms=60_000, active=False)
    alerts = h.evaluate(120_000)
    assert [a["feed"] for a in alerts] == ["kraken/BRTI"]


def test_heartbeat_rows_report_events_since_the_previous_heartbeat():
    h = FeedHealth(started_ms=0)
    h.register("f", max_silent_ms=10_000)
    h.beat("f", now_ms=1_000, n=5)
    assert h.heartbeat_rows(2_000)[0]["events"] == 5
    h.beat("f", now_ms=2_500, n=2)
    assert h.heartbeat_rows(3_000)[0]["events"] == 2


def test_compaction_merges_parts_without_losing_rows(tmp_path):
    store = EventStore(tmp_path)
    for i in range(4):
        store.append("trades", [P.parse_trade({**KALSHI_TRADE, "trade_id": f"t{i}", "ts_ms": DAY + i}, NOW)])
    date = store.dates("trades")[0]
    before = store.manifest()["trades"]["fingerprint"]
    result = store.compact("trades", date)
    assert result["merged"] == 4 and result["rows"] == 4
    assert len(list((tmp_path / "trades" / f"date={date}").glob("part-*.parquet"))) == 1
    assert store.count("trades") == 4 and store.manifest()["trades"]["fingerprint"] == before


def test_healthcheck_reports_stale_dropped_and_down(tmp_path):
    import time
    path = tmp_path / "s.json"
    now = int(time.time() * 1000)
    path.write_text(json.dumps({"ts_ms": now, "dropped_rows": 0, "feeds": {"a": {"down": False}}}))
    assert check(str(path)) == (True, "ok")
    path.write_text(json.dumps({"ts_ms": now - 120_000, "dropped_rows": 0, "feeds": {}}))
    assert not check(str(path))[0]
    path.write_text(json.dumps({"ts_ms": now, "dropped_rows": 3, "feeds": {}}))
    assert not check(str(path))[0]
    path.write_text(json.dumps({"ts_ms": now, "dropped_rows": 0, "feeds": {"kraken/BRTI": {"down": True}}}))
    assert "kraken/BRTI" in check(str(path))[1]
    assert not check(str(tmp_path / "missing.json"))[0]


def test_legacy_data_without_new_columns_reads_back_with_nulls(tmp_path):
    import gzip
    csv = tmp_path / "index_ticks.csv.gz"
    with gzip.open(csv, "wt") as fh:
        fh.write(f"index_id,ts_ms,value\nBRTI,{DAY},85000.5\n")
    store = EventStore(tmp_path / "s")
    store.import_csv("index_ticks", csv)
    store.append("index_ticks", [P.parse_index(KALSHI_INDEX, NOW)])  # new-schema file next to the legacy one
    rows = store.connect().execute("select value, avg_60s from index_ticks order by ts_ms").fetchall()
    assert rows == [(85000.5, None), (85447.77, 85430.5)]
    assert store.import_csv("index_ticks", csv, since_ms=DAY)["csv_rows"] == 0  # --since-ms skips imported rows


def test_a_store_holding_only_legacy_files_still_reports_and_fingerprints(tmp_path):
    """Files written before received_at_ms/avg_60s existed must not break manifest(), reports or reads."""
    import duckdb

    folder = tmp_path / "index_ticks" / "date=2026-10-06"
    folder.mkdir(parents=True)
    duckdb.connect().execute(
        f"copy (select 'BRTI' as index_id, {DAY}::BIGINT as ts_ms, 85000.5::DOUBLE as value) "
        f"to '{(folder / 'part-legacy.parquet').as_posix()}' (format parquet)")
    store = EventStore(tmp_path)
    assert store.manifest()["index_ticks"]["rows"] == 1
    assert store.connect().execute("select value, avg_60s, received_at_ms from index_ticks").fetchall() == [(85000.5, None, None)]
    from aqlabs.store.quality import feed_report
    assert {r["feed"]: r["ticks"] for r in feed_report(store)}["index/BRTI"] == 1


def test_feed_report_applies_a_separate_gap_limit_to_each_feed(tmp_path):
    from aqlabs.store.quality import feed_report

    store = EventStore(tmp_path)
    rows = []
    for source in ("coinbase", "bitstamp"):  # both tick every 60 s; coinbase tolerates 30 s, bitstamp 120 s
        rows += [{"source": source, "symbol": "x", "index_id": "BRTI", "ts_ms": DAY + i * 60_000,
                  "received_at_ms": DAY + i * 60_000, "price": 100.0, "bid": None, "ask": None, "volume_24h": None}
                 for i in range(5)]
    store.append("external_ticks", rows)
    report = {r["feed"]: r for r in feed_report(store)}
    assert report["coinbase/BRTI"]["gaps"] == 4 and report["coinbase/BRTI"]["limit_sec"] == 30
    assert report["bitstamp/BRTI"]["gaps"] == 0 and report["bitstamp/BRTI"]["uptime"] == 1.0
    overridden = {r["feed"]: r for r in feed_report(store, max_gap_ms=30_000)}
    assert overridden["bitstamp/BRTI"]["gaps"] == 4  # an explicit limit applies to every feed


def test_collector_alert_limits_are_the_same_numbers_the_report_uses():
    from aqlabs.collector.main import SILENCE_LIMITS, build_health
    from aqlabs.store.quality import FEED_GAP_LIMIT_SEC

    for feed, seconds in FEED_GAP_LIMIT_SEC.items():
        name = f"kalshi/{feed}" if feed.startswith("index/") else feed
        assert SILENCE_LIMITS[name] == seconds
    assert set(build_health().feeds) == set(SILENCE_LIMITS)
