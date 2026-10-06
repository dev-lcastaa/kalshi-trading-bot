"""External exchange feeds: Coinbase, Kraken and Bitstamp public WebSockets -> external_ticks rows."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable

import websockets

from .parsers import (BITSTAMP_CHANNELS, COINBASE_PRODUCTS, KRAKEN_PRODUCTS, parse_bitstamp, parse_coinbase,
                      parse_kraken)

logger = logging.getLogger(__name__)

Emit = Callable[[str, dict | None], None]
Beat = Callable[[str], None]


class Throttle:
    """Keeps at most one row per key every `min_interval_ms` (0 = keep everything)."""

    def __init__(self, min_interval_ms: int):
        self.min_interval_ms = min_interval_ms
        self._last: dict[str, int] = {}

    def allow(self, key: str, now_ms: int) -> bool:
        if self.min_interval_ms <= 0:
            return True
        if now_ms - self._last.get(key, -10**18) < self.min_interval_ms:
            return False
        self._last[key] = now_ms
        return True


async def ws_loop(name: str, url: str, subscribe: list[dict], on_message: Callable[[dict, int], None],
                  stop: asyncio.Event, idle_timeout_sec: float = 60.0) -> None:
    """Connect, subscribe and dispatch messages; reconnect with backoff on any failure or idle stall."""
    backoff = 1.0
    while not stop.is_set():
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as socket:
                for message in subscribe:
                    await socket.send(json.dumps(message))
                logger.info("%s connected", name)
                backoff = 1.0
                while not stop.is_set():
                    raw = await asyncio.wait_for(socket.recv(), timeout=idle_timeout_sec)
                    received_at_ms = int(time.time() * 1000)
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    on_message(message, received_at_ms)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning("%s: no messages for %.0fs; reconnecting", name, idle_timeout_sec)
        except Exception as exc:
            logger.warning("%s disconnected (%s); retrying in %.0fs", name, exc, backoff)
        if stop.is_set():
            return
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


def _make_handler(source: str, parse, emit: Emit, beat: Beat, throttle: Throttle):
    def handle(message: dict, received_at_ms: int) -> None:
        for row in parse(message, received_at_ms):
            beat(f"{source}/{row['index_id']}")  # liveness counts every tick, even throttled ones
            if throttle.allow(f"{source}/{row['symbol']}", row["ts_ms"]):
                emit("external_ticks", row)
    return handle


async def run_coinbase(emit: Emit, beat: Beat, stop: asyncio.Event, min_interval_ms: int = 250) -> None:
    sub = [{"type": "subscribe", "product_ids": list(COINBASE_PRODUCTS), "channel": "ticker"}]
    await ws_loop("coinbase", "wss://advanced-trade-ws.coinbase.com", sub,
                  _make_handler("coinbase", parse_coinbase, emit, beat, Throttle(min_interval_ms)), stop)


async def run_kraken(emit: Emit, beat: Beat, stop: asyncio.Event, min_interval_ms: int = 250) -> None:
    sub = [{"method": "subscribe", "params": {"channel": "ticker", "symbol": list(KRAKEN_PRODUCTS)}}]
    await ws_loop("kraken", "wss://ws.kraken.com/v2", sub,
                  _make_handler("kraken", parse_kraken, emit, beat, Throttle(min_interval_ms)), stop)


async def run_bitstamp(emit: Emit, beat: Beat, stop: asyncio.Event, min_interval_ms: int = 250) -> None:
    sub = [{"event": "bts:subscribe", "data": {"channel": ch}} for ch in BITSTAMP_CHANNELS]
    # Bitstamp is trade-driven, so SOL can legitimately be quiet for minutes: allow a long idle window.
    await ws_loop("bitstamp", "wss://ws.bitstamp.net", sub,
                  _make_handler("bitstamp", parse_bitstamp, emit, beat, Throttle(min_interval_ms)), stop,
                  idle_timeout_sec=300.0)
