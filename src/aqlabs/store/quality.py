"""Data-quality checks: find the holes before they contaminate a test."""
from __future__ import annotations

import numpy as np

from .event_store import EventStore


def feed_gaps(ts_ms, max_gap_ms: int = 30_000) -> list[tuple[int, int]]:
    """(start_ms, gap_ms) for each interval between consecutive ticks longer than `max_gap_ms`."""
    t = np.sort(np.asarray(ts_ms, dtype=np.int64))
    if len(t) < 2:
        return []
    d = np.diff(t)
    idx = np.flatnonzero(d > max_gap_ms)
    return [(int(t[i]), int(d[i])) for i in idx]


def uptime(ts_ms, max_gap_ms: int = 30_000) -> float:
    """Share of the observed span not inside a gap longer than `max_gap_ms` (1.0 = no holes)."""
    t = np.asarray(ts_ms, dtype=np.int64)
    if len(t) < 2:
        return 0.0
    span = int(t.max() - t.min())
    lost = sum(g for _, g in feed_gaps(t, max_gap_ms))
    return 1.0 - lost / span if span else 0.0


def feed_report(store: EventStore, max_gap_ms: int = 30_000) -> list[dict]:
    """Per-feed tick counts, span, gap count/hours and uptime. Feeds that never ticked are listed with n=0."""
    rows = []
    feeds = [("index_ticks", "index_id", None, f"index/{i}", i) for i in ("BRTI", "SOLUSD_RTI")]
    feeds += [("external_ticks", "index_id", ("source", s), f"{s}/{i}", i)
              for s in ("coinbase", "kraken") for i in ("BRTI", "SOLUSD_RTI")]
    con = store.connect() if any(store.has_data(t) for t in ("index_ticks", "external_ticks")) else None
    for table, key, extra, label, value in feeds:
        ts = np.array([], dtype=np.int64)
        if con is not None and store.has_data(table):
            where = f"{key} = ?" + (f" and {extra[0]} = ?" if extra else "")
            params = [value] + ([extra[1]] if extra else [])
            ts = con.execute(f"select ts_ms from {table} where {where} order by ts_ms", params).fetchnumpy()["ts_ms"]
        gaps = feed_gaps(ts, max_gap_ms)
        rows.append({
            "feed": label, "ticks": int(len(ts)),
            "first_ms": int(ts[0]) if len(ts) else None, "last_ms": int(ts[-1]) if len(ts) else None,
            "gaps": len(gaps), "gap_hours": round(sum(g for _, g in gaps) / 3.6e6, 2),
            "longest_gap_min": round(max((g for _, g in gaps), default=0) / 6e4, 1),
            "uptime": round(uptime(ts, max_gap_ms), 4) if len(ts) else 0.0,
        })
    return rows


def quote_report(store: EventStore) -> dict:
    """Quote sanity: null, crossed and bound (no-liquidity) counts."""
    if not store.has_data("market_ticks"):
        return {}
    row = store.connect().execute(
        "select count(*), sum((yes_bid_dollars is null or yes_ask_dollars is null)::int), "
        "sum((yes_bid_dollars > yes_ask_dollars)::int), "
        "sum((yes_ask_dollars >= 1 or yes_bid_dollars <= 0)::int), count(distinct market_ticker) from market_ticks"
    ).fetchone()
    return dict(zip(("rows", "null_quotes", "crossed", "at_bounds", "markets"), (int(x) for x in row)))
