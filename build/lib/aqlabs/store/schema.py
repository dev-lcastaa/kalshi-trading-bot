"""Event table definitions. Column names match the legacy Postgres tables so imports are 1:1.

Columns added after the legacy export (received_at_ms, exchange_ts_ms, avg_60s) are nullable: legacy
data reads back as NULL for them (`union_by_name`), and legacy CSVs without them still import.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Table:
    name: str
    columns: dict[str, str]  # column -> DuckDB type
    time_column: str  # UTC date partition is derived from this (milliseconds)
    sort_by: tuple[str, ...]
    replace: bool = False  # dimension tables are replaced on import instead of appended


TABLES: dict[str, Table] = {
    "market_ticks": Table(
        "market_ticks",
        {"market_ticker": "VARCHAR", "ts_ms": "BIGINT", "price_dollars": "DOUBLE", "yes_bid_dollars": "DOUBLE",
         "yes_ask_dollars": "DOUBLE", "yes_bid_size": "DOUBLE", "yes_ask_size": "DOUBLE", "volume": "DOUBLE",
         "open_interest": "DOUBLE", "received_at_ms": "BIGINT"},
        "ts_ms", ("market_ticker", "ts_ms"),
    ),
    # ts_ms is Kalshi's `received_at`; exchange_ts_ms is the index's own timestamp; received_at_ms is our clock;
    # avg_60s is Kalshi's running 60-second average (the quantity markets settle on).
    "index_ticks": Table(
        "index_ticks",
        {"index_id": "VARCHAR", "ts_ms": "BIGINT", "value": "DOUBLE", "exchange_ts_ms": "BIGINT",
         "received_at_ms": "BIGINT", "avg_60s": "DOUBLE"},
        "ts_ms", ("index_id", "ts_ms"),
    ),
    "external_ticks": Table(
        "external_ticks",
        {"source": "VARCHAR", "symbol": "VARCHAR", "index_id": "VARCHAR", "ts_ms": "BIGINT",
         "received_at_ms": "BIGINT", "price": "DOUBLE", "bid": "DOUBLE", "ask": "DOUBLE", "volume_24h": "DOUBLE"},
        "ts_ms", ("source", "index_id", "ts_ms"),
    ),
    "markets": Table(
        "markets",
        {"ticker": "VARCHAR", "index_id": "VARCHAR", "strike": "DOUBLE", "close_ts_ms": "BIGINT",
         "first_seen_ts_ms": "BIGINT", "last_seen_ts_ms": "BIGINT", "status": "VARCHAR", "closed_at_ms": "BIGINT",
         "result": "VARCHAR", "outcome_checked_at_ms": "BIGINT"},
        "close_ts_ms", ("close_ts_ms", "ticker"), replace=True,
    ),
    # Full public trade tape (every fill, not only large ones).
    "trades": Table(
        "trades",
        {"trade_id": "VARCHAR", "market_ticker": "VARCHAR", "ts_ms": "BIGINT", "received_at_ms": "BIGINT",
         "yes_price_dollars": "DOUBLE", "no_price_dollars": "DOUBLE", "count": "DOUBLE", "taker_side": "VARCHAR",
         "is_block_trade": "BIGINT"},
        "ts_ms", ("market_ticker", "ts_ms"),
    ),
    # 1 Hz book snapshots derived from the order-book delta stream: top levels as JSON [[price, qty], ...].
    "orderbook_depth": Table(
        "orderbook_depth",
        {"market_ticker": "VARCHAR", "ts_ms": "BIGINT", "seq": "BIGINT", "yes_bids": "VARCHAR", "no_bids": "VARCHAR",
         "yes_total": "DOUBLE", "no_total": "DOUBLE"},
        "ts_ms", ("market_ticker", "ts_ms"),
    ),
    # Append-only market metadata. A row is written on discovery and again when the result is known.
    "market_meta": Table(
        "market_meta",
        {"ticker": "VARCHAR", "index_id": "VARCHAR", "strike": "DOUBLE", "close_ts_ms": "BIGINT",
         "observed_ms": "BIGINT", "status": "VARCHAR", "result": "VARCHAR", "expiration_value": "DOUBLE"},
        "observed_ms", ("observed_ms", "ticker"),
    ),
    # Per-feed liveness, written about once a minute. A feed with no rows or events = 0 was down.
    "heartbeats": Table(
        "heartbeats",
        {"feed": "VARCHAR", "ts_ms": "BIGINT", "events": "BIGINT", "silent_ms": "BIGINT", "note": "VARCHAR"},
        "ts_ms", ("feed", "ts_ms"),
    ),
}
