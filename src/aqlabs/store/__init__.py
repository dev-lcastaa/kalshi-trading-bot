"""Event store: immutable, date-partitioned Parquet files queried with DuckDB."""
from .event_store import EventStore
from .schema import TABLES

__all__ = ["EventStore", "TABLES"]
