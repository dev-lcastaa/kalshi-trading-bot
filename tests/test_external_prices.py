import asyncio

import pytest

from kalshi_bot.external_prices import _enqueue_tick, aggregate_external_prices, persist_external_ticks
from kalshi_bot import external_prices


def test_aggregate_external_prices_filters_stale_rows_and_computes_median():
    rows = [
        {"source": "coinbase", "index_id": "BRTI", "price": 100.0, "received_at_ms": 9_000},
        {"source": "kraken", "index_id": "BRTI", "price": 102.0, "received_at_ms": 9_500},
        {"source": "old", "index_id": "BRTI", "price": 1.0, "received_at_ms": 1_000},
        {"source": "coinbase", "index_id": "SOLUSD_RTI", "price": 50.0, "received_at_ms": 9_000},
    ]

    result = aggregate_external_prices(rows, now_ms=10_000, max_age_ms=1_000)

    assert result["BRTI"]["price"] == 101.0
    assert result["BRTI"]["source_count"] == 2
    assert result["BRTI"]["max_age_ms"] == 1_000
    assert result["BRTI"]["sources"] == ["coinbase", "kraken"]
    assert result["SOLUSD_RTI"]["price"] == 50.0


def test_aggregate_external_prices_has_no_stale_result():
    assert aggregate_external_prices(
        [{"source": "coinbase", "index_id": "BRTI", "price": 100.0, "received_at_ms": 1}],
        now_ms=10_000,
        max_age_ms=5_000,
    ) == {}


def test_external_tick_persistence_interval_is_one_second():
    assert external_prices._PERSIST_INTERVAL_MS == 1_000


@pytest.mark.asyncio
async def test_persistence_worker_batches_queue_items_off_the_receive_loop():
    class Store:
        def __init__(self):
            self.batches = []

        def insert_external_ticks(self, ticks):
            self.batches.append(ticks)

    queue = asyncio.Queue(maxsize=2)
    store = Store()
    _enqueue_tick(queue, {"source": "coinbase", "symbol": "BTC-USD"})
    _enqueue_tick(queue, {"source": "kraken", "symbol": "XBT/USD"})
    worker = asyncio.create_task(persist_external_ticks(store, queue))
    await asyncio.wait_for(queue.join(), timeout=1)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert len(store.batches) == 1
    assert [tick["source"] for tick in store.batches[0]] == ["coinbase", "kraken"]


def test_enqueue_drops_when_bounded_queue_is_full():
    queue = asyncio.Queue(maxsize=1)
    _enqueue_tick(queue, {"source": "coinbase", "symbol": "BTC-USD"})
    _enqueue_tick(queue, {"source": "kraken", "symbol": "XBT/USD"})
    assert queue.qsize() == 1
