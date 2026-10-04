"""Persistent, opt-in event-market trading and fill reconciliation."""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from decimal import Decimal, ROUND_CEILING
from typing import Any
from uuid import uuid4

import httpx

from .data.store import Store
from .trading import CENT, ONE, TradingPolicy, dollars, fee_reserve


def policy_json(policy: TradingPolicy) -> dict[str, str]:
    return {name: str(value) for name, value in asdict(policy).items()}


def parse_policy(values: dict) -> TradingPolicy:
    return TradingPolicy(**{name: dollars(values[name]) for name in ("budget", "take_profit", "stop_loss")})


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
        account_identity: str = "", worker_id: str | None = None,
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
        self.worker_claimed = False
        self.control_revision = 0
        self.enabled = False
        self.enabled_since_ms = 0
        self.last_cycle_ms: int | None = None
        self.error: str | None = None
        self.lock = asyncio.Lock()
        self.settings_key = f"settings:{mode}"
        self.position_kind = "position:paper" if mode == "paper" else f"position:{environment}"
        if store.trading_record(self.settings_key) is None:
            inherited = store.trading_record("settings") if mode == "live" else None
            store.save_trading_record(self.settings_key, "settings",
                                      inherited or policy_json(defaults or TradingPolicy()))
        if rest is not None:
            store.record_trading_event("started_paused", "Bot restarted with new entries paused",
                                       environment=environment, mode=mode)

    def policy(self) -> TradingPolicy:
        return parse_policy(self.store.trading_record(self.settings_key))

    def positions(self) -> list[dict]:
        return self.store.trading_records(self.position_kind)

    def blockers(self) -> list[str]:
        blockers = []
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
        return {
            "mode": self.mode,
            "settings": policy_json(self.policy()), "enabled": self.enabled,
            "environment": self.environment,
            "blockers": self.blockers(), "last_cycle_ms": self.last_cycle_ms, "error": self.error,
            "positions": self.positions()[-100:], "events": events,
            "decisions": self.store.trading_decisions(),
        }

    async def save_settings(self, values: dict) -> dict:
        policy = parse_policy(values)
        async with self.lock:
            if self.enabled:
                raise ValueError("Pause trading before changing settings")
            self.store.save_trading_record(self.settings_key, "settings", policy_json(policy))
            self.store.record_trading_event("settings_saved", "Trading targets saved",
                                            settings=policy_json(policy), mode=self.mode)
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
            if expected_settings is not None and parse_policy(expected_settings) != self.policy():
                raise ValueError("Saved settings changed; review and confirm again")
            blockers = self.blockers()
            if blockers:
                raise ValueError("; ".join(blockers))
            self.enabled_since_ms = int(time.time() * 1000)
            self.enabled = True
            self.store.record_trading_event(
                "enabled", "Trading enabled for new decisions", environment=self.environment,
                settings=policy_json(self.policy()), mode=self.mode,
            )
            return self.snapshot()

    def save_position(self, position: dict) -> None:
        self.store.save_trading_record(f"{self.position_kind}:{position['ticker']}", self.position_kind, position)

    def event(self, action: str, reason: str, position: dict) -> None:
        self.store.record_trading_event(action, reason, ticker=position["ticker"],
                                        environment=self.environment, mode=self.mode)

    async def cycle(self) -> None:
        async with self.lock:
            try:
                if self.execution_allowed and self.rest is not None:
                    if not self.account_identity:
                        raise ValueError("Trading credentials are not identified")
                    self.worker_claimed = self.store.claim_trading_worker(self.worker_id)
                    if not self.worker_claimed:
                        raise ValueError("Another trading worker owns this database")
                    for position in self.positions():
                        if position["status"] in ("pending", "open") and position.get("account_identity") != self.account_identity:
                            raise ValueError("Trading credentials changed; position ownership cannot be verified")
                        if position.get("pending"):
                            await self.reconcile(position)
                        if position["status"] == "open" and not position.get("pending"):
                            await self.monitor(position)
                    if self.enabled and not self.blockers():
                        await self.enter_new_decision()
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

    async def run(self) -> None:
        try:
            while True:
                await self.cycle()
                await asyncio.sleep(2)
        finally:
            self.enabled = False
            self.store.release_trading_worker(self.worker_id)
            self.worker_claimed = False

    async def levels(self, ticker: str, side: str) -> list[tuple[Decimal, Decimal]]:
        data = await self.rest.get_market_orderbook(ticker)
        book = data["orderbook_fp"]
        levels = [(dollars(price), dollars(count)) for price, count in book[f"{side}_dollars"]]
        if any(not Decimal("0") < price < ONE or count < 0 for price, count in levels):
            raise ValueError("Invalid order book")
        return sorted((level for level in levels if level[1] > 0), reverse=True)

    async def account_quantity(self, ticker: str) -> Decimal:
        data = await self.rest.get_positions(ticker)
        if data.get("cursor"):
            raise ValueError("Incomplete portfolio response")
        return sum((dollars(row["position_fp"]) for row in data["market_positions"] if row["ticker"] == ticker), Decimal("0"))

    async def enter_new_decision(self) -> None:
        existing = {position["ticker"] for position in self.positions()}
        busy = any(position["status"] in ("pending", "open") for position in self.positions())
        now_ms = int(time.time() * 1000)
        for decision in reversed(self.store.trading_decisions()):
            if decision["ticker"] in existing or decision["ts_ms"] <= self.enabled_since_ms:
                continue
            if now_ms - decision["ts_ms"] > 30_000:
                continue
            policy = self.policy()
            position = {
                "ticker": decision["ticker"], "side": "yes" if decision["recommendation"] == "BUY_YES" else "no",
                "status": "skipped", "quantity": "0", "entry_cost": "0", "exit_credit": "0",
                "policy": policy_json(policy), "pending": None, "net_pnl": None,
                "account_identity": self.account_identity,
            }
            reason = None
            if decision["recommendation"] not in ("BUY_YES", "BUY_NO"):
                reason = "Model chose no trade"
            elif busy:
                reason = "One bot position is already open or pending"
            elif not decision["close_ts_ms"] or decision["close_ts_ms"] <= now_ms:
                reason = "Market is closed"
            if reason is None:
                market = (await self.rest.get_market(position["ticker"]))["market"]
                event = (await self.rest.get_event(market["event_ticker"]))["event"]
                series = (await self.rest.get_series(event["series_ticker"]))["series"]
                fee_types = ("quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees")
                if (market["status"] != "active" or series.get("fee_type") not in fee_types
                    or market.get("market_type") != "binary"
                    or dollars(market.get("notional_value_dollars", "0")) != ONE
                    or market.get("settlement_bounds_type") != "default"
                    or dollars(series.get("fee_multiplier", "1")) > ONE
                    or event.get("fee_type_override") not in (None, *fee_types)
                    or dollars(event.get("fee_multiplier_override") or "1") > ONE):
                    reason = "Market is not open or uses an unsupported fee schedule"
                elif await self.account_quantity(position["ticker"]) != 0:
                    reason = "Existing account holdings in this market; bot will not mix positions"
                else:
                    orders = await self.rest.get_orders(position["ticker"])
                    if orders.get("cursor") or any(order["status"] == "resting" for order in orders["orders"]):
                        reason = "Existing or incompletely checked account orders in this market"
            if reason is None:
                opposite = "no" if position["side"] == "yes" else "yes"
                asks = await self.levels(position["ticker"], opposite)
                if not asks:
                    reason = "No entry liquidity"
                else:
                    ask = ONE - asks[0][0]
                    count = policy.entry_count(ask)
                    if count == 0 or asks[0][1] < count:
                        reason = "Budget, profit target, or entry liquidity does not permit this trade"
                    else:
                        bids = await self.levels(position["ticker"], position["side"])
                        if not bids or bids[0][1] < count:
                            reason = "Insufficient initial sell liquidity"
                        elif (ask - bids[0][0]) * count + 2 * fee_reserve(count) >= policy.stop_loss:
                            reason = "Quoted spread and reserved fees already reach the loss limit"
                        else:
                            if not self.enabled or int(time.time() * 1000) - decision["ts_ms"] > 30_000:
                                return
                            await self.submit(position, "buy", Decimal(count), ask, "Locked model decision")
                            return
            self.save_position(position)
            self.event("skipped", reason, position)

    async def submit(self, position: dict, action: str, quantity: Decimal, price: Decimal, reason: str) -> None:
        client_id = str(uuid4())
        payload = order_payload(position["ticker"], position["side"], action, quantity, price, client_id)
        position["pending"] = {"client_order_id": client_id, "action": action, "quantity": str(quantity), "reason": reason}
        if action == "buy":
            position["status"] = "pending"
        self.save_position(position)
        self.event(f"{action}_submitted", reason, position)
        try:
            response = await self.rest.create_event_order(payload)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 401, 403, 404, 422, 429):
                position["pending"] = None
                position["status"] = "skipped" if action == "buy" else "open"
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
            position["quantity"] = str(filled)
            position["entry_cost"] = str(gross + fees)
        else:
            remaining = dollars(position["quantity"]) - filled
            if remaining < 0:
                raise ValueError("Exit exceeded bot position")
            position["quantity"] = str(remaining)
            position["exit_credit"] = str(dollars(position["exit_credit"]) + gross - fees)
        position["pending"] = None
        position["status"] = "open" if dollars(position["quantity"]) > 0 else "closed"
        position["net_pnl"] = (str(dollars(position["exit_credit"]) - dollars(position["entry_cost"]))
                               if position["status"] == "closed" else None)
        self.save_position(position)
        self.event("filled" if filled else "unfilled", f"{pending['action']} filled {filled} contracts: {pending['reason']}", position)
        if dollars(position["entry_cost"]) > parse_policy(position["policy"]).budget:
            raise ValueError("Actual entry fees exceeded budget")

    async def monitor(self, position: dict) -> None:
        position["net_pnl"] = None
        self.save_position(position)
        market = (await self.rest.get_market(position["ticker"]))["market"]
        quantity = dollars(position["quantity"])
        if market.get("result") in ("yes", "no") and market["status"] == "finalized":
            payout = quantity if market["result"] == position["side"] else Decimal("0")
            position["exit_credit"] = str(dollars(position["exit_credit"]) + payout)
            position["quantity"] = "0"
            position["status"] = "closed"
            position["net_pnl"] = str(dollars(position["exit_credit"]) - dollars(position["entry_cost"]))
            self.save_position(position)
            self.event("settled", f"Market settled {market['result']}", position)
            return
        if market["status"] != "active":
            return
        actual = await self.account_quantity(position["ticker"])
        if actual != (quantity if position["side"] == "yes" else -quantity):
            raise ValueError("Account holdings differ from bot journal; manual intervention required")
        levels = await self.levels(position["ticker"], position["side"])
        remaining = quantity
        proceeds = Decimal("0")
        worst_price = None
        for price, available in levels:
            matched = min(available, remaining)
            proceeds += price * matched
            remaining -= matched
            worst_price = price
            if remaining == 0:
                break
        if worst_price is None:
            if not position.get("liquidity_warning"):
                position["liquidity_warning"] = True
                self.save_position(position)
                self.event("waiting_for_liquidity", "No sell liquidity; exit cannot be filled yet", position)
            return
        reserve = fee_reserve(int(quantity.to_integral_value(rounding=ROUND_CEILING)))
        net = dollars(position["exit_credit"]) + proceeds - reserve - dollars(position["entry_cost"])
        position["net_pnl"] = str(net) if remaining == 0 else None
        position["liquidity_warning"] = remaining > 0
        self.save_position(position)
        policy = parse_policy(position["policy"])
        mark = dollars(position["exit_credit"]) + levels[0][0] * quantity - reserve - dollars(position["entry_cost"])
        reason = position.get("exit_trigger") or (
            policy.exit_reason(net) if remaining == 0 else "stop_loss" if mark <= -policy.stop_loss else None
        )
        if reason:
            if reason == "stop_loss":
                position["exit_trigger"] = reason
            self.save_position(position)
            if reason == "take_profit":
                minimum = ((policy.take_profit + dollars(position["entry_cost"]) - dollars(position["exit_credit"]) + reserve)
                           / quantity).quantize(CENT, rounding=ROUND_CEILING)
                worst_price = max(worst_price, minimum)
                if worst_price >= ONE:
                    return
            await self.submit(position, "sell", quantity - remaining, worst_price, reason)