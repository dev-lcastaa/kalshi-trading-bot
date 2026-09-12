"""Pydantic models for Kalshi REST/WS payloads and our internal signals."""
from __future__ import annotations

from pydantic import BaseModel


class Series(BaseModel):
    ticker: str
    title: str = ""
    category: str = ""
    frequency: str = ""


class Market(BaseModel):
    ticker: str
    event_ticker: str = ""
    series_ticker: str = ""
    title: str = ""
    status: str = ""
    open_time: str | None = None
    close_time: str | None = None
    strike_type: str | None = None
    floor_strike: float | None = None
    cap_strike: float | None = None
    yes_bid_dollars: float | None = None
    yes_ask_dollars: float | None = None


class IndexTick(BaseModel):
    """A CF Benchmarks (or Pyth) underlying price observation."""

    index_id: str
    ts_ms: int
    value: float


class MarketTick(BaseModel):
    """A `ticker` channel update for a single market."""

    market_ticker: str
    ts_ms: int
    price_dollars: float
    yes_bid_dollars: float
    yes_ask_dollars: float
    yes_bid_size: float
    yes_ask_size: float
    volume: float
    open_interest: float


class Signal(BaseModel):
    ticker: str
    ts_ms: int
    index_id: str
    index_price: float
    strike: float
    seconds_to_expiry: float
    model_p_yes: float
    market_p_yes: float
    edge: float
    recommendation: str  # "BUY_YES" | "BUY_NO" | "NO_EDGE"
    confidence: float
