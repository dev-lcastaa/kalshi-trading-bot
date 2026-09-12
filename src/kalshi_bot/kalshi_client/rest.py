"""Thin async REST client for Kalshi's public/authenticated GET endpoints.

Only read endpoints are implemented - this bot never places orders.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ..auth import KalshiAuth

logger = logging.getLogger(__name__)

_MAX_RETRIES = 5
_INITIAL_BACKOFF_SEC = 0.5


class KalshiRestClient:
    def __init__(self, base_url: str, auth: KalshiAuth | None = None, timeout: float = 10.0):
        self._base_url = base_url.rstrip("/")
        self._auth = auth
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "KalshiRestClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = self._auth.headers("GET", f"/trade-api/v2{path}") if self._auth else {}
        backoff = _INITIAL_BACKOFF_SEC
        for attempt in range(_MAX_RETRIES + 1):
            response = await self._client.get(path, params=params, headers=headers)
            if response.status_code != 429:
                response.raise_for_status()
                return response.json()
            if attempt == _MAX_RETRIES:
                response.raise_for_status()
            logger.warning("429 from %s; backing off %.1fs (attempt %d/%d)", path, backoff, attempt + 1, _MAX_RETRIES)
            await asyncio.sleep(backoff)
            backoff *= 2
        raise RuntimeError("unreachable")  # pragma: no cover

    # --- Series / Events / Markets -------------------------------------------------

    async def get_series_list(
        self, category: str | None = None, tags: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if category:
            params["category"] = category
        if tags:
            params["tags"] = tags
        return await self._get("/series", params=params)

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        return await self._get(f"/series/{series_ticker}")

    async def get_markets(
        self,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        status: str | None = None,
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        return await self._get("/markets", params=params)

    async def get_market(self, ticker: str) -> dict[str, Any]:
        return await self._get(f"/markets/{ticker}")

    async def get_event(self, event_ticker: str) -> dict[str, Any]:
        return await self._get(f"/events/{event_ticker}")

    async def get_market_orderbook(self, ticker: str) -> dict[str, Any]:
        return await self._get(f"/markets/{ticker}/orderbook")

    async def get_market_candlesticks(
        self,
        series_ticker: str,
        ticker: str,
        start_ts: int,
        end_ts: int,
        period_interval: int = 1,
    ) -> dict[str, Any]:
        params = {
            "start_ts": start_ts,
            "end_ts": end_ts,
            "period_interval": period_interval,
        }
        return await self._get(
            f"/series/{series_ticker}/markets/{ticker}/candlesticks", params=params
        )

    # --- CF Benchmarks passthrough (crypto settlement source) ----------------------

    async def cfbenchmarks_values(self, index_id: str) -> dict[str, Any]:
        return await self._get("/cfbenchmarks/values", params={"id": index_id})

    async def cfbenchmarks_history(
        self, index_id: str, timespan: str, timestamp: str
    ) -> dict[str, Any]:
        params = {"id": index_id, "timespan": timespan, "timestamp": timestamp}
        return await self._get("/cfbenchmarks/history/values", params=params)
