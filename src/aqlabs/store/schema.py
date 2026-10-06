"""Event table definitions. Column names match the legacy Postgres tables so imports are 1:1."""
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
         "open_interest": "DOUBLE"},
        "ts_ms", ("market_ticker", "ts_ms"),
    ),
    "index_ticks": Table(
        "index_ticks", {"index_id": "VARCHAR", "ts_ms": "BIGINT", "value": "DOUBLE"}, "ts_ms", ("index_id", "ts_ms"),
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
}
