"""Public exchange price collection for shadow features.

Coinbase is a reference market, not the Kalshi settlement source. These prices
are recorded for research and are deliberately not fed into the live predictor
by this module.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from statistics import median, pstdev

import websockets

logger = logging.getLogger(__name__)

COINBASE_WS_URL = "wss://advanced-trade-ws.coinbase.com"
PRODUCTS = {"BTC-USD": "BRTI", "SOL-USD": "SOLUSD_RTI"}
KRAKEN_WS_URL = "wss://ws.kraken.com/v2"
KRAKEN_PRODUCTS = {"XBT/USD": "BRTI", "SOL/USD": "SOLUSD_RTI"}


def aggregate_external_prices(rows: list[dict], now_ms: int, max_age_ms: int = 5_000) -> dict[str, dict]:
    """Return median/latest reference prices and short-term dispersion."""
    by_index: dict[str, list[dict]] = {}
    for row in rows:
        if now_ms - row["received_at_ms"] <= max_age_ms and row["price"] > 0:
            by_index.setdefault(row["index_id"], []).append(row)
    result = {}
    for index_id, values in by_index.items():
        prices = [row["price"] for row in values]
        result[index_id] = {
            "price": median(prices),
            "source_count": len({row["source"] for row in values}),
            "sample_count": len(values),
            "max_age_ms": max(now_ms - row["received_at_ms"] for row in values),
            "price_dispersion": pstdev(prices) if len(prices) > 1 else 0.0,
            "sources": sorted({row["source"] for row in values}),
        }
    return result


async def collect_coinbase(store, stop_event: asyncio.Event) -> None:
    """Reconnect Coinbase's public ticker channel until stop_event is set."""
    subscribe = {
        "type": "subscribe",
        "product_ids": list(PRODUCTS),
        "channel": "ticker",
    }
    while not stop_event.is_set():
        try:
            async with websockets.connect(COINBASE_WS_URL, ping_interval=20, ping_timeout=10) as socket:
                await socket.send(json.dumps(subscribe))
                async for raw in socket:
                    message = json.loads(raw)
                    received_at_ms = int(time.time() * 1000)
                    for event in message.get("events", []):
                        for ticker in event.get("tickers", []):
                            product = ticker.get("product_id")
                            index_id = PRODUCTS.get(product)
                            if not index_id:
                                continue
                            try:
                                price = float(ticker["price"])
                                bid = float(ticker["best_bid"])
                                ask = float(ticker["best_ask"])
                            except (KeyError, TypeError, ValueError):
                                continue
                            if not 0 < bid <= ask or not price > 0:
                                continue
                            timestamp_ms = received_at_ms
                            if ticker.get("time"):
                                try:
                                    timestamp_ms = int(datetime.fromisoformat(
                                        ticker["time"].replace("Z", "+00:00")
                                    ).timestamp() * 1000)
                                except (TypeError, ValueError):
                                    timestamp_ms = received_at_ms
                            store.insert_external_tick(
                                source="coinbase", symbol=product, index_id=index_id,
                                ts_ms=timestamp_ms, received_at_ms=received_at_ms,
                                price=price, bid=bid, ask=ask,
                                volume_24h=float(ticker.get("volume_24_h", 0) or 0),
                            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("External Coinbase price feed disconnected; retrying")
            await asyncio.sleep(2)


async def collect_kraken(store, stop_event: asyncio.Event) -> None:
    """Reconnect Kraken's public v2 ticker channel until stop_event is set."""
    subscribe = {
        "method": "subscribe",
        "params": {"channel": "ticker", "symbol": list(KRAKEN_PRODUCTS)},
    }
    while not stop_event.is_set():
        try:
            async with websockets.connect(KRAKEN_WS_URL, ping_interval=20, ping_timeout=10) as socket:
                await socket.send(json.dumps(subscribe))
                async for raw in socket:
                    message = json.loads(raw)
                    if message.get("channel") != "ticker":
                        continue
                    received_at_ms = int(time.time() * 1000)
                    for ticker in message.get("data", []):
                        product = ticker.get("symbol")
                        index_id = KRAKEN_PRODUCTS.get(product)
                        if not index_id:
                            continue
                        try:
                            price = float(ticker["last"])
                            bid = float(ticker["bid"])
                            ask = float(ticker["ask"])
                        except (KeyError, TypeError, ValueError):
                            continue
                        if not 0 < bid <= ask or not price > 0:
                            continue
                        timestamp_ms = received_at_ms
                        if ticker.get("timestamp"):
                            try:
                                timestamp_ms = int(datetime.fromisoformat(
                                    ticker["timestamp"].replace("Z", "+00:00")
                                ).timestamp() * 1000)
                            except (TypeError, ValueError):
                                timestamp_ms = received_at_ms
                        store.insert_external_tick(
                            source="kraken", symbol=product, index_id=index_id,
                            ts_ms=timestamp_ms, received_at_ms=received_at_ms,
                            price=price, bid=bid, ask=ask,
                            volume_24h=float(ticker.get("volume", 0) or 0),
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("External Kraken price feed disconnected; retrying")
            await asyncio.sleep(2)