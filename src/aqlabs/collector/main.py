"""Collector entrypoint: `python -m aqlabs.collector.main`.

Environment (all optional except the Kalshi credentials the bot already uses):
    COLLECTOR_ROOT                       event store directory            (default /data/eventstore)
    COLLECTOR_FLUSH_SEC                  write buffers to disk every N s  (default 30)
    COLLECTOR_EXTERNAL_MIN_INTERVAL_MS   max one exchange tick per symbol per N ms (default 250, 0 = keep all)
    COLLECTOR_ALERT_WEBHOOK              POST {"text": ...} here when a feed goes silent or recovers
    COLLECTOR_MIN_FREE_GB                warn when free disk falls below this (default 10)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import shutil
import signal
import time
from pathlib import Path

import httpx

from kalshi_bot.config import Settings

from ..store import EventStore
from ..store.quality import FEED_GAP_LIMIT_SEC
from .exchanges import run_bitstamp, run_coinbase, run_kraken
from .health import FeedHealth
from .kalshi import KalshiFeed, sleep_or_stop
from .sink import BatchSink

logger = logging.getLogger("aqlabs.collector")

HEARTBEAT_EVERY_SEC = 60
STATUS_EVERY_SEC = 10
COMPACT_EVERY_SEC = 3600

# Seconds of silence before a feed raises an alert. Exchange and index limits come from the store's
# per-feed gap limits so the alerts and the quality report always agree.
SILENCE_LIMITS = {
    "kalshi/quotes": 60, "kalshi/trades": 180, "kalshi/orderbook": 60,
    **{(f"kalshi/{name}" if name.startswith("index/") else name): seconds
       for name, seconds in FEED_GAP_LIMIT_SEC.items()},
}


def build_health() -> FeedHealth:
    health = FeedHealth()
    for name, seconds in SILENCE_LIMITS.items():
        health.register(name, seconds * 1000, active=not name.startswith(("kalshi/quotes", "kalshi/orderbook")))
    return health


async def send_alert(webhook: str | None, message: str) -> None:
    logger.error("ALERT: %s", message)
    if not webhook:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(webhook, json={"text": f"[aqlabs-collector] {message}"})
    except Exception:
        logger.warning("Alert webhook failed")


def write_status(path: Path, health: FeedHealth, sink: BatchSink, root: Path, min_free_gb: float) -> dict:
    now = int(time.time() * 1000)
    free_gb = shutil.disk_usage(root).free / 1e9
    status = {"ts_ms": now, "feeds": health.status(now), "buffered_rows": sink.buffered, "dropped_rows": sink.dropped,
              "write_failures": sink.failures, "last_flush_ms": sink.last_flush_ms, "written": sink.written,
              "disk_free_gb": round(free_gb, 1), "disk_low": free_gb < min_free_gb}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(status))
    os.replace(tmp, path)
    return status


async def monitor_loop(stop, health: FeedHealth, sink: BatchSink, root: Path, webhook, min_free_gb: float) -> None:
    last_heartbeat = 0.0
    last_disk_alert = 0.0
    while not stop.is_set():
        now_ms = int(time.time() * 1000)
        for alert in health.evaluate(now_ms):
            await send_alert(webhook, alert["message"])
        if time.monotonic() - last_heartbeat >= HEARTBEAT_EVERY_SEC:
            for row in health.heartbeat_rows(now_ms):
                sink.put("heartbeats", row)
            last_heartbeat = time.monotonic()
        status = write_status(root / "collector_status.json", health, sink, root, min_free_gb)
        if status["disk_low"] and time.monotonic() - last_disk_alert > 3600:
            await send_alert(webhook, f"disk space low: {status['disk_free_gb']} GB free")
            last_disk_alert = time.monotonic()
        await sleep_or_stop(stop, STATUS_EVERY_SEC)


async def compaction_loop(stop, store: EventStore) -> None:
    """Merge the small part files of finished UTC days into one file per table per day."""
    while not stop.is_set():
        await sleep_or_stop(stop, COMPACT_EVERY_SEC)
        today = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
        for table in ("market_ticks", "index_ticks", "external_ticks", "trades", "orderbook_depth", "market_meta",
                      "heartbeats"):
            for date in store.dates(table):
                if date >= today:
                    continue
                try:
                    result = await asyncio.to_thread(store.compact, table, date)
                    if result["merged"]:
                        logger.info("Compacted %s %s: %d files -> 1 (%d rows)", table, date, result["merged"], result["rows"])
                except Exception:
                    logger.exception("Compaction failed for %s %s", table, date)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.load()
    root = Path(os.environ.get("COLLECTOR_ROOT", "/data/eventstore"))
    store = EventStore(root)
    webhook = os.environ.get("COLLECTOR_ALERT_WEBHOOK") or None
    min_free_gb = float(os.environ.get("COLLECTOR_MIN_FREE_GB", "10"))
    min_interval = int(os.environ.get("COLLECTOR_EXTERNAL_MIN_INTERVAL_MS", "250"))

    health = build_health()
    sink = BatchSink(store, flush_interval_sec=float(os.environ.get("COLLECTOR_FLUSH_SEC", "30")))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: Ctrl+C raises KeyboardInterrupt instead
            pass

    kalshi = KalshiFeed(settings, sink.put, health.beat, health, store=store)
    tasks = [asyncio.create_task(c) for c in (
        sink.run(), kalshi.run(stop), monitor_loop(stop, health, sink, root, webhook, min_free_gb),
        compaction_loop(stop, store),
        run_coinbase(sink.put, health.beat, stop, min_interval), run_kraken(sink.put, health.beat, stop, min_interval),
        run_bitstamp(sink.put, health.beat, stop, min_interval))]
    logger.info("Collector started; writing to %s", root)
    try:
        await stop.wait()
    finally:
        logger.info("Collector stopping")
        sink_task = tasks[0]
        for t in tasks[1:]:
            t.cancel()
        await asyncio.gather(*tasks[1:], return_exceptions=True)
        sink_task.cancel()  # its CancelledError handler performs the final flush
        await asyncio.gather(sink_task, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
