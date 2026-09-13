"""Compares model probability vs market-implied probability to flag edges.

Signal-only: this never places, amends, or cancels orders.
"""
from __future__ import annotations

import math
import time

from ..features.engine import Features
from ..kalshi_client.models import Signal
from ..prediction.model import Predictor, market_implied_probability


def kalshi_taker_fee_per_contract(price_dollars: float, multiplier: float = 1.0) -> float:
    """Documented Kalshi taker fee, rounded up to the nearest cent."""
    if not 0 <= price_dollars <= 1 or multiplier < 0:
        raise ValueError("price must be in [0, 1] and multiplier must be non-negative")
    raw_fee = multiplier * 0.07 * price_dollars * (1 - price_dollars)
    return math.ceil(raw_fee * 100) / 100


def generate_signal(
    ticker: str,
    index_id: str,
    features: Features,
    predictor: Predictor,
    yes_bid_dollars: float,
    yes_ask_dollars: float,
    edge_threshold: float,
    fee_multiplier: float = 1.0,
    slippage_per_contract: float = 0.0,
    ts_ms: int | None = None,
) -> Signal:
    if fee_multiplier < 0 or slippage_per_contract < 0:
        raise ValueError("fee_multiplier and slippage_per_contract must be non-negative")
    model_p = predictor.predict(features)
    market_p = market_implied_probability(yes_bid_dollars, yes_ask_dollars)
    edge = model_p - market_p

    # Require the recommendation to agree with the model's own directional call
    # (not just "edge vs market"), so a signal meant to dictate a trade is never
    # BUY_YES while the model itself thinks BELOW is more likely, or vice versa.
    yes_cost = kalshi_taker_fee_per_contract(yes_ask_dollars, fee_multiplier) + slippage_per_contract
    no_cost = kalshi_taker_fee_per_contract(1 - yes_bid_dollars, fee_multiplier) + slippage_per_contract
    yes_purchase_edge = model_p - yes_ask_dollars - yes_cost
    no_purchase_edge = (1 - model_p) - (1 - yes_bid_dollars) - no_cost
    if model_p > 0.5 and yes_purchase_edge > edge_threshold:
        recommendation = "BUY_YES"
    elif model_p < 0.5 and no_purchase_edge > edge_threshold:
        recommendation = "BUY_NO"
    else:
        recommendation = "NO_EDGE"

    return Signal(
        ticker=ticker,
        ts_ms=ts_ms if ts_ms is not None else int(time.time() * 1000),
        index_id=index_id,
        index_price=features.index_price,
        strike=features.strike,
        seconds_to_expiry=features.seconds_to_expiry,
        model_p_yes=model_p,
        market_p_yes=market_p,
        edge=edge,
        recommendation=recommendation,
        confidence=abs(edge),
    )
