"""Local order book rebuilt from Kalshi's snapshot + delta stream.

Storing every delta (about 1,000 per second across two markets) is not worth the disk, so the
collector keeps the book in memory and writes a 1 Hz depth snapshot per market. Any sequence gap
invalidates the books of that connection until a fresh snapshot arrives.
"""
from __future__ import annotations

import json

_EPS = 1e-9


def _level_list(raw) -> dict[float, float]:
    out: dict[float, float] = {}
    for level in raw or []:
        try:
            price, qty = float(level[0]), float(level[1])
        except (TypeError, ValueError, IndexError):
            continue
        if qty > _EPS:
            out[price] = qty
    return out


class BookTracker:
    def __init__(self) -> None:
        # ticker -> {"yes": {price: qty}, "no": {price: qty}, "seq": int | None}; yes/no are resting bids per side.
        self.books: dict[str, dict] = {}

    def snapshot(self, msg: dict, seq: int | None) -> None:
        ticker = msg.get("market_ticker")
        if ticker is None:
            return
        self.books[ticker] = {"yes": _level_list(msg.get("yes_dollars_fp")), "no": _level_list(msg.get("no_dollars_fp")),
                              "seq": seq}

    def delta(self, msg: dict, seq: int | None) -> bool:
        """Apply a delta. Returns False (and ignores it) if there is no snapshot to apply it to."""
        book = self.books.get(msg.get("market_ticker"))
        side = msg.get("side")
        if book is None or side not in ("yes", "no"):
            return False
        try:
            price, change = float(msg["price_dollars"]), float(msg["delta_fp"])
        except (KeyError, TypeError, ValueError):
            return False
        qty = book[side].get(price, 0.0) + change
        if qty > _EPS:
            book[side][price] = qty
        else:
            book[side].pop(price, None)
        book["seq"] = seq
        return True

    def invalidate(self) -> None:
        self.books.clear()

    def forget_except(self, tickers) -> None:
        keep = set(tickers)
        for t in [t for t in self.books if t not in keep]:
            del self.books[t]

    def depth_row(self, ticker: str, ts_ms: int, levels: int = 10) -> dict | None:
        book = self.books.get(ticker)
        if book is None:
            return None

        def top(side: dict[float, float]) -> str:
            best = sorted(side.items(), key=lambda kv: -kv[0])[:levels]  # best (highest) bid first
            return json.dumps([[round(p, 4), round(q, 2)] for p, q in best], separators=(",", ":"))

        return {"market_ticker": ticker, "ts_ms": ts_ms, "seq": book["seq"], "yes_bids": top(book["yes"]),
                "no_bids": top(book["no"]), "yes_total": round(sum(book["yes"].values()), 2),
                "no_total": round(sum(book["no"].values()), 2)}
