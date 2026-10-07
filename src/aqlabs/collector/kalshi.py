"""Kalshi feeds: quotes, full trade tape, order-book depth, the CF Benchmarks index and market metadata/results.

The WebSocket subscription has to change whenever the open market set rotates. To avoid dropping
ticks during that swap, the new connection is opened and confirmed *before* the old one is closed;
the resulting duplicates are removed by per-stream dedupe. Each connection owns its own order book so
two connections can never double-apply deltas.
"""
from __future__ import annotations

import asyncio
import functools
import itertools
import logging
import time
from collections import OrderedDict

from kalshi_bot.auth import KalshiAuth
from kalshi_bot.kalshi_client.rest import KalshiRestClient
from kalshi_bot.kalshi_client.ws import KalshiWsClient
from kalshi_bot.market_discovery import find_15min_markets

from .book import BookTracker
from .parsers import parse_index, parse_market_meta, parse_quote, parse_trade

logger = logging.getLogger(__name__)

_DISCOVERY_INTERVAL_SEC = 60
_SETTLEMENT_POLL_SEC = 30
_RESULT_GIVE_UP_MS = 6 * 3600 * 1000
_TRADE_DEDUPE_SIZE = 100_000


async def sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


class _Conn:
    def __init__(self, conn_id: int, tickers: list[str], expected_subscriptions: int):
        self.id = conn_id
        self.tickers = tuple(tickers)
        self.expected = expected_subscriptions
        self.book = BookTracker()
        self.sid_channel: dict[int, str] = {}
        self.seq: dict[int, int] = {}
        self.subscribed = 0
        self.ready = asyncio.Event()
        self.needs_resync = False
        self.client: KalshiWsClient | None = None
        self.task: asyncio.Task | None = None


