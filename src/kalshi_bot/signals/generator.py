"""Compares model probability vs market-implied probability to flag edges.

Signal-only: this never places, amends, or cancels orders.
"""
from __future__ import annotations

import time

from ..features.engine import Features
from ..kalshi_client.models import Signal
from ..prediction.model import Predictor, market_implied_probability


def generate_signal(
    ticker: str,
    index_id: str,
    features: Features,
    predictor: Predictor,
    yes_bid_dollars: float,
    yes_ask_dollars: float,
    edge_threshold: float,
    ts_ms: int | None = None,
) -> Signal:
    model_p = predictor.predict(features)
    market_p = market_implied_probability(yes_bid_dollars, yes_ask_dollars)
    edge = model_p - market_p

    # Require the recommendation to agree with the model's own directional call
    # (not just "edge vs market"), so a signal meant to dictate a trade is never
    # BUY_YES while the model itself thinks BELOW is more likely, or vice versa.
    if model_p > 0.5 and edge > edge_threshold:
        recommendation = "BUY_YES"
    elif model_p < 0.5 and edge < -edge_threshold:
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
