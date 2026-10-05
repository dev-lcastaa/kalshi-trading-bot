"""Optional entry constraints, separate from the user's strategy rules."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

from .trading import CENT, ONE, dollars, fee_reserve, taker_fee


@dataclass(frozen=True)
class RiskLimits:
    daily_loss_limit: Decimal = Decimal("0")
    max_open_cost: Decimal = Decimal("0")
    max_open_positions: int = 0
    min_edge: Decimal = Decimal("0")
    uncertainty_buffer: Decimal = Decimal("0")
    max_spread: Decimal = Decimal("0")
    max_signal_age_ms: int = 10_000
    max_book_age_ms: int = 2_000
    require_reference_agreement: bool = False
    require_fair_value: bool = False

    def __post_init__(self) -> None:
        for name in ("daily_loss_limit", "max_open_cost", "min_edge", "uncertainty_buffer", "max_spread"):
            value = getattr(self, name)
            if not value.is_finite() or value < 0 or value != value.quantize(CENT):
                raise ValueError(f"{name} must be non-negative dollars with at most two decimals")
        for name in ("min_edge", "uncertainty_buffer", "max_spread"):
            if getattr(self, name) > ONE:
                raise ValueError(f"{name} must not exceed one dollar")
        for name, minimum, maximum in (
            ("max_open_positions", 0, 100), ("max_signal_age_ms", 100, 10_000),
            ("max_book_age_ms", 100, 10_000),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be a whole number between {minimum} and {maximum}")
        for name in ("require_reference_agreement", "require_fair_value"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be true or false")

    @classmethod
    def parse(cls, values: dict | None) -> "RiskLimits":
        if values is None:
            return cls()
        if not isinstance(values, dict) or set(values) - set(cls.__dataclass_fields__):
            raise ValueError("Invalid risk limits")
        parsed = dict(values)
        for name in ("daily_loss_limit", "max_open_cost", "min_edge", "uncertainty_buffer", "max_spread"):
            if name in parsed:
                parsed[name] = dollars(parsed[name])
        return cls(**parsed)

    def to_json(self) -> dict:
        return {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(self).items()}

    def quote_reason(self, confidence: Decimal, ask: Decimal, bid: Decimal | None) -> str | None:
        if self.max_spread > 0 and (bid is None or ask - bid > self.max_spread):
            return "Risk limit: spread is missing or exceeds the maximum"
        if self.min_edge > 0 or self.uncertainty_buffer > 0:
            edge = confidence - self.uncertainty_buffer - ask - taker_fee(ask)
            if edge < self.min_edge:
                return f"Risk limit: buffered after-fee edge {edge:.4f} < {self.min_edge}"
        return None

    def affordable_count(
        self, count: int, ask: Decimal, open_cost: Decimal, realized_pnl: Decimal,
        server_daily_limit: Decimal = Decimal("0"),
    ) -> int:
        limits = [value for value in (self.daily_loss_limit, server_daily_limit) if value > 0]
        available = self.max_open_cost - open_cost if self.max_open_cost > 0 else None
        if limits:
            # Reserve the entire remaining entry cost: existing positions can all lose.
            daily_available = min(limits) - max(-realized_pnl, Decimal("0")) - open_cost
            available = daily_available if available is None else min(available, daily_available)
        if available is None:
            return count
        while count and ask * count + fee_reserve(count) > available:
            count -= 1
        return count
