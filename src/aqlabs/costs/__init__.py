"""Costs: the single source of truth for what a trade costs."""
from .fees import taker_fee, taker_fee_array

__all__ = ["taker_fee", "taker_fee_array"]
