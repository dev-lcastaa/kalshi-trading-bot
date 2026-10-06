"""Buffered, non-blocking writer into the event store.

Feed handlers call `put()` (synchronous, O(1)); a background task flushes buffers to Parquet in a
worker thread. A failed write keeps the rows and retries; if the buffer ever hits its cap the newest
rows are dropped and counted, never blocking a feed. A crash loses at most one flush interval.
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..store import EventStore

logger = logging.getLogger(__name__)


class BatchSink:
    def __init__(self, store: EventStore, flush_interval_sec: float = 30.0, flush_rows: int = 200_000,
                 max_buffer_rows: int = 2_000_000):
        self.store = store
        self.flush_interval_sec = flush_interval_sec
        self.flush_rows = flush_rows
        self.max_buffer_rows = max_buffer_rows
        self._buffers: dict[str, list[dict]] = {}
        self._buffered = 0
        self._last_flush = time.monotonic()
        self.written: dict[str, int] = {}
        self.dropped = 0
        self.failures = 0
        self.last_flush_ms: int | None = None

    @property
    def buffered(self) -> int:
        return self._buffered

    def put(self, table: str, row: dict | None) -> None:
        if row is None:
            return
        if self._buffered >= self.max_buffer_rows:
            self.dropped += 1
            return
        self._buffers.setdefault(table, []).append(row)
        self._buffered += 1

    async def flush(self) -> int:
        """Write everything buffered. Returns rows written; on failure the rows go back in the buffer."""
        pending, self._buffers, self._buffered = self._buffers, {}, 0
        total = 0
        for table, rows in pending.items():
            if not rows:
                continue
            try:
                await asyncio.to_thread(self.store.append, table, rows)
            except Exception:
                self.failures += 1
                logger.exception("Event store write failed for %s (%d rows); will retry", table, len(rows))
                self._buffers.setdefault(table, [])[:0] = rows  # keep original order, ahead of newer rows
                self._buffered += len(rows)
                continue
            self.written[table] = self.written.get(table, 0) + len(rows)
            total += len(rows)
        if total:
            self.last_flush_ms = int(time.time() * 1000)
        self._last_flush = time.monotonic()
        return total

    async def run(self) -> None:
        try:
            while True:
                await asyncio.sleep(1.0)
                if self._buffered >= self.flush_rows or time.monotonic() - self._last_flush >= self.flush_interval_sec:
                    await self.flush()
        except asyncio.CancelledError:
            await self.flush()  # final flush on shutdown
            raise
