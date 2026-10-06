"""Pure parsers from raw feed messages to event-store rows. No I/O, so they are fully unit-testable."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

COINBASE_PRODUCTS = {"BTC-USD": "BRTI", "SOL-USD": "SOLUSD_RTI"}
KRAKEN_PRODUCTS = {"BTC/USD": "BRTI", "SOL/USD": "SOLUSD_RTI"}  # v2 API: "XBT/USD" is rejected
BITSTAMP_CHANNELS = {"live_trades_btcusd": ("btcusd", "BRTI"), "live_trades_solusd": ("solusd", "SOLUSD_RTI")}


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # drops NaN


def _iso_ms(value: Any) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


# ------------------------------------------------------------------ Kalshi
def parse_quote(msg: dict, received_at_ms: int) -> dict | None:
    """`ticker` channel message -> market_ticks row."""
    bid, ask = _f(msg.get("yes_bid_dollars")), _f(msg.get("yes_ask_dollars"))
    if msg.get("market_ticker") is None or msg.get("ts_ms") is None or bid is None or ask is None:
        return None
    return {
        "market_ticker": msg["market_ticker"], "ts_ms": int(msg["ts_ms"]), "price_dollars": _f(msg.get("price_dollars")),
        "yes_bid_dollars": bid, "yes_ask_dollars": ask, "yes_bid_size": _f(msg.get("yes_bid_size_fp")),
        "yes_ask_size": _f(msg.get("yes_ask_size_fp")), "volume": _f(msg.get("volume_fp")),
        "open_interest": _f(msg.get("open_interest_fp")), "received_at_ms": received_at_ms,
    }


def parse_trade(msg: dict, received_at_ms: int) -> dict | None:
    """`trade` channel message -> trades row (every public fill)."""
    if msg.get("trade_id") is None or msg.get("market_ticker") is None or msg.get("ts_ms") is None:
        return None
    return {
        "trade_id": msg["trade_id"], "market_ticker": msg["market_ticker"], "ts_ms": int(msg["ts_ms"]),
        "received_at_ms": received_at_ms, "yes_price_dollars": _f(msg.get("yes_price_dollars")),
        "no_price_dollars": _f(msg.get("no_price_dollars")), "count": _f(msg.get("count_fp")),
        "taker_side": msg.get("taker_side"), "is_block_trade": 1 if msg.get("is_block_trade") else 0,
    }


def parse_index(msg: dict, received_at_ms: int) -> dict | None:
    """`cfbenchmarks_value` message -> index_ticks row, keeping Kalshi's own 60 s average."""
    index_id, ts = msg.get("index_id"), msg.get("received_at")
    if index_id is None or ts is None:
        return None
    avg = _f((msg.get("avg_60s_data") or {}).get("value"))
    value, exchange_ts = None, None
    try:
        data = json.loads(msg["data"])
        value, exchange_ts = _f(data.get("value")), data.get("time")
    except (KeyError, TypeError, ValueError):
        pass
    if value is None:
        value = avg  # same fallback as the legacy bot
    if value is None:
        return None
    return {"index_id": index_id, "ts_ms": int(ts), "value": value,
            "exchange_ts_ms": int(exchange_ts) if exchange_ts is not None else None,
            "received_at_ms": received_at_ms, "avg_60s": avg}


def parse_market_meta(market: dict, index_id: str, observed_ms: int) -> dict | None:
    """REST market JSON -> market_meta row. `result` is None until the market settles."""
    strike = _f(market.get("floor_strike"))
    if strike is None:
        strike = _f(market.get("cap_strike"))
    close = _iso_ms(market.get("close_time"))
    if market.get("ticker") is None or strike is None or close is None:
        return None
    result = market.get("result")
    return {"ticker": market["ticker"], "index_id": index_id, "strike": strike, "close_ts_ms": close,
            "observed_ms": observed_ms, "status": market.get("status"),
            "result": result if result in ("yes", "no") else None, "expiration_value": _f(market.get("expiration_value"))}


# ------------------------------------------------------------------ external exchanges
def parse_coinbase(message: dict, received_at_ms: int) -> list[dict]:
    rows = []
    for event in message.get("events", []) if message.get("channel") == "ticker" else []:
        for t in event.get("tickers", []):
            index_id = COINBASE_PRODUCTS.get(t.get("product_id"))
            price, bid, ask = _f(t.get("price")), _f(t.get("best_bid")), _f(t.get("best_ask"))
            if not index_id or not price or price <= 0 or bid is None or ask is None or not 0 < bid <= ask:
                continue
            rows.append({"source": "coinbase", "symbol": t["product_id"], "index_id": index_id,
                         "ts_ms": _iso_ms(t.get("time")) or received_at_ms, "received_at_ms": received_at_ms,
                         "price": price, "bid": bid, "ask": ask, "volume_24h": _f(t.get("volume_24_h"))})
    return rows


def parse_kraken(message: dict, received_at_ms: int) -> list[dict]:
    rows = []
    for t in message.get("data", []) if message.get("channel") == "ticker" else []:
        index_id = KRAKEN_PRODUCTS.get(t.get("symbol"))
        price, bid, ask = _f(t.get("last")), _f(t.get("bid")), _f(t.get("ask"))
        if not index_id or not price or price <= 0 or bid is None or ask is None or not 0 < bid <= ask:
            continue
        rows.append({"source": "kraken", "symbol": t["symbol"], "index_id": index_id,
                     "ts_ms": _iso_ms(t.get("timestamp")) or received_at_ms, "received_at_ms": received_at_ms,
                     "price": price, "bid": bid, "ask": ask, "volume_24h": _f(t.get("volume"))})
    return rows


def parse_bitstamp(message: dict, received_at_ms: int) -> list[dict]:
    """Bitstamp `live_trades_*` trade -> external_ticks row (a trade price; no bid/ask)."""
    if message.get("event") != "trade":
        return []
    symbol_index = BITSTAMP_CHANNELS.get(message.get("channel"))
    data = message.get("data") or {}
    price = _f(data.get("price"))
    if not symbol_index or not price or price <= 0:
        return []
    micro = _f(data.get("microtimestamp"))
    ts_ms = int(micro // 1000) if micro else (int(data["timestamp"]) * 1000 if data.get("timestamp") else received_at_ms)
    return [{"source": "bitstamp", "symbol": symbol_index[0], "index_id": symbol_index[1], "ts_ms": ts_ms,
             "received_at_ms": received_at_ms, "price": price, "bid": None, "ask": None, "volume_24h": None}]
