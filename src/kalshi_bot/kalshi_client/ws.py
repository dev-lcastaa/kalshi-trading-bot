"""Async WebSocket client for Kalshi's single multiplexed connection.

Handles auth handshake, subscribe/resubscribe, reconnect with backoff, and
per-subscription sequence-number gap detection (logged, not auto-healed).
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

from ..auth import KalshiAuth

logger = logging.getLogger(__name__)

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


class KalshiWsClient:
    def __init__(self, ws_url: str, auth: KalshiAuth, on_message: MessageHandler):
        self._ws_url = ws_url
        self._auth = auth
        self._on_message = on_message
        self._id_counter = itertools.count(1)
        self._pending_subscriptions: list[dict[str, Any]] = []
        self._seq_by_sid: dict[int, int] = {}
        self._stop = False

    def _next_id(self) -> int:
        return next(self._id_counter)

    def queue_subscribe(
        self,
        channels: list[str],
        market_tickers: list[str] | None = None,
        index_ids: list[str] | None = None,
        underlying_tickers: list[str] | None = None,
    ) -> None:
        """Queue a subscribe command, sent on connect and every reconnect."""
        params: dict[str, Any] = {"channels": channels}
        if market_tickers:
            params["market_tickers"] = market_tickers
        if index_ids:
            params["index_ids"] = index_ids
        if underlying_tickers:
            params["underlying_tickers"] = underlying_tickers
        self._pending_subscriptions.append(params)

    def stop(self) -> None:
        self._stop = True

    async def run_forever(self, max_backoff: float = 30.0) -> None:
        backoff = 1.0
        while not self._stop:
            try:
                await self._run_once()
                backoff = 1.0
            except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                logger.warning("Kalshi WS disconnected (%s); reconnecting in %.1fs", exc, backoff)
            except Exception:
                logger.exception("Kalshi WS crashed; reconnecting in %.1fs", backoff)
            if self._stop:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

    async def _run_once(self) -> None:
        headers = self._auth.headers("GET", "/trade-api/ws/v2")
        async with websockets.connect(self._ws_url, additional_headers=headers) as ws:
            logger.info("Connected to Kalshi WebSocket (%s)", self._ws_url)
            for params in self._pending_subscriptions:
                await ws.send(json.dumps({"id": self._next_id(), "cmd": "subscribe", "params": params}))
            async for raw in ws:
                if self._stop:
                    return
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("Non-JSON WS message: %r", raw)
                    continue
                self._check_sequence(data)
                await self._on_message(data)

    def _check_sequence(self, data: dict[str, Any]) -> None:
        sid = data.get("sid")
        seq = data.get("seq")
        if sid is None or seq is None:
            return
        last = self._seq_by_sid.get(sid)
        if last is not None and seq != last + 1:
            logger.warning("Sequence gap on sid=%s: expected %s, got %s", sid, last + 1, seq)
        self._seq_by_sid[sid] = seq
