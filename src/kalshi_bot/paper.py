"""Simulated fills against live Kalshi order books; never places real orders."""
from __future__ import annotations

import time
from decimal import Decimal, ROUND_CEILING
from typing import Any
from uuid import uuid4

from .data.store import Store
from .trading import CENT, ONE, dollars


def taker_fee(count: Decimal, yes_price: Decimal) -> Decimal:
    return (Decimal("0.07") * count * yes_price * (ONE - yes_price)).quantize(CENT, rounding=ROUND_CEILING)


class PaperExchange:
    """The client surface AutoTrader uses, backed by a simulated account."""

    def __init__(self, store: Store, rest: Any):
        self.store = store
        self.rest = rest

    async def get_market(self, ticker: str) -> dict[str, Any]:
        return await self.rest.get_market(ticker)

    async def get_event(self, event_ticker: str) -> dict[str, Any]:
        return await self.rest.get_event(event_ticker)

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        return await self.rest.get_series(series_ticker)

    async def get_market_orderbook(self, ticker: str) -> dict[str, Any]:
        return await self.rest.get_market_orderbook(ticker)

    async def get_positions(self, ticker: str) -> dict[str, Any]:
        account = self.store.trading_record("paper_account") or {}
        quantity = account.get(ticker, "0")
        rows = [{"ticker": ticker, "position_fp": quantity}] if dollars(quantity) != 0 else []
        return {"market_positions": rows, "cursor": ""}

    async def get_orders(self, ticker: str, cursor: str = "") -> dict[str, Any]:
        return {"orders": [], "cursor": ""}

    def _record(self, order_id: str) -> dict:
        record = self.store.trading_record(f"paper_order:{order_id}")
        if record is None:
            raise ValueError("Unknown paper order")
        return record

    async def get_order(self, order_id: str) -> dict[str, Any]:
        return {"order": self._record(order_id)["order"]}

    async def get_fills(self, order_id: str, cursor: str = "") -> dict[str, Any]:
        return {"fills": self._record(order_id)["fills"], "cursor": ""}

    async def cancel_event_order(self, order_id: str, ticker: str) -> dict[str, Any]:
        return {"order_id": order_id, "reduced_by": "0", "ts_ms": int(time.time() * 1000)}

    async def create_event_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Match an IOC order against the live book, in YES-scale prices."""
        ticker = payload["ticker"]
        limit = dollars(payload["price"])
        count = dollars(payload["count"])
        book = (await self.rest.get_market_orderbook(ticker))["orderbook_fp"]
        buying = payload["side"] == "bid"
        if buying:
            levels = sorted((ONE - dollars(price), dollars(size)) for price, size in book.get("no_dollars", []))
        else:
            levels = sorted(((dollars(price), dollars(size)) for price, size in book.get("yes_dollars", [])), reverse=True)
        remaining = count
        fills = []
        order_id = str(uuid4())
        for yes_price, available in levels:
            if remaining <= 0 or (yes_price > limit if buying else yes_price < limit):
                break
            matched = min(available, remaining)
            if matched <= 0 or not Decimal("0") < yes_price < ONE:
                continue
            remaining -= matched
            fills.append({
                "fill_id": str(uuid4()), "order_id": order_id, "ticker": ticker,
                "count_fp": str(matched), "yes_price_dollars": str(yes_price),
                "no_price_dollars": str(ONE - yes_price),
                "fee_cost": str(taker_fee(matched, yes_price)),
            })
        filled = count - remaining
        order = {"order_id": order_id, "client_order_id": payload["client_order_id"], "ticker": ticker,
                 "status": "executed" if remaining == 0 else "canceled", "fill_count_fp": str(filled)}
        self.store.save_trading_record(f"paper_order:{order_id}", "paper_order", {"order": order, "fills": fills})
        if filled:
            account = self.store.trading_record("paper_account") or {}
            delta = filled if buying else -filled
            account[ticker] = str(dollars(account.get(ticker, "0")) + delta)
            self.store.save_trading_record("paper_account", "paper_account", account)
        return {"order_id": order_id, "fill_count": str(filled),
                "remaining_count": str(remaining), "ts_ms": int(time.time() * 1000)}