class KalshiFeed:
    def __init__(self, settings, emit, beat, health, store=None):
        self.settings = settings
        self.emit = emit
        self.beat = beat
        self.health = health
        self.store = store
        self.auth = KalshiAuth.from_file(settings.key_id, settings.private_key_path)
        self.rest = KalshiRestClient(settings.rest_base, self.auth)
        self.coin_to_index = dict(zip(settings.coin_ticks, settings.index_ids))
        self.markets: dict[str, dict] = {}
        self.pending: dict[str, int] = {}  # ticker -> close_ms, awaiting a settlement result
        self.active: _Conn | None = None
        self.seq_gaps = 0
        self._conn_ids = itertools.count(1)
        self._wake = asyncio.Event()
        self._last_quote_ts: dict[str, int] = {}
        self._last_index_ts: dict[str, int] = {}
        self._trade_ids: OrderedDict[str, None] = OrderedDict()

    # ------------------------------------------------------------------ lifecycle
    async def run(self, stop: asyncio.Event) -> None:
        self._resume_pending()
        tasks = [asyncio.create_task(c) for c in (
            self._discovery_loop(stop), self._supervisor(stop), self._depth_loop(stop), self._settlement_loop(stop))]
        try:
            await stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.active:
                await self._close_conn(self.active)
            await self.rest.aclose()

    def _index_for_ticker(self, ticker: str) -> str | None:
        # series tickers look like KX<COIN>15M-<date>-<strike>; match the prefix, not a substring of the whole ticker
        for coin, index_id in self.coin_to_index.items():
            if ticker.upper().startswith(f"KX{coin.upper()}15M"):
                return index_id
        return None

    def _resume_pending(self) -> None:
        """After a restart, keep waiting for results of markets that closed while we were down."""
        if self.store is None or not self.store.has_data("market_meta"):
            return
        now = int(time.time() * 1000)
        rows = self.store.connect().execute(
            "select ticker, any_value(close_ts_ms) from market_meta group by ticker "
            "having max(result) filter (where result in ('yes','no')) is null and any_value(close_ts_ms) > ?",
            [now - _RESULT_GIVE_UP_MS]).fetchall()
        for ticker, close_ms in rows:
            self.pending[ticker] = int(close_ms)
        if rows:
            logger.info("Resuming result tracking for %d markets", len(rows))

    # ------------------------------------------------------------------ discovery + settlement
    async def _discovery_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self._discover()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Market discovery failed")
            await sleep_or_stop(stop, _DISCOVERY_INTERVAL_SEC)

    async def _discover(self) -> None:
        now = int(time.time() * 1000)
        found: dict[str, dict] = {}
        for market in await find_15min_markets(self.rest, self.settings.coin_ticks):
            index_id = self._index_for_ticker(market.get("ticker", ""))
            row = parse_market_meta(market, index_id, now) if index_id else None
            if row:
                found[row["ticker"]] = row
        for ticker, row in found.items():
            if ticker not in self.markets and ticker not in self.pending:
                self.emit("market_meta", row)
            if ticker not in self.markets:
                self.pending.setdefault(ticker, row["close_ts_ms"])
        changed = set(found) != set(self.markets)
        self.markets = found
        self.health.set_active("kalshi/quotes", bool(found))
        self.health.set_active("kalshi/orderbook", bool(found))
        if changed:
            logger.info("Open markets: %s", sorted(found))
            self._wake.set()

    async def _settlement_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await sleep_or_stop(stop, _SETTLEMENT_POLL_SEC)
            now = int(time.time() * 1000)
            for ticker, close_ms in list(self.pending.items()):
                if now < close_ms + 30_000:
                    continue
                if now > close_ms + _RESULT_GIVE_UP_MS:
                    logger.warning("No settlement result for %s after 6h; giving up", ticker)
                    del self.pending[ticker]
                    continue
                try:
                    market = (await self.rest.get_market(ticker)).get("market", {})
                    index_id = self._index_for_ticker(ticker)
                    row = parse_market_meta(market, index_id, now) if index_id else None
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Result lookup failed for %s", ticker)
                    continue
                if row and row["result"]:
                    self.emit("market_meta", row)
                    del self.pending[ticker]

    # ------------------------------------------------------------------ WebSocket connections
    async def _supervisor(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                tickers = sorted(self.markets)
                stale = self.active is None or list(self.active.tickers) != tickers or self.active.needs_resync
                if stale and (tickers or self.active is None):
                    new = await self._open_conn(tickers)
                    old, self.active = self.active, new
                    if old:
                        await self._close_conn(old)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Kalshi WebSocket supervisor error")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    async def _open_conn(self, tickers: list[str]) -> _Conn:
        # One subscription reply per channel: ticker, trade, orderbook_delta (+ the index channel).
        conn = _Conn(next(self._conn_ids), tickers, expected_subscriptions=4 if tickers else 1)
        client = KalshiWsClient(self.settings.ws_url, self.auth, on_message=functools.partial(self._on_message, conn))
        if tickers:
            client.queue_subscribe(["ticker", "trade", "orderbook_delta"], market_tickers=tickers)
        client.queue_subscribe(["cfbenchmarks_value"], index_ids=self.settings.index_ids)
        conn.client = client
        conn.task = asyncio.create_task(client.run_forever())
        try:
            await asyncio.wait_for(conn.ready.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            logger.warning("Kalshi connection %d not fully subscribed after 15s; continuing", conn.id)
        logger.info("Kalshi connection %d live for %s", conn.id, list(tickers))
        return conn

    async def _close_conn(self, conn: _Conn) -> None:
        if conn.client:
            conn.client.stop()
        if conn.task:
            conn.task.cancel()
            await asyncio.gather(conn.task, return_exceptions=True)

    async def _on_message(self, conn: _Conn, data: dict) -> None:
        kind, msg = data.get("type"), data.get("msg") or {}
        now = int(time.time() * 1000)
        if kind == "subscribed":
            channel, sid = msg.get("channel"), msg.get("sid")
            conn.sid_channel[sid] = channel
            conn.seq.pop(sid, None)
            conn.subscribed += 1
            if channel == "orderbook_delta":
                conn.book.invalidate()  # a (re)subscription is followed by fresh snapshots
            if conn.subscribed >= conn.expected:
                conn.ready.set()
            return
        if data.get("seq") is not None:
            self._check_sequence(conn, data.get("sid"), data["seq"], now)
        if kind == "ticker":
            row = parse_quote(msg, now)
            if row and row["ts_ms"] > self._last_quote_ts.get(row["market_ticker"], -1):
                self._last_quote_ts[row["market_ticker"]] = row["ts_ms"]
                self.emit("market_ticks", row)
                self.beat("kalshi/quotes")
        elif kind == "trade":
            row = parse_trade(msg, now)
            if row and row["trade_id"] not in self._trade_ids:
                self._trade_ids[row["trade_id"]] = None
                if len(self._trade_ids) > _TRADE_DEDUPE_SIZE:
                    self._trade_ids.popitem(last=False)
                self.emit("trades", row)
                self.beat("kalshi/trades")
        elif kind == "cfbenchmarks_value":
            row = parse_index(msg, now)
            if row and row["ts_ms"] > self._last_index_ts.get(row["index_id"], -1):
                self._last_index_ts[row["index_id"]] = row["ts_ms"]
                self.emit("index_ticks", row)
                self.beat(f"kalshi/index/{row['index_id']}")
        elif kind == "orderbook_snapshot":
            conn.book.snapshot(msg, data.get("seq"))
            self.beat("kalshi/orderbook")
        elif kind == "orderbook_delta":
            conn.book.delta(msg, data.get("seq"))
            self.beat("kalshi/orderbook")
        elif kind == "error":
            logger.warning("Kalshi WS error: %s", msg)

    def _check_sequence(self, conn: _Conn, sid, seq: int, now_ms: int) -> None:
        last = conn.seq.get(sid)
        conn.seq[sid] = seq
        if last is None or seq == last + 1:
            return
        channel = conn.sid_channel.get(sid, "unknown")
        self.seq_gaps += 1
        logger.warning("Sequence gap on %s (sid=%s): expected %s, got %s", channel, sid, last + 1, seq)
        self.emit("heartbeats", {"feed": f"kalshi/seq_gap/{channel}", "ts_ms": now_ms, "events": seq - last - 1,
                                 "silent_ms": 0, "note": f"expected {last + 1}, got {seq}"})
        if channel == "orderbook_delta":
            conn.book.invalidate()
            conn.needs_resync = True  # the supervisor opens a fresh connection to get new snapshots
            self._wake.set()

    async def _depth_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await sleep_or_stop(stop, 1.0)
            conn = self.active
            if conn is None or not conn.ready.is_set():
                continue
            now = int(time.time() * 1000)
            for ticker in conn.tickers:
                self.emit("orderbook_depth", conn.book.depth_row(ticker, now))
