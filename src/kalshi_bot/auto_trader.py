"""Persistent, opt-in event-market trading and fill reconciliation."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from decimal import Decimal, ROUND_CEILING
from typing import Any
from uuid import uuid4

import httpx

from .data.store import Store
from .notifier import DiscordNotifier, result_embed, trade_embed
from .risk import RiskLimits
from .trading import (
    CENT, ONE, STOP_HEADROOM, EntryRule, ScalpPolicy, TradingPolicy, default_rules, dollars, fee_reserve, parse_rules,
    round_trip_cost, rules_json, taker_fee,
)

MarketFeed = Callable[[], list[dict]]
ExecutionFeed = Callable[[str, Decimal, Decimal, Decimal, Decimal, int], dict | None]
logger = logging.getLogger(__name__)
# After a transient entry failure (price moved, thin book) wait before re-checking the market.
_RETRY_AFTER_MS = 6_000
# Target period of the trading loop. Open positions are checked faster so a stop or target is not left waiting.
_IDLE_CYCLE_SEC = 2.0
_POSITION_CYCLE_SEC = 1.0
_MIN_SLEEP_SEC = 0.1


def series_of(ticker: str) -> str:
    return ticker.split("-")[0].upper()


def policy_json(policy: TradingPolicy) -> dict[str, str]:
    return {name: str(value) for name, value in asdict(policy).items()}


def pnl_tracking(position: dict, ts_ms: int, *, from_entry: bool = False) -> dict:
    return position.setdefault("pnl_tracking", {
        "started_ms": ts_ms, "from_entry": from_entry,
        "samples": 0, "unavailable_samples": 0, "low": None, "high": None,
    })


def observe_pnl(position: dict, net: Decimal, ts_ms: int, source: str) -> None:
    tracking = pnl_tracking(position, ts_ms)
    tracking["samples"] += 1
    point = {"net_pnl": str(net), "ts_ms": ts_ms, "source": source}
    for key, better in (("low", lambda previous: net < previous),
                        ("high", lambda previous: net > previous)):
        if tracking[key] is None or better(dollars(tracking[key]["net_pnl"])):
            tracking[key] = dict(point)


def entries_of(position: dict) -> int:
    """Filled buys in a position; records from before scale-in support count as one."""
    if "entries" in position:
        return int(position["entries"])
    return 1 if dollars(position.get("quantity", "0")) > 0 or dollars(position.get("entry_cost", "0")) > 0 else 0


def can_add_to(position: dict, rule: EntryRule, now_ms: int) -> str | None:
    """None when another buy may be added to `position` under `rule`, else why not."""
    if position.get("status") != "open" or position.get("pending"):
        return f"Bot position {position.get('status')}"
    if position.get("scalp"):
        return "Scalping cycle open - waiting for a full exit before another buy"
    if position.get("rule") != rule.name:
        return "Bot position open (another rule)"
    if dollars(position.get("exit_credit", "0")) != 0 or position.get("exit_trigger"):
        return "Bot position open (already selling)"
    entries = entries_of(position)
    if entries >= rule.max_entries:
        return f"Bot position open ({entries}/{rule.max_entries} buys)"
    wait = int(position.get("last_entry_ms") or position.get("opened_ms") or 0) + rule.reentry_gap_sec * 1000 - now_ms
    if wait > 0:
        return f"Bot position open ({entries}/{rule.max_entries} buys, next buy allowed in {wait // 1000 + 1}s)"
    return None


def market_totals(history: list[dict]) -> tuple[Decimal, Decimal, Decimal]:
    spent = sum((dollars(p.get("entry_cost", "0")) for p in history), Decimal("0"))
    realized = [dollars(p["net_pnl"]) for p in history
                if p.get("status") == "closed" and p.get("net_pnl") is not None]
    return spent, sum((max(-pnl, Decimal("0")) for pnl in realized), Decimal("0")), sum(realized, Decimal("0"))


def scalp_limits(rule: EntryRule, history: list[dict]) -> ScalpPolicy:
    """A settings edit cannot loosen caps already committed for this market."""
    if rule.scalp is None:
        raise ValueError("Scalping is not enabled for this rule")
    policies = [rule.scalp, *[
        ScalpPolicy(**{**p["scalp"], "market_spend_limit": dollars(p["scalp"]["market_spend_limit"]),
                       "market_loss_limit": dollars(p["scalp"]["market_loss_limit"])})
        for p in history if p.get("scalp")
    ]]
    return ScalpPolicy(
        max_cycles=min(p.max_cycles for p in policies),
        cycle_cooldown_sec=max(p.cycle_cooldown_sec for p in policies),
        market_spend_limit=min(p.market_spend_limit for p in policies),
        market_loss_limit=min(p.market_loss_limit for p in policies),
    )


def can_start_cycle(history: list[dict], rule: EntryRule, now_ms: int) -> str | None:
    if not history:
        return None
    last = history[-1]
    if not last.get("scalp"):
        return f"Bot position {last['status']}"
    if rule.scalp is None or last.get("rule") != rule.name:
        return "Scalping stopped - only the original enabled scalping rule can re-enter"
    if (last["status"] != "closed" or last.get("pending") or dollars(last.get("quantity", "0")) != 0
            or last.get("closed_by") != "take_profit" or dollars(last.get("net_pnl") or "0") <= 0):
        return "Scalping stopped - re-entry requires a fully closed profitable take-profit exit"
    limits = scalp_limits(rule, history)
    spent, losses, _ = market_totals(history)
    if max(int(p.get("cycle_number", 1)) for p in history) >= limits.max_cycles:
        return f"Scalping stopped - {limits.max_cycles}/{limits.max_cycles} cycles used"
    if losses >= limits.market_loss_limit:
        return f"Scalping stopped - market loss limit ${limits.market_loss_limit} reached"
    if spent >= limits.market_spend_limit:
        return f"Scalping stopped - market spending limit ${limits.market_spend_limit} reached"
    wait = int(last.get("closed_ms") or 0) + limits.cycle_cooldown_sec * 1000 - now_ms
    if wait > 0:
        return f"Scalping cooldown - next cycle allowed in {(wait + 999) // 1000}s"
    return None


def parse_policy(values: dict) -> TradingPolicy:
    return TradingPolicy(**{name: dollars(values[name]) for name in ("budget", "take_profit", "stop_loss")})


# Same safety checks as the model's locked pick (3 momentum/book signals + the LLM review).
MIN_LOCKED_CHECKS = 2


def locked_call_side(decision: dict) -> str:
    return "yes" if float(decision["model_p_yes"]) >= 0.5 else "no"


def locked_gate_reason(decision: dict | None, side: str) -> str | None:
    """None when `side` may be traded under the model's locked call, else why not."""
    if decision is None:
        return "Waiting for the model to lock its direction"
    agree, total = int(decision.get("confirmation_agree") or 0), int(decision.get("confirmation_total") or 0)
    if agree < MIN_LOCKED_CHECKS:
        return f"Model safety checks {agree}/{total} - need at least {MIN_LOCKED_CHECKS}"
    call = locked_call_side(decision)
    if side != call:
        return f"Entry side {side.upper()} conflicts with the model's locked {call.upper()} call"
    return None


