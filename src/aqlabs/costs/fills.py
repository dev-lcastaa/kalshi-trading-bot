"""Resting (maker) order fill rules, evaluated against the recorded trade and quote stream.

A resting order is not guaranteed to fill, and the fills it does get are biased: they happen when the price
moves against it. The replay captures that bias by itself because fills are decided from the real future
price path. What it cannot know from quote data alone is our queue position, so three rules bound the answer:

- optimistic:  filled by the first trade at our price or through it (as if we were first in the queue)
- queue:       filled once enough volume has traded at or through our price to clear the displayed size ahead
               of us (cancellations ahead of us would help, so this is slightly conservative), or on a trade-through
- pessimistic: filled only by a trade strictly through our price. By price priority such a trade cannot happen
               unless our order was hit first, so this is a true lower bound on the fill probability.

Fill times satisfy optimistic <= queue <= pessimistic. Sides are in YES-price space: a 'yes' order is a resting
YES bid at `limit` (it buys YES at `limit`); a 'no' order is a resting YES ask at `limit` (it buys NO at 1 - limit).
"""
from __future__ import annotations

import numpy as np

from .fees import taker_fee_array

RULES = ("optimistic", "queue", "pessimistic")
_EPS = 1e-9


def maker_fee(price, scale: float = 0.0):
    """Fee on a resting order that fills. Kalshi's series metadata for the 15-minute crypto markets lists a
    plain quadratic (taker-only) fee, so the base case is zero. `scale` is the maker share of the taker
    formula, for stress tests in case that metadata turns out to be wrong."""
    p = np.asarray(price, dtype=float)
    return np.zeros_like(p) if scale == 0 else taker_fee_array(p, scale)


def first_fills(side: str, limit: float, ts_ms, price, volume, bid, ask, bid_size, ask_size,
                start_ms: int, end_ms: int) -> dict[str, int | None]:
    """Timestamp (ms) at which a 1-contract resting order is filled under each rule, or None.

    The order rests from `start_ms` (exclusive) to `end_ms` (inclusive). `ts_ms`, `price` (last trade price),
    `volume` (cumulative traded contracts), `bid`/`ask` and their sizes are the raw per-tick arrays of one market.
    """
    none = {rule: None for rule in RULES}
    ts_ms = np.asarray(ts_ms)
    lo = int(np.searchsorted(ts_ms, start_ms, side="right"))
    hi = int(np.searchsorted(ts_ms, end_ms, side="right"))
    j = lo - 1  # the book as it was when our order arrived
    if j < 0 or lo >= hi:
        return none
    # Cumulative volume can be missing on a tick; carry the last value forward so the next tick that reports it
    # still shows the increase (otherwise a real trade would be silently dropped).
    cumulative = np.asarray(volume, dtype=float)
    seen = np.where(np.isnan(cumulative), 0, np.arange(len(cumulative)))
    cumulative = cumulative[np.maximum.accumulate(seen)]
    traded = np.clip(np.nan_to_num(cumulative[lo:hi] - cumulative[lo - 1:hi - 1]), 0.0, None)
    last = np.asarray(price[lo:hi], dtype=float)
    is_trade = (traded > _EPS) & np.isfinite(last)
    if side == "yes":
        at_or_through = last <= limit + _EPS
        through = last < limit - _EPS
        best, queue = bid[j], np.nan_to_num(bid_size[j])
        ahead = queue if abs(best - limit) < _EPS else (0.0 if best < limit else np.inf)
    elif side == "no":
        at_or_through = last >= limit - _EPS
        through = last > limit + _EPS
        best, queue = ask[j], np.nan_to_num(ask_size[j])
        ahead = queue if abs(best - limit) < _EPS else (0.0 if best > limit else np.inf)
    else:
        raise ValueError("side must be 'yes' or 'no'")
    ts_window = ts_ms[lo:hi]

    def first(mask) -> int | None:
        idx = np.flatnonzero(mask)
        return int(ts_window[idx[0]]) if len(idx) else None

    optimistic = first(is_trade & at_or_through)
    pessimistic = first(is_trade & through)
    cleared = np.cumsum(np.where(is_trade & at_or_through, traded, 0.0)) >= ahead + 1.0 - _EPS
    by_volume = first(cleared) if np.isfinite(ahead) else None
    candidates = [t for t in (by_volume, pessimistic) if t is not None]
    return {"optimistic": optimistic, "queue": min(candidates) if candidates else None, "pessimistic": pessimistic}
