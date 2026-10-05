"""Opt-in trading policy; money is represented as decimal dollars."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal


CENT = Decimal("0.01")
ONE = Decimal("1")
MAX_BUDGET = Decimal("25")


def dollars(value: object) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("Money must be finite")
    return amount


def fee_reserve(count: int) -> Decimal:
    return Decimal("0.02") * count


@dataclass(frozen=True)
class TradingPolicy:
    """Per-trade bet size and optional early exits.

    A take_profit or stop_loss of 0 disables that exit; with both disabled the
    position is held to settlement.
    """

    budget: Decimal = Decimal("1.00")
    take_profit: Decimal = Decimal("0")
    stop_loss: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        for name in ("budget", "take_profit", "stop_loss"):
            amount = getattr(self, name)
            if not amount.is_finite() or amount < 0 or amount != amount.quantize(CENT):
                raise ValueError(f"{name} must be non-negative dollars with at most two decimals")
        if self.budget <= 0:
            raise ValueError("budget must be positive")
        if self.budget > MAX_BUDGET:
            raise ValueError(f"budget must be at most ${MAX_BUDGET}")
        if self.stop_loss >= self.budget:
            raise ValueError("stop_loss must be less than the budget")

    @property
    def has_exits(self) -> bool:
        return self.take_profit > 0 or self.stop_loss > 0

    def entry_count(self, ask: Decimal) -> int:
        """Whole contracts affordable at `ask` with fees reserved, within budget."""
        if not ask.is_finite() or not CENT <= ask < ONE:
            return 0
        count = int(self.budget / ask)
        while count and ask * count + fee_reserve(count) > self.budget:
            count -= 1
        return count

    def exit_reason(self, net_pnl: Decimal) -> str | None:
        if self.take_profit > 0 and net_pnl >= self.take_profit:
            return "take_profit"
        if self.stop_loss > 0 and net_pnl <= -self.stop_loss:
            return "stop_loss"
        return None

    def profit_target_reachable(self, ask: Decimal, count: int) -> bool:
        return (Decimal("0.99") - ask) * count - 2 * fee_reserve(count) >= self.take_profit


def taker_fee(price: Decimal, count: int = 1) -> Decimal:
    """Kalshi quadratic taker fee for an order, rounded up to the cent."""
    raw = Decimal("0.07") * count * price * (ONE - price)
    return (raw / CENT).to_integral_value(rounding=ROUND_CEILING) * CENT


MAX_RULES = 10
RULE_SIDES = ("model", "yes", "no", "momentum")
_COIN_RE = re.compile(r"^(ANY|[A-Z0-9]{2,10})$")


def _prob(values: dict, name: str, default: str) -> Decimal:
    raw = values.get(name)
    amount = dollars(default if raw in (None, "") else raw)
    if not Decimal("0") <= amount <= ONE or amount != amount.quantize(CENT):
        raise ValueError(f"{name} must be between 0 and 1 with at most two decimals")
    return amount


def _seconds(values: dict, name: str, default: int) -> int:
    raw = values.get(name)
    try:
        seconds = int(str(default if raw in (None, "") else raw))
    except ValueError:
        raise ValueError(f"{name} must be whole seconds") from None
    if not 0 <= seconds <= 3600:
        raise ValueError(f"{name} must be between 0 and 3600 seconds")
    return seconds


MAX_ENTRIES = 10


@dataclass(frozen=True)
class ScalpPolicy:
    max_cycles: int = 3
    cycle_cooldown_sec: int = 30
    market_spend_limit: Decimal = Decimal("3.00")
    market_loss_limit: Decimal = Decimal("0.50")

    def __post_init__(self) -> None:
        if not 1 <= self.max_cycles <= 10:
            raise ValueError("max_cycles must be between 1 and 10")
        if not 5 <= self.cycle_cooldown_sec <= 900:
            raise ValueError("cycle_cooldown_sec must be between 5 and 900 seconds")
        for name in ("market_spend_limit", "market_loss_limit"):
            value = getattr(self, name)
            if not value.is_finite() or value <= 0 or value != value.quantize(CENT) or value > MAX_BUDGET:
                raise ValueError(f"{name} must be positive dollars with at most two decimals, up to {MAX_BUDGET}")
        if self.market_loss_limit > self.market_spend_limit:
            raise ValueError("market_loss_limit must not exceed market_spend_limit")

    def to_json(self) -> dict:
        return {
            "max_cycles": self.max_cycles, "cycle_cooldown_sec": self.cycle_cooldown_sec,
            "market_spend_limit": str(self.market_spend_limit), "market_loss_limit": str(self.market_loss_limit),
        }


def _int(values: dict, name: str, default: int) -> int:
    raw = values.get(name)
    if raw in (None, ""):
        return default
    if isinstance(raw, bool):
        raise ValueError(f"{name} must be a whole number")
    try:
        number = Decimal(str(raw))
    except Exception as exc:
        raise ValueError(f"{name} must be a whole number") from exc
    if number != number.to_integral_value():
        raise ValueError(f"{name} must be a whole number")
    return int(number)


@dataclass(frozen=True)
class EntryRule:
    """A user-defined entry rule: when a live market matches, the bot buys.

    The bot does not second-guess a matching rule; every check here is
    something the user chose. Prices are the cost of one contract of the side
    being bought; confidence is the model's probability that side wins (or the
    market midpoint for momentum entries); edge is that probability minus the
    price and the taker fee for settlement-value strategies.
    """

    name: str = "Rule"
    enabled: bool = True
    coin: str = "ANY"
    side: str = "model"
    min_price: Decimal = Decimal("0.50")
    max_price: Decimal = Decimal("0.95")
    min_confidence: Decimal = Decimal("0.50")
    min_edge: Decimal | None = Decimal("0")
    min_seconds_left: int = 330
    max_seconds_left: int = 390
    policy: TradingPolicy = TradingPolicy()
    max_entries: int = 1
    reentry_gap_sec: int = 60
    scalp: ScalpPolicy | None = None

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 40:
            raise ValueError("Rule name must be 1-40 characters")
        if not _COIN_RE.match(self.coin):
            raise ValueError("coin must be ANY or a ticker symbol such as BTC")
        if self.side not in RULE_SIDES:
            raise ValueError("side must be model, yes, no, or momentum")
        if self.side == "momentum" and self.scalp is None:
            raise ValueError("Momentum entries require scalping with protected exits")
        if not CENT <= self.min_price <= self.max_price <= Decimal("0.99"):
            raise ValueError("Price range must satisfy 0.01 <= min_price <= max_price <= 0.99")
        if self.min_edge is not None and not Decimal("-1") <= self.min_edge <= ONE:
            raise ValueError("min_edge must be between -1 and 1")
        if self.min_seconds_left > self.max_seconds_left:
            raise ValueError("min_seconds_left must not exceed max_seconds_left")
        if not 1 <= self.max_entries <= MAX_ENTRIES:
            raise ValueError(f"max_entries must be between 1 and {MAX_ENTRIES}")
        if not 0 <= self.reentry_gap_sec <= 900:
            raise ValueError("reentry_gap_sec must be between 0 and 900 seconds")
        if self.scalp is not None:
            if self.max_entries != 1:
                raise ValueError("Scalping requires max_entries=1 (one buy per cycle, no scale-in)")
            if self.policy.take_profit <= 0 or self.policy.stop_loss <= 0:
                raise ValueError("Scalping requires positive take_profit and stop_loss")
            if self.side != "momentum" and (self.min_edge is None or self.min_edge < CENT):
                raise ValueError("Scalping requires min_edge of at least 0.01 after entry fees")
            if self.side == "momentum" and self.min_edge is not None:
                raise ValueError("Momentum uses observed movement, not settlement-value edge; min_edge must be null")
            if self.min_seconds_left < 60 or self.max_seconds_left > 900:
                raise ValueError("Scalping entry window must be between 60 and 900 seconds left")
            if self.policy.budget > self.scalp.market_spend_limit:
                raise ValueError("Scalping budget must not exceed market_spend_limit")
            if self.policy.stop_loss > self.scalp.market_loss_limit:
                raise ValueError("Scalping stop_loss must not exceed market_loss_limit")

    @staticmethod
    def parse(values: dict) -> "EntryRule":
        if not isinstance(values, dict):
            raise ValueError("Each rule must be an object")
        raw_edge = values.get("min_edge")
        min_edge = None if raw_edge in (None, "") else dollars(raw_edge)
        if min_edge is not None and min_edge != min_edge.quantize(CENT):
            raise ValueError("min_edge must have at most two decimals")
        enabled = values.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be true or false")
        scalping = values.get("scalping", False)
        if not isinstance(scalping, bool):
            raise ValueError("scalping must be true or false")
        scalp = ScalpPolicy(
            max_cycles=_int(values, "max_cycles", 3),
            cycle_cooldown_sec=_int(values, "cycle_cooldown_sec", 30),
            market_spend_limit=dollars(values.get("market_spend_limit", "3.00")),
            market_loss_limit=dollars(values.get("market_loss_limit", "0.50")),
        ) if scalping else None
        return EntryRule(
            name=str(values.get("name") or "Rule").strip()[:40] or "Rule",
            enabled=enabled,
            coin=str(values.get("coin") or "ANY").strip().upper(),
            side=str(values.get("side") or "model").strip().lower(),
            min_price=_prob(values, "min_price", "0.50"),
            max_price=_prob(values, "max_price", "0.95"),
            min_confidence=_prob(values, "min_confidence", "0"),
            min_edge=min_edge,
            min_seconds_left=_seconds(values, "min_seconds_left", 0),
            max_seconds_left=_seconds(values, "max_seconds_left", 900),
            policy=TradingPolicy(
                budget=dollars(values.get("budget") or "1.00"),
                take_profit=dollars(values.get("take_profit") or "0"),
                stop_loss=dollars(values.get("stop_loss") or "0"),
            ),
            max_entries=_int(values, "max_entries", 1),
            reentry_gap_sec=_int(values, "reentry_gap_sec", 60),
            scalp=scalp,
        )

    def to_json(self) -> dict:
        result = {
            "name": self.name, "enabled": self.enabled, "coin": self.coin, "side": self.side,
            "min_price": str(self.min_price), "max_price": str(self.max_price),
            "min_confidence": str(self.min_confidence),
            "min_edge": None if self.min_edge is None else str(self.min_edge),
            "min_seconds_left": self.min_seconds_left, "max_seconds_left": self.max_seconds_left,
            "budget": str(self.policy.budget), "take_profit": str(self.policy.take_profit),
            "stop_loss": str(self.policy.stop_loss),
            "max_entries": self.max_entries, "reentry_gap_sec": self.reentry_gap_sec,
        }
        if self.scalp is not None:
            result.update(scalping=True, **self.scalp.to_json())
        return result

    def coin_matches(self, ticker: str) -> bool:
        return self.coin == "ANY" or self.coin in ticker.split("-")[0].upper()

    def pick_side(self, model_p_yes: Decimal, yes_ask: Decimal | None = None, no_ask: Decimal | None = None) -> str:
        """For side="model", buy the side whose after-fee edge is larger; without
        quotes fall back to the side the model favours."""
        if self.side == "momentum":
            raise ValueError("Momentum side requires fresh quote movement")
        if self.side != "model":
            return self.side
        if yes_ask is not None and no_ask is not None:
            yes_edge = model_p_yes - yes_ask - taker_fee(yes_ask)
            no_edge = (ONE - model_p_yes) - no_ask - taker_fee(no_ask)
            return "yes" if yes_edge >= no_edge else "no"
        return "yes" if model_p_yes >= Decimal("0.5") else "no"

    def check(self, ticker: str, seconds_left: float, model_p_yes: Decimal, side: str, ask: Decimal) -> str | None:
        """None when the market satisfies this rule at `ask`, else the first failing condition."""
        if not self.coin_matches(ticker):
            return f"coin is not {self.coin}"
        if not self.min_seconds_left <= seconds_left <= self.max_seconds_left:
            return f"{int(seconds_left)}s left is outside {self.min_seconds_left}-{self.max_seconds_left}s"
        if not self.min_price <= ask <= self.max_price:
            return f"{side.upper()} costs {ask}, outside {self.min_price}-{self.max_price}"
        confidence = model_p_yes if side == "yes" else ONE - model_p_yes
        if confidence < self.min_confidence:
            if self.side == "momentum":
                return f"market implies {side.upper()} {confidence:.3f} < {self.min_confidence}"
            return f"model gives {side.upper()} {confidence:.2f} < {self.min_confidence}"
        if self.min_edge is not None:
            edge = confidence - ask - taker_fee(ask)
            if edge < self.min_edge:
                return f"edge {edge:.3f} after fees < {self.min_edge}"
        return None


def default_rules(budget: Decimal = Decimal("1.00"), policy: TradingPolicy | None = None) -> list[EntryRule]:
    """Mirrors the coin-price fair-value backtest: buy whichever side is at least
    3c cheap after fees with 4-14 minutes left, add up to 5 times (60s apart)
    while the edge lasts, and hold to settlement."""
    return [EntryRule(
        name="Coin price edge", min_price=Decimal("0.03"), max_price=Decimal("0.97"),
        min_confidence=Decimal("0"), min_edge=Decimal("0.03"),
        min_seconds_left=240, max_seconds_left=840, max_entries=5, reentry_gap_sec=60,
        policy=policy or TradingPolicy(budget=min(budget, MAX_BUDGET)),
    )]


def parse_rules(values: dict) -> list[EntryRule]:
    """Parse a settings record; legacy budget/take_profit/stop_loss records migrate to one rule."""
    if not isinstance(values, dict):
        raise ValueError("Settings must be an object")
    if "rules" not in values:
        if "budget" not in values:
            raise ValueError("Settings must contain rules")
        return default_rules(dollars(values["budget"]))
    rules = values["rules"]
    if not isinstance(rules, list) or not rules:
        raise ValueError("Add at least one rule")
    if len(rules) > MAX_RULES:
        raise ValueError(f"At most {MAX_RULES} rules are supported")
    return [EntryRule.parse(rule) for rule in rules]


def rules_json(rules: list[EntryRule]) -> dict:
    return {"rules": [rule.to_json() for rule in rules]}