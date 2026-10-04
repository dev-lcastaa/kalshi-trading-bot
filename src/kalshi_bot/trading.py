"""Opt-in trading policy; money is represented as decimal dollars."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


CENT = Decimal("0.01")
ONE = Decimal("1")


def dollars(value: object) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("Money must be finite")
    return amount


def fee_reserve(count: int) -> Decimal:
    return Decimal("0.02") * count


@dataclass(frozen=True)
class TradingPolicy:
    budget: Decimal = Decimal("1.00")
    take_profit: Decimal = Decimal("0.50")
    stop_loss: Decimal = Decimal("0.10")

    def __post_init__(self) -> None:
        for name in ("budget", "take_profit", "stop_loss"):
            amount = getattr(self, name)
            if not amount.is_finite() or amount <= 0 or amount != amount.quantize(CENT):
                raise ValueError(f"{name} must be positive dollars with at most two decimals")
        if self.budget >= Decimal("2"):
            raise ValueError("The small-money trader requires a budget below $2")
        if self.stop_loss >= self.budget:
            raise ValueError("stop_loss must be less than the budget")

    def entry_count(self, ask: Decimal) -> int:
        if not ask.is_finite() or not CENT <= ask < ONE:
            return 0
        count = int(self.budget / ask)
        while count and ask * count + fee_reserve(count) > self.budget:
            count -= 1
        if not count or (ONE - ask) * count - 2 * fee_reserve(count) < self.take_profit:
            return 0
        return count

    def exit_reason(self, net_pnl: Decimal) -> str | None:
        if net_pnl >= self.take_profit:
            return "take_profit"
        if net_pnl <= -self.stop_loss:
            return "stop_loss"
        return None