def locked_gate_snapshot(decision: dict) -> dict:
    return {
        "market_id": decision["ticker"], "locked_ts_ms": decision["ts_ms"],
        "call": locked_call_side(decision), "model_p_yes": str(decision["model_p_yes"]),
        "recommendation": decision["recommendation"],
        "checks_agree": decision.get("confirmation_agree"), "checks_total": decision.get("confirmation_total"),
        "checks": decision.get("confirmation_detail"),
    }


def order_payload(ticker: str, side: str, action: str, quantity: Decimal, price: Decimal, client_id: str) -> dict:
    if side not in ("yes", "no") or action not in ("buy", "sell"):
        raise ValueError("Invalid order direction")
    if quantity <= 0 or not Decimal("0") < price < ONE:
        raise ValueError("Invalid order quantity or price")
    return {
        "ticker": ticker, "client_order_id": client_id,
        "side": "bid" if (side == "yes") == (action == "buy") else "ask",
        "count": str(quantity), "price": str(price if side == "yes" else ONE - price),
        "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross",
        "reduce_only": action == "sell", "cancel_order_on_pause": True,
    }


class AutoTrader:
    def __init__(
        self, store: Store, rest: Any, defaults: TradingPolicy | None = None,
        environment: str = "demo", execution_allowed: bool = False, mode: str = "live",
        account_identity: str = "", worker_id: str | None = None, market_feed: MarketFeed | None = None,
        daily_loss_limit: Decimal = Decimal("0"),
        execution_feed: ExecutionFeed | None = None,
        notifier: DiscordNotifier | None = None,
    ):
        if mode not in ("live", "paper"):
            raise ValueError("mode must be 'live' or 'paper'")
        self.store = store
        self.rest = rest
        self.environment = environment
        self.execution_allowed = execution_allowed
        self.mode = mode
        self.account_identity = account_identity
        self.worker_id = worker_id or str(uuid4())
        self.market_feed = market_feed
        self.daily_loss_limit = daily_loss_limit
        self.execution_feed = execution_feed
        self.notifier = notifier
        self.worker_claimed = False
        self.control_revision = 0
        self.enabled = False
        self.enabled_since_ms = 0
        self.last_cycle_ms: int | None = None
        self.last_cycle_duration_ms: int | None = None
        self.error: str | None = None
        self.lock = asyncio.Lock()
        self.watch: list[dict] = []
        self.retry_after_ms: dict[str, int] = {}
        self.last_skip_reason: dict[str, str] = {}
        self.eligible_markets: set[str] = set()
        self.quote_history: dict[str, list[tuple[int, Decimal, Decimal]]] = {}
        self.settings_key = f"settings:{mode}"
        self.position_kind = "position:paper" if mode == "paper" else f"position:{environment}"
        if store.trading_record(self.settings_key) is None:
            inherited = store.trading_record("settings") if mode == "live" else None
            first_run = (default_rules(dollars(inherited["budget"])) if inherited and "budget" in inherited
                         else default_rules(policy=defaults or TradingPolicy()))
            store.save_trading_record(self.settings_key, "settings", rules_json(first_run))
        else:
            # Pre-rules records (budget/take_profit/stop_loss) migrate to one default rule.
            stored = store.trading_record(self.settings_key)
            if "rules" not in stored:
                store.save_trading_record(self.settings_key, "settings", rules_json(parse_rules(stored)))
        if rest is not None:
            store.record_trading_event("started_paused", "Bot restarted with new entries paused",
                                       environment=environment, mode=mode)

    def rules(self) -> list[EntryRule]:
        return parse_rules(self.store.trading_record(self.settings_key))

    def risk_limits(self) -> RiskLimits:
        return RiskLimits.parse((self.store.trading_record(self.settings_key) or {}).get("risk"))

    def settings(self) -> dict:
        settings = rules_json(self.rules())
        risk = self.risk_limits()
        if risk != RiskLimits():
            settings["risk"] = risk.to_json()
        return settings

    def open_cost(self) -> Decimal:
        return sum((max(dollars(p["entry_cost"]) - dollars(p.get("exit_credit", "0")), Decimal("0"))
                    for p in self.positions() if p["status"] in ("pending", "open")), Decimal("0"))

    def risk_blockers(self) -> list[str]:
        risk = self.risk_limits()
        blockers = []
        limits = [value for value in (risk.daily_loss_limit, self.daily_loss_limit) if value > 0]
        if limits and max(-self.today_pnl(), Decimal("0")) + self.open_cost() >= min(limits):
            blockers.append("Daily loss budget exhausted or reserved by open positions")
        if risk.max_open_cost > 0 and self.open_cost() >= risk.max_open_cost:
            blockers.append("Maximum open-position cost reached")
        if risk.max_open_positions and sum(p["status"] in ("pending", "open") for p in self.positions()) >= risk.max_open_positions:
            blockers.append("Maximum simultaneous positions reached")
        return blockers

    def entry_risk_reason(self, confidence: Decimal, ask: Decimal, bid: Decimal) -> str | None:
        risk = self.risk_limits()
        reason = risk.quote_reason(confidence, ask, bid)
        if reason is None and risk.affordable_count(1, ask, self.open_cost(), self.today_pnl(), self.daily_loss_limit) == 0:
            reason = "Risk limit: remaining exposure or daily loss budget buys no whole contract"
        return reason

    def positions(self) -> list[dict]:
        return sorted(self.store.trading_records(self.position_kind),
                      key=lambda p: (int(p.get("opened_ms") or 0), int(p.get("cycle_number") or 1)))

    def market_history(self, ticker: str) -> list[dict]:
        return [p for p in self.positions() if p["ticker"] == ticker]

    def today_pnl(self) -> Decimal:
        """Net result of bets closed since midnight UTC."""
        midnight_ms = int(time.time() // 86_400 * 86_400 * 1000)
        return sum((dollars(p["net_pnl"]) for p in self.positions()
                    if p.get("status") == "closed" and p.get("net_pnl") is not None
                    and int(p.get("closed_ms") or 0) >= midnight_ms), Decimal("0"))

    def blockers(self) -> list[str]:
        blockers = []
        blockers.extend(self.risk_blockers())
        if self.daily_loss_limit > 0 and self.today_pnl() <= -self.daily_loss_limit:
            blockers.append(f"Daily loss limit of ${self.daily_loss_limit} reached; new bets resume tomorrow (UTC)")
        if not self.execution_allowed or self.rest is None:
            blockers.append("Order execution is not authorized on this server")
        if not self.account_identity:
            blockers.append("Trading credentials are not identified")
        if self.execution_allowed and not self.worker_claimed:
            blockers.append("Another trading worker owns this database, or this worker has not started")
        if self.last_cycle_ms is None or int(time.time() * 1000) - self.last_cycle_ms > 30_000:
            blockers.append("Trading worker is not healthy")
        if self.error:
            blockers.append(self.error)
        if any(position.get("pending") for position in self.positions()):
            blockers.append("An order is awaiting reconciliation; new entries are blocked")
        return blockers

    def snapshot(self) -> dict:
        events = [event for event in self.store.trading_events(limit=200)
                  if event.get("mode", "live") == self.mode][:100]
        positions = self.positions()
        scored = [p for p in positions if p["status"] == "closed" and p.get("net_pnl") is not None
                  and dollars(p.get("entry_cost", "0")) > 0]
        running = [p for p in positions if p["status"] in ("pending", "open")]
        closed = [p for p in positions if p["status"] == "closed"]
        execution = [entry for p in positions for entry in p.get("execution_history", [])]
        bought = [entry for entry in execution if entry["action"] == "buy" and dollars(entry["filled"]) > 0]
        bought_quantity = sum((dollars(entry["filled"]) for entry in bought), Decimal("0"))
        measured_settlements = []
        for position in scored:
            history = position.get("execution_history", [])
            buys = [entry for entry in history if entry["action"] == "buy" and dollars(entry["filled"]) > 0]
            if (position.get("closed_by") != "settled" or not buys
                    or position.get("strategy") == "momentum"
                    or any(entry["action"] == "sell" for entry in history)
                    or any(not entry.get("quote") or entry["quote"].get("confidence") is None for entry in buys)):
                continue
            if sum((dollars(entry["gross"]) + dollars(entry["fees"]) for entry in buys), Decimal("0")) != dollars(position["entry_cost"]):
                continue
            expected = sum((dollars(entry["quote"]["confidence"]) * dollars(entry["filled"])
                            - dollars(entry["gross"]) - dollars(entry["fees"]) for entry in buys), Decimal("0"))
            measured_settlements.append((expected, dollars(position["net_pnl"])))
        return {
            "mode": self.mode,
            "settings": self.settings(), "enabled": self.enabled,
            "environment": self.environment,
            "blockers": self.blockers(), "last_cycle_ms": self.last_cycle_ms,
            "last_cycle_duration_ms": self.last_cycle_duration_ms, "error": self.error,
            "positions": [*closed[-100:], *running], "events": events,
            "summary": {
                "running": len(running), "finished": len(scored),
                "wins": sum(dollars(p["net_pnl"]) > 0 for p in scored),
                "net_pnl": str(sum((dollars(p["net_pnl"]) for p in scored), Decimal("0"))),
            },
            "risk_state": {"open_cost": str(self.open_cost()), "today_pnl": str(self.today_pnl())},
            "execution": {
                "orders": len(execution),
                "filled_orders": sum(dollars(entry["filled"]) > 0 for entry in execution),
                "entry_fees": str(sum((dollars(entry["fees"]) for entry in bought), Decimal("0"))),
                "entry_cost_above_mid": str(sum((dollars(entry["cost_above_mid"]) for entry in bought
                                               if entry.get("cost_above_mid") is not None), Decimal("0"))),
                "bought_quantity": str(bought_quantity),
                "settlement_comparison": {
                    "measured_bets": len(measured_settlements),
                    "predicted_net": str(sum((expected for expected, _ in measured_settlements), Decimal("0"))),
                    "realized_net": str(sum((actual for _, actual in measured_settlements), Decimal("0"))),
                },
            },
            "decisions": self.store.trading_decisions(),
            "watch": self.watch,
        }

    async def save_settings(self, values: dict) -> dict:
        rules = parse_rules(values)
        risk = RiskLimits.parse(values.get("risk")) if values.get("risk") is not None else self.risk_limits()
        settings = rules_json(rules)
        if risk != RiskLimits():
            settings["risk"] = risk.to_json()
        async with self.lock:
            if self.enabled:
                raise ValueError("Pause trading before changing settings")
            self.store.save_trading_record(self.settings_key, "settings", settings)
            self.store.record_trading_event("settings_saved", f"{len(rules)} trading rule(s) saved",
                                            settings=settings, mode=self.mode)
            return self.snapshot()

    async def control(self, enabled: bool, confirm: bool, expected_settings: dict | None = None) -> dict:
        self.control_revision += 1
        revision = self.control_revision
        if not enabled:
            self.enabled = False
            self.store.record_trading_event("paused", "New entries paused; existing positions remain monitored",
                                            mode=self.mode)
            return self.snapshot()
        async with self.lock:
            if revision != self.control_revision:
                raise ValueError("Enable request superseded by a newer trading control request")
            if not confirm:
                raise ValueError("Confirm the saved settings and trading risk before enabling")
            if expected_settings is not None and parse_rules(expected_settings) != self.rules():
                raise ValueError("Saved settings changed; review and confirm again")
            if expected_settings is not None and RiskLimits.parse(expected_settings.get("risk")) != self.risk_limits():
                raise ValueError("Saved risk limits changed; review and confirm again")
            if not any(rule.enabled for rule in self.rules()):
                raise ValueError("Enable at least one rule before starting auto trading")
            blockers = self.blockers()
            if blockers:
                raise ValueError("; ".join(blockers))
            self.enabled_since_ms = int(time.time() * 1000)
            self.enabled = True
            self.store.record_trading_event(
                "enabled", "Auto trading enabled; entering every market that matches a rule",
                environment=self.environment, settings=self.settings(), mode=self.mode,
            )
            return self.snapshot()

    def save_position(self, position: dict) -> None:
        now_ms = int(time.time() * 1000)
        position.setdefault("opened_ms", now_ms)
        position.setdefault("market_id", position["ticker"])
        if position.get("status") in ("closed", "skipped"):
            position.setdefault("closed_ms", now_ms)
        suffix = f":{position['position_id']}" if position.get("position_id") else ""
        self.store.save_trading_record(f"{self.position_kind}:{position['ticker']}{suffix}", self.position_kind, position)

    def event(self, action: str, reason: str, position: dict) -> None:
        self.store.record_trading_event(action, reason, ticker=position["ticker"],
                                        market_id=position["ticker"],
                                        environment=self.environment, mode=self.mode,
                                        position_id=position.get("position_id"), cycle_number=position.get("cycle_number"))

    def notify(self, send: Callable[[DiscordNotifier], None]) -> None:
        """A notification problem must never pause trading."""
        if self.notifier is None:
            return
        try:
            send(self.notifier)
        except Exception:
            logger.exception("Could not send Discord notification")

    @property
    def notify_label(self) -> str:
        return "PAPER" if self.mode == "paper" else self.environment

    def notify_closed(self, position: dict) -> None:
        self.notify(lambda n: n.settlement(result_embed(position, self.notify_label, self.today_pnl())))

    async def service_positions(self) -> None:
        """Reconcile and monitor every held position, different markets in parallel.

        Positions in one market stay sequential (they share one account holding); a failure in one
        market never delays another market's exit, and the first failure is raised afterwards.
        """
        by_ticker: dict[str, list[dict]] = {}
        for position in self.positions():
            if position["status"] in ("pending", "open") and position.get("account_identity") != self.account_identity:
                raise ValueError("Trading credentials changed; position ownership cannot be verified")
            by_ticker.setdefault(position["ticker"], []).append(position)

        async def service(group: list[dict]) -> None:
            for position in group:
                if position.get("pending"):
                    await self.reconcile(position)
                if position["status"] == "open" and not position.get("pending"):
                    await self.monitor(position)

        results = await asyncio.gather(*(service(group) for group in by_ticker.values()), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    def has_running_position(self) -> bool:
        return any(p["status"] in ("pending", "open") for p in self.positions())

    async def cycle(self) -> None:
        started = time.monotonic()
        async with self.lock:
            try:
                candidates = self.evaluate_rules()
                if self.execution_allowed and self.rest is not None:
                    if not self.account_identity:
                        raise ValueError("Trading credentials are not identified")
                    self.worker_claimed = self.store.claim_trading_worker(self.worker_id)
                    if not self.worker_claimed:
                        raise ValueError("Another trading worker owns this database")
                    await self.service_positions()
                    if any(rule.scalp for rule in self.rules()):
                        candidates = self.evaluate_rules()
                    if self.enabled and not self.blockers():
                        await self.enter_by_rules(candidates)
                self.error = None
            except Exception as exc:
                self.enabled = False
                self.control_revision += 1
                message = (str(exc) if isinstance(exc, ValueError) else
                           "Trading API or reconciliation failed; entries paused. Check Kalshi before intervening.")
                if self.error != message:
                    self.store.record_trading_event("error", message, environment=self.environment, mode=self.mode)
                self.error = message
            finally:
                self.last_cycle_ms = int(time.time() * 1000)
                self.last_cycle_duration_ms = int((time.monotonic() - started) * 1000)

    async def run(self, on_update: Callable[[], Awaitable[None]] | None = None) -> None:
        try:
            while True:
                started = time.monotonic()
                await self.cycle()
                if on_update is not None:
                    try:
                        await on_update()
                    except Exception:
                        logger.exception("Could not publish trading dashboard update")
                period = _POSITION_CYCLE_SEC if self.has_running_position() else _IDLE_CYCLE_SEC
                await asyncio.sleep(max(_MIN_SLEEP_SEC, period - (time.monotonic() - started)))
        finally:
            self.enabled = False
            self.store.release_trading_worker(self.worker_id)
            self.worker_claimed = False

    async def levels(self, ticker: str, side: str) -> list[tuple[Decimal, Decimal]]:
        data = await self.rest.get_market_orderbook(ticker)
        return self.book_levels(data["orderbook_fp"], side)

    @staticmethod
    def book_levels(book: dict, side: str) -> list[tuple[Decimal, Decimal]]:
        levels = [(dollars(price), dollars(count)) for price, count in book[f"{side}_dollars"]]
        if any(not Decimal("0") < price < ONE or count < 0 for price, count in levels):
            raise ValueError("Invalid order book")
        return sorted((level for level in levels if level[1] > 0), reverse=True)

    def reference_reason(self, market: dict, now_ms: int) -> str | None:
        if self.risk_limits().require_fair_value and market.get("model") != "fair-value":
            return "Risk limit: fair-value model unavailable; fallback entries are disabled"
        if not self.risk_limits().require_reference_agreement:
            return None
        if market.get("index_price") is None or market.get("strike") is None:
            return "Reference guard: settlement index or strike is missing"
        index_price, strike = dollars(market["index_price"]), dollars(market["strike"])
        if index_price <= 0 or strike <= 0 or index_price == strike:
            return "Reference guard: index direction is undefined"
        latest: dict[str, dict] = {}
        for tick in self.store.recent_external_ticks(now_ms - 5_000):
            if tick["index_id"] != market.get("index_id"):
                continue
            if not 0 <= now_ms - tick["ts_ms"] <= 5_000 or not 0 <= now_ms - tick["received_at_ms"] <= 5_000:
                continue
            source = tick["source"]
            if source not in latest or tick["ts_ms"] > latest[source]["ts_ms"]:
                latest[source] = tick
        if not {"coinbase", "kraken"} <= latest.keys():
            return "Reference guard: fresh Coinbase and Kraken prices are required"
        for source in ("coinbase", "kraken"):
            price = dollars(latest[source]["price"])
            if price <= 0 or price == strike or (price > strike) != (index_price > strike):
                return f"Reference guard: {source} disagrees with the settlement index"
        return None

    async def account_quantity(self, ticker: str) -> Decimal:
        data = await self.rest.get_positions(ticker)
        if data.get("cursor"):
            raise ValueError("Incomplete portfolio response")
        return sum((dollars(row["position_fp"]) for row in data["market_positions"] if row["ticker"] == ticker), Decimal("0"))

    def entry_read(
        self, rule: EntryRule, market: dict, yes_bid: Decimal, yes_ask: Decimal,
    ) -> tuple[Decimal, str | None, str | None]:
        model_p = dollars(market["model_p_yes"])
        ticker = market["ticker"]
        # Scalping may only trade once the model has locked its call, and only with it.
        decision = self.store.locked_decision(ticker) if rule.scalp is not None else None
        if rule.scalp is not None and decision is None:
            return (yes_bid + yes_ask) / 2 if rule.side == "momentum" else model_p, None, locked_gate_reason(None, "yes")
        if rule.side != "momentum":
            side = rule.pick_side(model_p, yes_ask, ONE - yes_bid)
            gate = locked_gate_reason(decision, side) if decision is not None and side else None
            return model_p, None if gate else side, gate
        now_ms = int(time.time() * 1000)
        samples = [sample for sample in self.quote_history.get(ticker, [])
                   if 10_000 <= now_ms - sample[0] <= 60_000]
        probability = (yes_bid + yes_ask) / 2
        if not samples:
            return probability, None, "Warming up - need at least 10 seconds of recent quote movement"
        _, old_bid, old_ask = samples[-1]
        raw_drift = market.get("momentum_short_per_sec")
        if raw_drift is None:
            return probability, None, "No fresh short-term coin movement"
        drift = dollars(raw_drift)
        if yes_bid - old_bid >= CENT and yes_ask - old_ask >= CENT and drift > 0:
            side = "yes"
        elif old_bid - yes_bid >= CENT and old_ask - yes_ask >= CENT and drift < 0:
            side = "no"
        else:
            return probability, None, "Waiting for quote movement and short-term coin direction to agree"
        gate = locked_gate_reason(decision, side)
        if gate:
            return probability, None, gate
        _, latest_bid, latest_ask = self.quote_history[ticker][-1]
        if ((side == "yes" and (yes_bid < latest_bid or yes_ask < latest_ask))
                or (side == "no" and (yes_bid > latest_bid or yes_ask > latest_ask))):
            return probability, None, "Quote movement reversed during execution checks"
        ask = yes_ask if side == "yes" else ONE - yes_bid
        count = rule.policy.entry_count(ask)
        if count and not rule.policy.profit_target_reachable(ask, count):
            return probability, side, "Not enough price room for the net profit target after reserved fees"
        return probability, side, None

    def evaluate_rules(self) -> list[dict]:
        """Check every live market against the rules; refreshes the dashboard watch list.

        Returns candidates (market + first matching rule) whose quoted price
        satisfies a rule. Runs even while paused so the user can preview matches.
        """
        feed = self.market_feed() if self.market_feed is not None else []
        now_ms = int(time.time() * 1000)
        rules = [rule for rule in self.rules() if rule.enabled]
        risk = self.risk_limits()
        all_positions = self.positions()
        positions = {position["ticker"]: position for position in all_positions if position["status"] in ("pending", "open")}
        busy_series = {series_of(p["ticker"]) for p in all_positions if p["status"] in ("pending", "open")}
        watch, candidates = [], []
        active_tickers = {market["ticker"] for market in feed}
        self.quote_history = {ticker: samples for ticker, samples in self.quote_history.items()
                              if ticker in active_tickers}
        for market in sorted(feed, key=lambda row: row.get("close_ts_ms") or 0):
            ticker = market["ticker"]
            seconds_left = ((market.get("close_ts_ms") or 0) - now_ms) / 1000
            if seconds_left <= 0:
                continue
            row = {"ticker": ticker, "seconds_left": int(seconds_left),
                   "close_ts_ms": market["close_ts_ms"],
                   "model_p_yes": market.get("model_p_yes"), "market_p_yes": market.get("market_p_yes"),
                   "side": None, "price": None, "rule": None, "status": "watching"}
            watch.append(row)
            history = [p for p in all_positions if p["ticker"] == ticker]
            if history and history[-1].get("scalp"):
                spent, losses, realized = market_totals(history)
                row["scalping"] = {
                    "cycles": max(p.get("cycle_number", 1) for p in history),
                    "spent": str(spent), "losses": str(losses), "realized_pnl": str(realized),
                }
            try:
                model_p_yes = Decimal(str(round(float(market["model_p_yes"]), 4)))
                yes_bid = Decimal(str(market["yes_bid"]))
                yes_ask = Decimal(str(market["yes_ask"]))
                if (not model_p_yes.is_finite() or not yes_bid.is_finite() or not yes_ask.is_finite()
                        or not Decimal("0") <= model_p_yes <= ONE
                        or not Decimal("0") <= yes_bid <= yes_ask <= ONE):
                    raise ValueError("Invalid live price or probability")
            except (KeyError, TypeError, ValueError, ArithmeticError):
                row["status"] = "No live price"
                continue
            existing = positions.get(ticker)
            if existing is not None and existing["status"] != "open":
                row["status"] = f"Bot position {existing['status']}"
                continue
            if not 0 <= now_ms - int(market.get("ts_ms") or 0) <= risk.max_signal_age_ms:
                row["status"] = "Live read is stale"
                continue
            if market.get("quality_flags"):
                row["status"] = "Degraded inputs: " + ", ".join(market["quality_flags"])
                continue
            samples = [sample for sample in self.quote_history.get(ticker, [])
                       if now_ms - sample[0] <= 60_000]
            signal_ms = int(market["ts_ms"])
            if not samples or signal_ms > samples[-1][0]:
                samples.append((signal_ms, yes_bid, yes_ask))
            self.quote_history[ticker] = samples
            risk_reasons = self.risk_blockers()
            reference_reason = self.reference_reason(market, now_ms)
            if risk_reasons or reference_reason:
                row["status"] = "; ".join(risk_reasons) or reference_reason
                continue
            if not rules:
                row["status"] = "No enabled rules"
                continue
            if existing is not None:
                rule = next((r for r in rules if r.name == existing.get("rule")), None)
                blocked = "Bot position open (its rule is off)" if rule is None else can_add_to(existing, rule, now_ms)
                if blocked:
                    row["status"] = blocked
                    continue
                side = existing["side"]
                ask = yes_ask if side == "yes" else ONE - yes_bid
                reason = rule.check(ticker, seconds_left, model_p_yes, side, ask)
                if reason is None:
                    confidence = model_p_yes if side == "yes" else ONE - model_p_yes
                    reason = self.entry_risk_reason(confidence, ask, yes_bid if side == "yes" else ONE - yes_ask)
                if reason:
                    row["status"] = f"Bot position open ({entries_of(existing)}/{rule.max_entries} buys) - {reason}"
                    continue
                row.update(side=side, price=f"{ask:.2f}", rule=rule.name,
                           status=f"Matches '{rule.name}' again (buy {entries_of(existing) + 1}/{rule.max_entries})")
                candidates.append({"market": market, "rule": rule, "side": side,
                                   "model_p_yes": model_p_yes, "add_to": existing})
                continue
            reasons = []
            for rule in rules:
                blocked = can_start_cycle(history, rule, now_ms)
                if blocked:
                    reasons.append(f"{rule.name}: {blocked}")
                    continue
                entry_p, side, reason = self.entry_read(rule, market, yes_bid, yes_ask)
                if rule.side == "momentum":
                    row.update(strategy="momentum", side=side,
                               price=str(yes_ask if side == "yes" else ONE - yes_bid) if side else None,
                               entry_confidence=str(entry_p if side == "yes" else ONE - entry_p) if side else None)
                if reason or side is None:
                    reasons.append(f"{rule.name}: {reason}")
                    continue
                ask = yes_ask if side == "yes" else ONE - yes_bid
                reason = rule.check(ticker, seconds_left, entry_p, side, ask)
                if reason is None:
                    confidence = entry_p if side == "yes" else ONE - entry_p
                    reason = self.entry_risk_reason(confidence, ask, yes_bid if side == "yes" else ONE - yes_ask)
                if reason is None and rule.scalp:
                    spent, _, _ = market_totals(history)
                    available = scalp_limits(rule, history).market_spend_limit - spent
                    if rule.policy.entry_count(ask) == 0:
                        reason = f"Amount ${rule.policy.budget} buys no whole {side.upper()} contract and reserved entry fees at {ask}"
                    elif ask + fee_reserve(1) > available:
                        reason = "Scalping stopped - spending limit leaves no budget for a whole contract and fees"
                    elif self.retry_after_ms.get(ticker, 0) > now_ms:
                        reason = "Scalping retry - " + self.last_skip_reason.get(ticker, "waiting for a fresh execution check")
                if reason is None:
                    row.update(side=side, price=f"{ask:.2f}", rule=rule.name, status=f"Matches '{rule.name}'")
                    if rule.scalp:
                        cycle_number = max((p.get("cycle_number", 1) for p in history), default=0) + 1
                        row["status"] += f" (scalp cycle {cycle_number}/{scalp_limits(rule, history).max_cycles})"
                    if series_of(ticker) in busy_series:
                        row["status"] += " (waiting: a position on this coin is already open)"
                    else:
                        candidates.append({"market": market, "rule": rule, "side": side,
                                           "model_p_yes": entry_p})
                    break
                reasons.append(f"{rule.name}: {reason}")
            else:
                row["status"] = "No match - " + "; ".join(reasons)
        self.watch = watch
        return candidates

    def skip(self, ticker: str, reason: str, permanent: bool = False, position: dict | None = None) -> None:
        """Log why a matched market was not traded, without flooding the event log."""
        if permanent and position is not None:
            self.save_position(position)
        else:
            self.retry_after_ms[ticker] = int(time.time() * 1000) + _RETRY_AFTER_MS
        if self.last_skip_reason.get(ticker) != reason:
            self.last_skip_reason[ticker] = reason
            self.store.record_trading_event("skipped", reason, ticker=ticker, market_id=ticker,
                                            environment=self.environment, mode=self.mode)

    async def market_supported(self, ticker: str) -> bool:
        if ticker in self.eligible_markets:
            return True
        market = (await self.rest.get_market(ticker))["market"]
        event = (await self.rest.get_event(market["event_ticker"]))["event"]
        series = (await self.rest.get_series(event["series_ticker"]))["series"]
        fee_types = ("quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees")
        supported = not (
            market["status"] != "active" or series.get("fee_type") not in fee_types
            or market.get("market_type") != "binary"
            or dollars(market.get("notional_value_dollars", "0")) != ONE
            or market.get("settlement_bounds_type") != "default"
            or dollars(series.get("fee_multiplier", "1")) > ONE
            or event.get("fee_type_override") not in (None, *fee_types)
            or dollars(event.get("fee_multiplier_override") or "1") > ONE
        )
        if supported:
            self.eligible_markets.add(ticker)
        return supported

    async def enter_by_rules(self, candidates: list[dict]) -> None:
        current = {p["ticker"]: p for p in self.positions() if p["status"] in ("pending", "open")}
        busy_series = {series_of(p["ticker"]) for p in self.positions() if p["status"] in ("pending", "open")}
        for candidate in candidates:
            market, rule, side = candidate["market"], candidate["rule"], candidate["side"]
            ticker = market["ticker"]
            now_ms = int(time.time() * 1000)
            adding = candidate.get("add_to") is not None
            if self.retry_after_ms.get(ticker, 0) > now_ms:
                continue
            if not adding and series_of(ticker) in busy_series:
                continue
            if not self.enabled:
                return
            if (market.get("close_ts_ms") or 0) <= now_ms:
                self.skip(ticker, "Entry skipped - market expired during execution checks")
                continue
            policy = rule.policy
            risk = self.risk_limits()
            if self.risk_blockers():
                self.skip(ticker, "; ".join(self.risk_blockers()))
                continue
            if adding:
                position = current.get(ticker)
                if position is None or position["side"] != side or can_add_to(position, rule, now_ms):
                    continue
                position.setdefault("entries", entries_of(position))
            else:
                history = self.market_history(ticker)
                blocked = can_start_cycle(history, rule, now_ms)
                if blocked:
                    self.skip(ticker, blocked)
                    continue
                position = {
                    "ticker": ticker, "market_id": ticker, "side": side, "rule": rule.name,
                    "strategy": rule.side,
                    "close_ts_ms": market.get("close_ts_ms"), "opened_ms": now_ms,
                    "status": "skipped", "quantity": "0", "entry_cost": "0", "exit_credit": "0",
                    "policy": policy_json(policy), "pending": None, "net_pnl": None,
                    "account_identity": self.account_identity, "entries": 0,
                }
                if rule.scalp:
                    limits = scalp_limits(rule, history)
                    position.update(position_id=str(uuid4()), scalp=limits.to_json(),
                                    cycle_number=max((p.get("cycle_number", 1) for p in history), default=0) + 1)
            if not await self.market_supported(ticker):
                self.skip(ticker, "Market is not open or uses an unsupported fee schedule", not adding,
                          None if adding else position)
                continue
            held = dollars(position["quantity"]) if adding else Decimal("0")
            expected_account = held if side == "yes" else -held
            if await self.account_quantity(ticker) != expected_account:
                if adding:
                    raise ValueError("Account holdings differ from bot journal; manual intervention required")
                self.skip(ticker, "Existing account holdings in this market; bot will not mix positions",
                          True, position)
                continue
            orders = await self.rest.get_orders(ticker)
            if orders.get("cursor") or any(order["status"] == "resting" for order in orders["orders"]):
                self.skip(ticker, "Existing or incompletely checked account orders in this market")
                continue
            opposite = "no" if side == "yes" else "yes"
            book_started_ms = int(time.time() * 1000)
            book = (await self.rest.get_market_orderbook(ticker))["orderbook_fp"]
            asks = self.book_levels(book, opposite)
            bids = self.book_levels(book, side)
            if not asks:
                self.skip(ticker, f"Rule '{rule.name}' matched but there is no {side.upper()} liquidity")
                continue
            ask = ONE - asks[0][0]
            seconds_left = ((market.get("close_ts_ms") or 0) - int(time.time() * 1000)) / 1000
            reason = rule.check(ticker, seconds_left, candidate["model_p_yes"], side, ask)
            if reason:
                self.skip(ticker, f"Rule '{rule.name}' no longer matches at the live price: {reason}")
                continue
            count = min(policy.entry_count(ask), int(asks[0][1]))
            count = risk.affordable_count(count, ask, self.open_cost(), self.today_pnl(), self.daily_loss_limit)
            if rule.scalp:
                spent, _, _ = market_totals(self.market_history(ticker))
                available_budget = scalp_limits(rule, self.market_history(ticker)).market_spend_limit - spent
                while count and ask * count + fee_reserve(count) > available_budget:
                    count -= 1
            if count == 0:
                reason = "Not enough contracts offered at the best price"
                if risk.affordable_count(1, ask, self.open_cost(), self.today_pnl(), self.daily_loss_limit) == 0:
                    reason = "Risk limit: remaining exposure or daily loss budget buys no whole contract"
                elif policy.entry_count(ask) == 0:
                    reason = f"Bet ${policy.budget} buys no whole {side.upper()} contract at {ask}"
                elif rule.scalp:
                    reason = "Scalping spending limit leaves no budget for a whole contract and fees"
                self.skip(ticker, reason)
                continue
            if rule.side == "momentum" and not policy.profit_target_reachable(ask, count):
                self.skip(ticker, "Not enough price room for the net profit target after reserved fees")
                continue
            if policy.has_exits:
                if not bids or bids[0][1] < count:
                    self.skip(ticker, "Insufficient sell liquidity for the take-profit/stop-loss exits")
                    continue
                if rule.scalp:
                    cost = round_trip_cost(ask, bids[0][0], count)
                    if cost + STOP_HEADROOM > policy.stop_loss:
                        self.skip(ticker, f"Spread and fees would cost ${cost} to enter and exit right away, leaving "
                                          f"less than ${STOP_HEADROOM} of room before the ${policy.stop_loss} stop loss")
                        continue
                elif policy.stop_loss > 0 and (ask - bids[0][0]) * count + 2 * fee_reserve(count) >= policy.stop_loss:
                    self.skip(ticker, "Quoted spread and reserved fees already reach the stop loss")
                    continue
            if not self.enabled:
                return
            # Recompute with current index data and these executable quotes when available.
            now_ms = int(time.time() * 1000)
            yes_bids = bids if side == "yes" else asks
            no_bids = asks if side == "yes" else bids
            yes_bid = yes_bids[0][0] if yes_bids else Decimal("0")
            yes_ask = ONE - no_bids[0][0] if no_bids else ONE
            if not 0 <= now_ms - book_started_ms <= risk.max_book_age_ms:
                self.skip(ticker, "Entry skipped - order book request exceeded the freshness limit")
                continue
            if yes_bid > yes_ask:
                self.skip(ticker, "Entry skipped - order book is crossed")
                continue
            if self.execution_feed:
                fresh = self.execution_feed(
                    ticker, yes_bid, yes_ask, yes_bids[0][1] if yes_bids else Decimal("0"),
                    no_bids[0][1] if no_bids else Decimal("0"), now_ms,
                )
            elif self.market_feed:
                fresh = next((row for row in self.market_feed() if row["ticker"] == ticker), None)
            else:
                fresh = market
            now_ms = int(time.time() * 1000)
            if not 0 <= now_ms - book_started_ms <= risk.max_book_age_ms:
                self.skip(ticker, "Entry skipped - prediction recomputation exceeded book freshness")
                continue
            if fresh is None or not 0 <= now_ms - int(fresh.get("ts_ms") or 0) <= risk.max_signal_age_ms or fresh.get("quality_flags"):
                self.skip(ticker, "Entry skipped - live prediction is stale, missing, or degraded")
                continue
            fresh_p = dollars(fresh["model_p_yes"])
            if not Decimal("0") <= fresh_p <= ONE:
                raise ValueError("Invalid live model probability")
            entry_p, fresh_side, movement_reason = self.entry_read(rule, fresh, yes_bid, yes_ask)
            seconds_left = (fresh["close_ts_ms"] - now_ms) / 1000
            reason = movement_reason or rule.check(ticker, seconds_left, entry_p, side, ask)
            confidence = entry_p if side == "yes" else ONE - entry_p
            reason = reason or risk.quote_reason(confidence, ask, bids[0][0] if bids else None)
            reason = reason or self.reference_reason(fresh, now_ms)
            if reason or (not adding and fresh_side != side):
                self.skip(ticker, f"Entry changed - {reason or 'the preferred entry side changed'}")
                continue
            if self.blockers():
                self.skip(ticker, "; ".join(self.blockers()))
                continue
            if rule.scalp:
                blocked = can_start_cycle(self.market_history(ticker), rule, now_ms)
                if blocked:
                    self.skip(ticker, blocked)
                    continue
            now_ms = int(time.time() * 1000)
            if not 0 <= now_ms - book_started_ms <= risk.max_book_age_ms:
                self.skip(ticker, "Entry skipped - final safeguards exceeded book freshness")
                continue
            reason = rule.check(ticker, (fresh["close_ts_ms"] - now_ms) / 1000, entry_p, side, ask)
            if reason:
                self.skip(ticker, f"Entry changed before submission - {reason}")
                continue
            if rule.scalp is not None:
                locked = self.store.locked_decision(ticker)
                gate = locked_gate_reason(locked, side)
                if gate:
                    self.skip(ticker, f"Entry changed - {gate}")
                    continue
                position["entry_gate"] = locked_gate_snapshot(locked)
            position["entry_signal"] = {
                "ts_ms": fresh["ts_ms"], "model_p_yes": str(fresh_p),
                "confidence": str(confidence), "price": str(ask),
                "confidence_source": "market" if rule.side == "momentum" else "model",
            }
            position["execution_quote"] = {
                "signal_ts_ms": fresh["ts_ms"], "book_started_ms": book_started_ms,
                "decision_ms": now_ms, "ask": str(ask),
                "mid": str((yes_bid + yes_ask) / 2 if side == "yes" else ONE - (yes_bid + yes_ask) / 2),
                "best_size": str(asks[0][1]),
                "expected_edge": None if rule.side == "momentum" else str(confidence - ask - taker_fee(ask)),
                "confidence": str(confidence), "model": fresh.get("model"),
            }
            label = f" (buy {entries_of(position) + 1}/{rule.max_entries})" if rule.max_entries > 1 else ""
            if rule.scalp:
                label = f" (scalp cycle {position['cycle_number']}/{position['scalp']['max_cycles']})"
            await self.submit(position, "buy", Decimal(count), ask,
                              f"Rule '{rule.name}'{label}: {side.upper()} at {ask}, "
                              f"{'market confidence' if rule.side == 'momentum' else 'model'} {confidence:.2f}, "
                              f"fee {taker_fee(ask, count)}")
            busy_series.add(series_of(ticker))
            self.last_skip_reason.pop(ticker, None)
            if self.blockers():
                return

    async def submit(self, position: dict, action: str, quantity: Decimal, price: Decimal, reason: str) -> None:
        client_id = str(uuid4())
        payload = order_payload(position["ticker"], position["side"], action, quantity, price, client_id)
        position["pending"] = {"client_order_id": client_id, "market_id": position["ticker"],
                               "action": action, "quantity": str(quantity), "reason": reason}
        position["pending"]["submitted_ms"] = int(time.time() * 1000)
        if action == "buy" and position.get("execution_quote"):
            position["pending"]["execution_quote"] = dict(position["execution_quote"])
        if action == "buy":
            # An add-on buy keeps what is already held; reconcile adds the new fill on top.
            position["pending"]["base_quantity"] = position.get("quantity", "0")
            position["pending"]["base_cost"] = position.get("entry_cost", "0")
            position["pending"]["was_open"] = position.get("status") == "open"
            position["last_entry_ms"] = int(time.time() * 1000)
            position["status"] = "pending"
        self.save_position(position)
        self.event(f"{action}_submitted", reason, position)
        try:
            response = await self.rest.create_event_order(payload)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 401, 403, 404, 422, 429):
                was_open = position["pending"].get("was_open")
                position["pending"] = None
                position["status"] = "open" if action == "sell" or was_open else "skipped"
                self.save_position(position)
                self.event("rejected", f"Kalshi rejected {action} (HTTP {exc.response.status_code})", position)
            raise
        position["pending"]["order_id"] = response["order_id"]
        position["pending"]["ack_fill_count"] = str(dollars(response["fill_count"]))
        self.save_position(position)
        await self.reconcile(position)

    async def reconcile(self, position: dict) -> None:
        pending = position["pending"]
        order_id = pending.get("order_id")
        if order_id is None:
            cursor = ""
            while True:
                data = await self.rest.get_orders(position["ticker"], cursor)
                found = next((order for order in data["orders"] if order["client_order_id"] == pending["client_order_id"]), None)
                if found:
                    order_id = found["order_id"]
                    pending["order_id"] = order_id
                    self.save_position(position)
                    break
                cursor = data.get("cursor", "")
                if not cursor:
                    raise ValueError("Order outcome unknown; never resubmit automatically")
        order = (await self.rest.get_order(order_id))["order"]
        if order["client_order_id"] != pending["client_order_id"] or order["ticker"] != position["ticker"]:
            raise ValueError("Mismatched order response")
        if order["status"] == "resting":
            await self.rest.cancel_event_order(order_id, position["ticker"])
            return
        if order["status"] not in ("executed", "canceled"):
            raise ValueError("Nonterminal order")
        expected = dollars(order["fill_count_fp"])
        if "ack_fill_count" not in pending and expected == 0:
            raise ValueError("Submission acknowledgment was lost; zero fills cannot safely resolve this order yet")
        if expected < dollars(pending.get("ack_fill_count", "0")):
            raise ValueError("Order read is behind the submission acknowledgment; waiting for reconciliation")
        filled = Decimal("0")
        gross = Decimal("0")
        fees = Decimal("0")
        cursor = ""
        seen = set()
        while True:
            data = await self.rest.get_fills(order_id, cursor)
            for fill in data["fills"]:
                if fill["fill_id"] in seen:
                    continue
                seen.add(fill["fill_id"])
                if fill["order_id"] != order_id or fill["ticker"] != position["ticker"]:
                    raise ValueError("Mismatched fill")
                count = dollars(fill["count_fp"])
                price = dollars(fill[f"{position['side']}_price_dollars"])
                fee = dollars(fill["fee_cost"])
                if count <= 0 or not Decimal("0") <= price <= ONE or fee < 0:
                    raise ValueError("Invalid fill")
                filled += count
                gross += count * price
                fees += fee
            cursor = data.get("cursor", "")
            if not cursor:
                break
        if filled != expected or filled > dollars(pending["quantity"]) or filled < 0:
            raise ValueError("Fill history is incomplete or inconsistent")
        if pending["action"] == "buy":
            position["quantity"] = str(dollars(pending.get("base_quantity", "0")) + filled)
            position["entry_cost"] = str(dollars(pending.get("base_cost", "0")) + gross + fees)
            if filled:
                pnl_tracking(position, int(time.time() * 1000),
                             from_entry=dollars(pending.get("base_cost", "0")) == 0)
                position["entries"] = entries_of(position) + 1 if "entries" in position else 1
                if position.get("scalp"):
                    position["bought_quantity"] = str(dollars(position.get("bought_quantity", "0")) + filled)
        else:
            remaining = dollars(position["quantity"]) - filled
            if remaining < 0:
                raise ValueError("Exit exceeded bot position")
            position["quantity"] = str(remaining)
            position["exit_credit"] = str(dollars(position["exit_credit"]) + gross - fees)
        quote = pending.get("execution_quote")
        position.setdefault("execution_history", []).append({
            "market_id": position["ticker"],
            "order_id": order_id, "client_order_id": pending["client_order_id"],
            "action": pending["action"], "requested": pending["quantity"],
            "filled": str(filled), "gross": str(gross), "fees": str(fees),
            "average_price": str(gross / filled) if filled else None,
            "cost_above_mid": str(gross + fees - dollars(quote["mid"]) * filled) if quote else None,
            "submitted_ms": pending.get("submitted_ms"), "reconciled_ms": int(time.time() * 1000),
            "quote": quote,
        })
        position["pending"] = None
        position["status"] = "open" if dollars(position["quantity"]) > 0 else "closed"
        if position.get("scalp") and pending["action"] == "buy" and filled == 0 and not pending.get("was_open"):
            position["status"] = "skipped"
        position["net_pnl"] = (str(dollars(position["exit_credit"]) - dollars(position["entry_cost"]))
                               if position["status"] == "closed" else None)
        if position["status"] == "closed" and pending["action"] == "sell":
            position["closed_by"] = pending["reason"]
            observe_pnl(position, dollars(position["net_pnl"]), int(time.time() * 1000), "exit_fill")
        self.save_position(position)
        self.event("filled" if filled else "unfilled", f"{pending['action']} filled {filled} contracts: {pending['reason']}", position)
        if pending["action"] == "buy" and filled:
            self.notify(lambda n: n.trade(trade_embed(position, self.notify_label)))
        elif position["status"] == "closed":
            self.notify_closed(position)
        if dollars(position["entry_cost"]) > parse_policy(position["policy"]).budget * max(1, entries_of(position)):
            raise ValueError("Actual entry fees exceeded budget")
        if position.get("scalp") and pending["action"] == "buy":
            spent, _, _ = market_totals(self.market_history(position["ticker"]))
            if spent > dollars(position["scalp"]["market_spend_limit"]):
                raise ValueError("Actual scalp entry costs exceeded market spending limit; entries paused")

    async def monitor(self, position: dict) -> None:
        position["net_pnl"] = None
        self.save_position(position)
        # The three reads are independent, so one round trip of latency instead of three.
        market_result, actual_result, levels_result = await asyncio.gather(
            self.rest.get_market(position["ticker"]), self.account_quantity(position["ticker"]),
            self.levels(position["ticker"], position["side"]), return_exceptions=True)
        if isinstance(market_result, BaseException):
            raise market_result
        observed_ms = int(time.time() * 1000)
        market = market_result["market"]
        quantity = dollars(position["quantity"])
        if market.get("result") in ("yes", "no") and market["status"] == "finalized":
            payout = quantity if market["result"] == position["side"] else Decimal("0")
            position["exit_credit"] = str(dollars(position["exit_credit"]) + payout)
            position["quantity"] = "0"
            position["status"] = "closed"
            position["closed_by"] = "settled"
            position["result"] = market["result"]
            position["net_pnl"] = str(dollars(position["exit_credit"]) - dollars(position["entry_cost"]))
            observe_pnl(position, dollars(position["net_pnl"]), observed_ms, "settlement")
            self.save_position(position)
            self.event("settled", f"Market settled {market['result']}", position)
            self.notify_closed(position)
            return
        if market["status"] != "active":
            return
        for result in (actual_result, levels_result):
            if isinstance(result, BaseException):
                raise result
        actual, levels = actual_result, levels_result
        if actual != (quantity if position["side"] == "yes" else -quantity):
            raise ValueError("Account holdings differ from bot journal; manual intervention required")
        tracking = pnl_tracking(position, observed_ms)
        remaining = quantity
        proceeds = Decimal("0")
        exit_fees = Decimal("0")
        worst_price = None
        for price, available in levels:
            matched = min(available, remaining)
            proceeds += price * matched
            exit_fees += taker_fee(price, int(matched.to_integral_value(rounding=ROUND_CEILING)))
            remaining -= matched
            worst_price = price
            if remaining == 0:
                break
        if worst_price is None:
            tracking["unavailable_samples"] += 1
            self.save_position(position)
            if not position.get("liquidity_warning") and parse_policy(position["policy"]).has_exits:
                position["liquidity_warning"] = True
                self.save_position(position)
                self.event("waiting_for_liquidity", "No sell liquidity; exit cannot be filled yet", position)
            return
        reserve = fee_reserve(int(quantity.to_integral_value(rounding=ROUND_CEILING)))
        if position.get("scalp") and remaining == 0:
            # Scalp targets are a few cents, so use the real exit fee instead of the flat worst case.
            reserve = exit_fees
        net = dollars(position["exit_credit"]) + proceeds - reserve - dollars(position["entry_cost"])
        position["net_pnl"] = str(net) if remaining == 0 else None
        if remaining == 0:
            observe_pnl(position, net, observed_ms, "liquidation_quote")
        else:
            tracking["unavailable_samples"] += 1
        position["liquidity_warning"] = remaining > 0
        self.save_position(position)
        policy = parse_policy(position["policy"])
        mark = dollars(position["exit_credit"]) + levels[0][0] * quantity - reserve - dollars(position["entry_cost"])
        reason = position.get("exit_trigger") or (
            policy.exit_reason(net) if remaining == 0
            else "stop_loss" if policy.stop_loss > 0 and mark <= -policy.stop_loss else None
        )
        if position.get("scalp"):
            _, losses, _ = market_totals(self.market_history(position["ticker"]))
            limits = dollars(position["scalp"]["market_loss_limit"])
            rule = next((r for r in self.rules() if r.name == position.get("rule") and r.scalp), None)
            if rule is not None:
                limits = min(limits, rule.scalp.market_loss_limit)
            liquidation_pnl = net if remaining == 0 else mark
            if liquidation_pnl <= -(limits - losses):
                reason = "stop_loss"
        if reason:
            if reason == "stop_loss":
                if not position.get("exit_trigger"):
                    position["exit_trigger_ms"] = int(time.time() * 1000)
                position["exit_trigger"] = reason
            self.save_position(position)
            if reason == "take_profit":
                minimum = ((policy.take_profit + dollars(position["entry_cost"]) - dollars(position["exit_credit"]) + reserve)
                           / quantity).quantize(CENT, rounding=ROUND_CEILING)
                worst_price = max(worst_price, minimum)
                if worst_price >= ONE:
                    return
            await self.submit(position, "sell", quantity - remaining, worst_price, reason)