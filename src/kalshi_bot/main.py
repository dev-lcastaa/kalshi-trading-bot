"""Orchestrator: wires REST discovery, WS ingestion, prediction, and the dashboard.

This process only reads market data and computes signals - it never places,
amends, or cancels orders.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict
from datetime import datetime
from typing import Any

import uvicorn

from .auth import KalshiAuth
from .config import Settings
from .dashboard.broadcaster import Broadcaster
from .dashboard.server import create_app
from .data.store import Store
from .external_prices import _TICK_QUEUE_MAXSIZE, aggregate_external_prices, collect_coinbase, collect_kraken, persist_external_ticks
from .features.engine import Features, build_features
from .kalshi_client.models import Signal
from .kalshi_client.rest import KalshiRestClient
from .kalshi_client.ws import KalshiWsClient
from .llm_review import LlmReviewer
from .market_discovery import find_15min_markets
from .prediction.calibration import IsotonicCalibrator
from .prediction.logistic import FEATURE_NAMES, LogisticRegressionModel, LogisticSignalPredictor, fit_logistic_model
from .prediction.model import RandomWalkPredictor, RegularizedSettlementPredictor, SettlementAwarePredictor
from .signals.confirmation import check_confirmation
from .signals.generator import generate_signal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

_INDEX_LOOKBACK_MS = 5 * 60 * 1000
_REDISCOVERY_INTERVAL_SEC = 90
_OUTCOME_POLL_INTERVAL_SEC = 60
_OUTCOME_MAX_AGE_MS = 24 * 60 * 60 * 1000  # stop polling for a result after 24h


def _parse_ts_ms(value: str | None) -> int | None:
    if not value:
        return None
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


class MarketState:
    def __init__(self, ticker: str, strike: float, close_ts_ms: int, index_id: str):
        self.ticker = ticker
        self.strike = strike
        self.close_ts_ms = close_ts_ms
        self.index_id = index_id
        self.yes_bid_dollars: float | None = None
        self.yes_ask_dollars: float | None = None
        self.yes_bid_size: float = 0.0
        self.yes_ask_size: float = 0.0
        self.quote_ts_ms: int | None = None
        self.quote_received_at_ms: int | None = None


class BotApp:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.auth = KalshiAuth.from_file(settings.key_id, settings.private_key_path)
        self.rest = KalshiRestClient(settings.rest_base, self.auth)
        self.store = Store(settings.database_url)
        self.predictor = (
            RegularizedSettlementPredictor() if settings.predictor_version == "v3"
            else SettlementAwarePredictor() if settings.predictor_version == "v2"
            else RandomWalkPredictor()
        )
        self.calibrator = IsotonicCalibrator(
            min_samples=getattr(settings, "calibration_min_samples", 200)
        )
        self.logistic_model = LogisticRegressionModel(
            feature_names=list(FEATURE_NAMES),
            min_samples=getattr(settings, "logistic_min_samples", 300),
        )
        self.llm_reviewer = LlmReviewer(
            getattr(settings, "llm_base_url", ""),
            model=getattr(settings, "llm_model", ""),
            timeout_sec=getattr(settings, "llm_timeout_sec", 15.0),
        )
        self.markets: dict[str, MarketState] = {}
        self.index_ticks: dict[str, list[tuple[int, float]]] = {
            idx: [] for idx in settings.index_ids
        }
        self.coin_to_index = dict(zip(settings.coin_ticks, settings.index_ids))
        self.broadcaster = Broadcaster()
        self.ws: KalshiWsClient | None = None
        self._ws_task: asyncio.Task | None = None

    def _index_for_ticker(self, ticker: str) -> str | None:
        for coin, index_id in self.coin_to_index.items():
            if coin.upper() in ticker.upper():
                return index_id
        return None

    async def discover_and_subscribe(self) -> None:
        raw_markets = await find_15min_markets(self.rest, self.settings.coin_ticks)
        new_state: dict[str, MarketState] = {}
        for m in raw_markets:
            ticker = m["ticker"]
            strike = m.get("floor_strike") if m.get("floor_strike") is not None else m.get("cap_strike")
            close_ts_ms = _parse_ts_ms(m.get("close_time"))
            index_id = self._index_for_ticker(ticker)
            if strike is None or close_ts_ms is None or index_id is None:
                continue
            new_state[ticker] = MarketState(ticker, float(strike), close_ts_ms, index_id)

        self._sync_market_lifecycle(new_state)

        if set(new_state) == set(self.markets):
            return  # no change in the active market set

        logger.info("Discovered %d open 15-min crypto markets: %s", len(new_state), list(new_state))
        self.markets = new_state
        await self._restart_ws()

    def _sync_market_lifecycle(self, discovered: dict[str, MarketState]) -> None:
        """Update the DB's active/closed status regardless of WS resubscription."""
        now_ms = int(time.time() * 1000)
        previously_active = set(self.store.get_active_tickers())
        for ticker in previously_active - set(discovered):
            state = self.markets.get(ticker)
            if state is not None:
                self._finalize_signal(ticker, state)
            self.store.mark_closed(ticker, now_ms)
        for ticker, state in discovered.items():
            self.store.upsert_active_market(ticker, state.index_id, state.strike, state.close_ts_ms, now_ms)

    def _finalize_signal(self, ticker: str, state: MarketState) -> None:
        """Record one prediction pinned exactly at close (seconds_to_expiry=0),
        using the buffered settlement-window ticks. The periodic prediction_loop
        stops once seconds_to_expiry<=0, so without this the "latest" signal used
        for calibration could be up to poll_interval_sec stale relative to the
        true close - this guarantees an apples-to-apples final call per market.
        """
        if state.yes_bid_dollars is None or state.yes_ask_dollars is None:
            return
        ticks = self.index_ticks.get(state.index_id, [])
        if len(ticks) < 2:
            return
        features = build_features(
            ticks,
            strike=state.strike,
            seconds_to_expiry=0.0,
            yes_bid_size=state.yes_bid_size,
            yes_ask_size=state.yes_ask_size,
            close_ts_ms=state.close_ts_ms,
        )
        signal = generate_signal(
            ticker=ticker,
            index_id=state.index_id,
            features=features,
            predictor=self.predictor,
            yes_bid_dollars=state.yes_bid_dollars,
            yes_ask_dollars=state.yes_ask_dollars,
            edge_threshold=self.settings.edge_threshold,
            fee_multiplier=getattr(self.settings, "fee_multiplier", 1.0),
            slippage_per_contract=getattr(self.settings, "slippage_per_contract", 0.0),
            ts_ms=state.close_ts_ms,
            market_blend_weight=getattr(self.settings, "market_blend_weight", 0.0),
            calibrator=self.calibrator,
        )
        self.store.insert_signal(signal)

    async def _restart_ws(self) -> None:
        if self.ws is not None:
            self.ws.stop()
        if self._ws_task is not None:
            self._ws_task.cancel()

        ws = KalshiWsClient(self.settings.ws_url, self.auth, on_message=self._on_ws_message)
        tickers = list(self.markets)
        if tickers:
            ws.queue_subscribe(["ticker"], market_tickers=tickers)
        ws.queue_subscribe(["cfbenchmarks_value"], index_ids=self.settings.index_ids)
        self.ws = ws
        self._ws_task = asyncio.create_task(ws.run_forever())

    async def _on_ws_message(self, data: dict[str, Any]) -> None:
        msg_type = data.get("type")
        if msg_type == "ticker":
            self._handle_ticker(data["msg"])
        elif msg_type == "cfbenchmarks_value":
            await self._handle_index_value(data["msg"])
        elif msg_type == "error":
            logger.warning("WS error: %s", data.get("msg"))

    def _handle_ticker(self, msg: dict[str, Any]) -> None:
        ticker = msg["market_ticker"]
        state = self.markets.get(ticker)
        if state is None:
            return
        state.yes_bid_dollars = float(msg["yes_bid_dollars"])
        state.yes_ask_dollars = float(msg["yes_ask_dollars"])
        state.yes_bid_size = float(msg["yes_bid_size_fp"])
        state.yes_ask_size = float(msg["yes_ask_size_fp"])
        state.quote_ts_ms = int(msg["ts_ms"])
        state.quote_received_at_ms = int(time.time() * 1000)
        self.store.insert_market_tick(
            market_ticker=ticker,
            ts_ms=int(msg["ts_ms"]),
            price_dollars=float(msg["price_dollars"]),
            yes_bid_dollars=state.yes_bid_dollars,
            yes_ask_dollars=state.yes_ask_dollars,
            yes_bid_size=state.yes_bid_size,
            yes_ask_size=state.yes_ask_size,
            volume=float(msg["volume_fp"]),
            open_interest=float(msg["open_interest_fp"]),
        )

    async def _handle_index_value(self, msg: dict[str, Any]) -> None:
        index_id = msg["index_id"]
        try:
            value = float(json.loads(msg["data"])["value"])
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            value = float(msg["avg_60s_data"]["value"])
        ts_ms = int(msg["received_at"])

        buf = self.index_ticks.setdefault(index_id, [])
        buf.append((ts_ms, value))
        cutoff = ts_ms - _INDEX_LOOKBACK_MS
        while buf and buf[0][0] < cutoff:
            buf.pop(0)

        self.store.insert_index_tick(index_id, ts_ms, value)
        await self.broadcaster.broadcast(
            {"type": "index_tick", "index_id": index_id, "ts_ms": ts_ms, "value": value}
        )

    def _build_shadow_snapshot(
        self,
        state: MarketState,
        features: Features,
        signal: Signal,
        decision_recommendation: str,
        now_ms: int,
        ticks: list[tuple[int, float]],
    ) -> dict | None:
        if not isinstance(self.predictor, SettlementAwarePredictor):
            return None
        if state.yes_bid_dollars is None or state.yes_ask_dollars is None:
            return None
        challenger_base = RegularizedSettlementPredictor(
            momentum_weight=self.predictor.momentum_weight,
            window_sec=self.predictor.window_sec,
        )
        challenger = LogisticSignalPredictor(challenger_base, self.logistic_model)
        shadow_signal = generate_signal(
            ticker=state.ticker, index_id=state.index_id, features=features,
            predictor=challenger, yes_bid_dollars=state.yes_bid_dollars,
            yes_ask_dollars=state.yes_ask_dollars,
            edge_threshold=self.settings.edge_threshold,
            fee_multiplier=getattr(self.settings, "fee_multiplier", 1.0),
            slippage_per_contract=getattr(self.settings, "slippage_per_contract", 0.0),
            ts_ms=now_ms,
        )
        shadow_confirmation = check_confirmation(features, shadow_signal.model_p_yes >= 0.5)
        parameters = {
            "model_version": type(self.predictor).__name__,
            "momentum_weight": self.predictor.momentum_weight,
            "live_imbalance_weight": self.predictor.imbalance_weight,
            "shadow_imbalance_weight": 0.0,
            "shadow_model_version": "logistic-stacked-v1",
            "shadow_base_model_version": "regularized-settlement-v3",
            "shadow_min_history_sec": challenger_base.min_history_sec,
            "shadow_min_history_ticks": challenger_base.min_history_ticks,
            "shadow_logistic_feature_names": list(FEATURE_NAMES),
            "shadow_logistic_min_samples": self.logistic_model.min_samples,
            "shadow_logistic_fitted": self.logistic_model.is_fitted,
            "shadow_logistic_fitted_n": self.logistic_model.fitted_n,
            # The shadow challenger never blends with the market or applies
            # calibration, so this doubles as an ongoing test of whether those two
            # live-only adjustments actually help versus the raw model.
            "live_market_blend_weight": getattr(self.settings, "market_blend_weight", 0.0),
            "shadow_market_blend_weight": 0.0,
            "live_calibrated": self.calibrator.is_fitted,
            "shadow_calibrated": False,
            "window_sec": self.predictor.window_sec,
            "edge_threshold": self.settings.edge_threshold,
            "fee_multiplier": getattr(self.settings, "fee_multiplier", 1.0),
            "slippage_per_contract": getattr(self.settings, "slippage_per_contract", 0.0),
            "decision_lead_sec": self.settings.decision_lead_sec,
            "confirmation_version": "majority-v1",
            "recommendation_version": "purchase-price-v2",
        }
        fingerprint = hashlib.sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()[:16]
        snapshot = {
            "schema_version": 1,
            "experiment_id": "logistic-stacked-v1-" + fingerprint,
            "parameters": parameters,
            "index_id": state.index_id,
            "features": asdict(features),
            "index_ticks": list(ticks),
            "index_tick_ts_ms": ticks[-1][0],
            "index_tick_age_ms": now_ms - ticks[-1][0],
            "quotes": {
                "yes_bid_dollars": state.yes_bid_dollars,
                "yes_ask_dollars": state.yes_ask_dollars,
                "no_ask_dollars": 1.0 - state.yes_bid_dollars,
                "yes_bid_size": state.yes_bid_size,
                "yes_ask_size": state.yes_ask_size,
                "ts_ms": state.quote_ts_ms,
                "received_at_ms": state.quote_received_at_ms,
                "age_ms": now_ms - state.quote_ts_ms if state.quote_ts_ms is not None else None,
            },
            "live": {
                "model_p_yes": signal.model_p_yes,
                "raw_recommendation": signal.recommendation,
                "recommendation": decision_recommendation,
                "confirmation": asdict(check_confirmation(features, signal.model_p_yes >= 0.5)),
            },
            "shadow": {
                "model_p_yes": shadow_signal.model_p_yes,
                "raw_recommendation": shadow_signal.recommendation,
                "recommendation": shadow_signal.recommendation if shadow_confirmation.confirmed else "NO_EDGE",
                "confirmation": asdict(shadow_confirmation),
            },
            "market_p_yes": signal.market_p_yes,
        }
        json.dumps(snapshot, allow_nan=False)
        return snapshot

    def _build_decision_snapshot(
        self,
        state: MarketState,
        features: Features,
        signal: Signal,
        decision_recommendation: str,
        confirmation: Any,
        now_ms: int,
        ticks: list[tuple[int, float]],
    ) -> dict:
        quote_age_ms = now_ms - state.quote_ts_ms if state.quote_ts_ms is not None else None
        index_tick_age_ms = now_ms - ticks[-1][0] if ticks else None
        external = aggregate_external_prices(
            self.store.recent_external_ticks(now_ms - 5_000), now_ms, max_age_ms=5_000
        ).get(state.index_id)
        confirmation = check_confirmation(features, signal.model_p_yes >= 0.5)
        quality_flags = self._quality_flags(state, features, ticks, now_ms)
        external = aggregate_external_prices(
            self.store.recent_external_ticks(now_ms - 5_000), now_ms, max_age_ms=5_000
        ).get(state.index_id)
        parameters = {
            "predictor_version": getattr(self.settings, "predictor_version", type(self.predictor).__name__),
            "poll_interval_sec": self.settings.poll_interval_sec,
            "edge_threshold": self.settings.edge_threshold,
            "fee_multiplier": getattr(self.settings, "fee_multiplier", 1.0),
            "slippage_per_contract": getattr(self.settings, "slippage_per_contract", 0.0),
            "decision_lead_sec": self.settings.decision_lead_sec,
            "confirmation_version": "majority-v1",
            "recommendation_version": "purchase-price-v2",
        }
        fingerprint = hashlib.sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()[:16]
        return {
            "schema_version": 1,
            "experiment_id": "live-" + fingerprint,
            "index_id": state.index_id,
            "ticker": state.ticker,
            "decision_ts_ms": now_ms,
            "close_ts_ms": state.close_ts_ms,
            "features": asdict(features),
            "index_ticks": list(ticks),
            "index_tick_age_ms": index_tick_age_ms,
            "quotes": {
                "yes_bid_dollars": state.yes_bid_dollars,
                "yes_ask_dollars": state.yes_ask_dollars,
                "no_ask_dollars": 1.0 - state.yes_bid_dollars if state.yes_bid_dollars is not None else None,
                "yes_bid_size": state.yes_bid_size,
                "yes_ask_size": state.yes_ask_size,
                "ts_ms": state.quote_ts_ms,
                "received_at_ms": state.quote_received_at_ms,
                "age_ms": quote_age_ms,
            },
            "live": {
                "model_p_yes": signal.model_p_yes,
                "market_p_yes": signal.market_p_yes,
                "edge": signal.edge,
                "raw_recommendation": signal.recommendation,
                "recommendation": decision_recommendation,
                "confidence": max(signal.model_p_yes, 1 - signal.model_p_yes),
                "confirmation": asdict(confirmation),
            },
            "parameters": parameters,
            "quality_flags": quality_flags,
            "external_prices": external or {
                "source_count": 0,
                "max_age_ms": None,
                "price": None,
                "price_dispersion": None,
                "sources": [],
            },
        }

    def _quality_flags(
        self, state: MarketState, features: Features, ticks: list[tuple[int, float]], now_ms: int
    ) -> list[str]:
        min_history_sec = getattr(self.settings, "min_index_history_sec", 240.0)
        min_history_ticks = getattr(self.settings, "min_index_history_ticks", 120)
        max_input_age_ms = getattr(self.settings, "max_input_age_ms", 5_000)
        min_quote_size = getattr(self.settings, "min_quote_size", 0.0)
        flags: list[str] = []
        if len(ticks) < min_history_ticks or features.history_span_sec < min_history_sec:
            flags.append("short_index_history")
        index_tick_age_ms = now_ms - ticks[-1][0] if ticks else None
        if index_tick_age_ms is None or index_tick_age_ms > max_input_age_ms:
            flags.append("stale_index_tick")
        quote_age_ms = now_ms - state.quote_ts_ms if state.quote_ts_ms is not None else None
        if quote_age_ms is None or quote_age_ms > max_input_age_ms:
            flags.append("stale_quote")
        if state.yes_bid_dollars is None or state.yes_ask_dollars is None:
            flags.append("missing_quote")
        elif not 0 <= state.yes_bid_dollars <= state.yes_ask_dollars <= 1:
            flags.append("invalid_quote")
        if state.yes_bid_size < min_quote_size or state.yes_ask_size < min_quote_size:
            flags.append("insufficient_quote_size")
        return flags

    async def _run_llm_review(
        self,
        stage: str,
        ticker: str,
        state: MarketState,
        features: Features,
        signal: Signal,
        recommendation: str,
        quality_flags: list[str],
        ticks: list[tuple[int, float]],
        now_ms: int,
    ) -> dict[str, Any] | None:
        reviewer = getattr(self, "llm_reviewer", None)
        if reviewer is None or not reviewer.enabled or self.store.has_llm_review(ticker, stage):
            return None
        quote_age_ms = now_ms - state.quote_ts_ms if state.quote_ts_ms is not None else None
        index_tick_age_ms = now_ms - ticks[-1][0] if ticks else None
        review_call_up = signal.model_p_yes >= 0.5
        review_confirmation = check_confirmation(features, review_call_up)
        external_prices = aggregate_external_prices(
            self.store.recent_external_ticks(now_ms - 5_000), now_ms, max_age_ms=5_000
        ).get(state.index_id)
        try:
            review = await reviewer.review(
                stage=stage,
                ticker=ticker,
                index_id=state.index_id,
                seconds_to_expiry=features.seconds_to_expiry,
                features=features,
                model_p_yes=signal.model_p_yes,
                market_p_yes=signal.market_p_yes,
                recommendation=recommendation,
                quality_flags=quality_flags,
                quote_age_ms=quote_age_ms,
                index_tick_age_ms=index_tick_age_ms,
                yes_bid_dollars=state.yes_bid_dollars,
                yes_ask_dollars=state.yes_ask_dollars,
                yes_bid_size=state.yes_bid_size,
                yes_ask_size=state.yes_ask_size,
                fee_multiplier=getattr(self.settings, "fee_multiplier", 1.0),
                slippage_per_contract=getattr(self.settings, "slippage_per_contract", 0.0),
                confirmation_agree=review_confirmation.agree,
                confirmation_total=review_confirmation.total,
                external_prices=external_prices,
            )
            self.store.record_llm_review(
                ticker=ticker, stage=stage, ts_ms=now_ms,
                decision=review["decision"],
                confidence_adjustment=review["confidence_adjustment"],
                reason=review["reason"], latency_ms=review.get("latency_ms"),
                model=review.get("model"),
            )
            return review
        except Exception as exc:
            logger.warning("LLM %s review unavailable for %s: %s", stage, ticker, exc)
            self.store.record_llm_review(
                ticker=ticker, stage=stage, ts_ms=now_ms,
                decision="UNAVAILABLE", confidence_adjustment=0.0,
                reason="Local LLM review unavailable; mathematical model retained.",
                error=str(exc)[:240],
            )
            return None

    async def prediction_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.poll_interval_sec)
            now_ms = int(time.time() * 1000)
            for ticker, state in list(self.markets.items()):
                if state.yes_bid_dollars is None or state.yes_ask_dollars is None:
                    continue
                ticks = self.index_ticks.get(state.index_id, [])
                if len(ticks) < 2:
                    continue
                seconds_to_expiry = (state.close_ts_ms - now_ms) / 1000.0
                if seconds_to_expiry <= 0:
                    continue
                features = build_features(
                    ticks,
                    strike=state.strike,
                    seconds_to_expiry=seconds_to_expiry,
                    yes_bid_size=state.yes_bid_size,
                    yes_ask_size=state.yes_ask_size,
                    close_ts_ms=state.close_ts_ms,
                )
                quality_flags = self._quality_flags(state, features, ticks, now_ms)
                decision_window_open = seconds_to_expiry <= self.settings.decision_lead_sec
                if quality_flags and decision_window_open:
                    logger.warning(
                        "Decision window reached for %s with degraded inputs; locking NO_EDGE: %s",
                        ticker, ", ".join(quality_flags),
                    )
                signal = generate_signal(
                    ticker=ticker,
                    index_id=state.index_id,
                    features=features,
                    predictor=self.predictor,
                    yes_bid_dollars=state.yes_bid_dollars,
                    yes_ask_dollars=state.yes_ask_dollars,
                    edge_threshold=self.settings.edge_threshold,
                    fee_multiplier=getattr(self.settings, "fee_multiplier", 1.0),
                    slippage_per_contract=getattr(self.settings, "slippage_per_contract", 0.0),
                    market_blend_weight=getattr(self.settings, "market_blend_weight", 0.0),
                    calibrator=self.calibrator,
                )
                self.store.insert_signal(signal)
                for review_lead_sec, review_stage in (
                    (510, "review_8m30"),
                    (270, "review_4m30"),
                    (60, "review_1m"),
                ):
                    if seconds_to_expiry <= review_lead_sec and not self.store.has_llm_review(ticker, review_stage):
                        review_confirmation = check_confirmation(features, signal.model_p_yes >= 0.5)
                        await self._run_llm_review(
                            review_stage, ticker, state, features, signal,
                            signal.recommendation if review_confirmation.confirmed and not quality_flags else "NO_EDGE",
                            quality_flags, ticks, now_ms,
                        )
                if quality_flags and not decision_window_open:
                    logger.info("Abstaining from %s: %s", ticker, ", ".join(quality_flags))
                    continue
                # The shadow challenger is the logistic "stacking" model (falls back
                # to the raw rule-based model until it has enough fitted samples) -
                # this is the ongoing check of whether it's ready for promotion.
                if isinstance(self.predictor, SettlementAwarePredictor):
                    shadow_base = RegularizedSettlementPredictor(
                        momentum_weight=self.predictor.momentum_weight,
                        window_sec=self.predictor.window_sec,
                    )
                    shadow_predictor = LogisticSignalPredictor(shadow_base, self.logistic_model)
                    shadow_signal = generate_signal(
                        ticker=ticker, index_id=state.index_id, features=features,
                        predictor=shadow_predictor, yes_bid_dollars=state.yes_bid_dollars,
                        yes_ask_dollars=state.yes_ask_dollars,
                        edge_threshold=self.settings.edge_threshold,
                        fee_multiplier=getattr(self.settings, "fee_multiplier", 1.0),
                        slippage_per_contract=getattr(self.settings, "slippage_per_contract", 0.0),
                    )
                    shadow_confirmation = check_confirmation(features, shadow_signal.model_p_yes >= 0.5)
                    self.store.upsert_shadow_signal(
                        shadow_signal,
                        shadow_confirmation.agree,
                        shadow_confirmation.total,
                    )

                # Lock in the one-shot, actionable trade call the first time this
                # market crosses the decision lead time - this is what "dictates"
                # a trade, as distinct from the continuously-fluctuating live read.
                if (
                    seconds_to_expiry <= self.settings.decision_lead_sec
                    and not self.store.has_decision(ticker)
                ):
                    call_up = signal.model_p_yes >= 0.5
                    confirmation = check_confirmation(features, call_up)
                    # Independent momentum/book signals disagree with the model's
                    # direction: downgrade to NO_EDGE ("NO TRADE") instead of locking
                    # in a shaky directional call that will dictate a real trade.
                    decision_recommendation = (
                        signal.recommendation
                        if confirmation.confirmed and not quality_flags
                        else "NO_EDGE"
                    )
                    decision_confidence = max(signal.model_p_yes, 1 - signal.model_p_yes)
                    early_review = await self._run_llm_review(
                        "early", ticker, state, features, signal,
                        decision_recommendation, quality_flags, ticks, now_ms,
                    )
                    llm_decision = early_review.get("decision") if early_review else None
                    if llm_decision == "BLOCK":
                        decision_recommendation = "NO_EDGE"
                    elif llm_decision == "REDUCE_CONFIDENCE":
                        decision_confidence = max(
                            0.5,
                            decision_confidence + early_review["confidence_adjustment"],
                        )
                    # BUY_YES at 0.5-0.7 model confidence settled at ~48% (a losing
                    # bucket after fees) in calibration review; BUY_NO showed no such
                    # gap, so this floor is intentionally asymmetric.
                    if (
                        decision_recommendation == "BUY_YES"
                        and decision_confidence < self.settings.min_confidence_buy_yes
                    ):
                        decision_recommendation = "NO_EDGE"
                    llm_check = {
                        "name": "6:30 LLM risk review",
                        "agree": llm_decision == "ALLOW",
                        "value": llm_decision or "UNAVAILABLE",
                    }
                    confirmation_agree = confirmation.agree + int(llm_check["agree"])
                    confirmation_total = confirmation.total + 1
                    # Model's conviction in its own directional call (how far its
                    # probability sits from a coin flip) - not the edge vs. market,
                    # so this matches the "confident right now" figure shown elsewhere.
                    confirmation_detail = json.dumps(
                        [
                            {"name": c.name, "agree": c.agree, "value": c.value}
                            for c in confirmation.checks
                        ] + [llm_check]
                    )
                    decision_snapshot = self._build_decision_snapshot(
                        state, features, signal, decision_recommendation, confirmation, now_ms, ticks,
                    )
                    shadow_snapshot = None
                    try:
                        shadow_snapshot = self._build_shadow_snapshot(
                            state, features, signal, decision_recommendation, now_ms, ticks,
                        )
                    except Exception:
                        logger.exception("Shadow prediction failed for %s; keeping the live decision", ticker)
                    self.store.record_decision(
                        ticker=ticker,
                        ts_ms=now_ms,
                        seconds_to_expiry=seconds_to_expiry,
                        index_price=features.index_price,
                        strike=state.strike,
                        model_p_yes=signal.model_p_yes,
                        market_p_yes=signal.market_p_yes,
                        edge=signal.edge,
                        recommendation=decision_recommendation,
                        confidence=decision_confidence,
                        confirmation_agree=confirmation_agree,
                        confirmation_total=confirmation_total,
                        confirmation_detail=confirmation_detail,
                        shadow_snapshot=shadow_snapshot,
                        decision_snapshot=decision_snapshot,
                    )
                    logger.info(
                        "Decision locked for %s at T-%.0fs: %s (model=%.3f market=%.3f, confirmation=%d/%d)",
                        ticker, seconds_to_expiry, decision_recommendation, signal.model_p_yes,
                        signal.market_p_yes, confirmation_agree, confirmation_total,
                    )

                # Late review is displayed for context but never rewrites the
                # immutable T-6:30 decision used for performance measurement.
                if seconds_to_expiry <= 150 and not self.store.has_llm_review(ticker, "late"):
                    late_call_up = signal.model_p_yes >= 0.5
                    late_confirmation = check_confirmation(features, late_call_up)
                    asyncio.create_task(
                        self._run_llm_review(
                            "late", ticker, state, features, signal,
                            signal.recommendation
                            if late_confirmation.confirmed and not quality_flags
                            else "NO_EDGE",
                            quality_flags, ticks, now_ms,
                        )
                    )

    async def rediscovery_loop(self) -> None:
        # Initial discovery already happened in run(); wait before refreshing.
        while True:
            await asyncio.sleep(_REDISCOVERY_INTERVAL_SEC)
            try:
                await self.discover_and_subscribe()
            except Exception:
                logger.exception("Market rediscovery failed")

    async def outcome_polling_loop(self) -> None:
        """Fetch settlement results for closed markets so calibration can be tracked live."""
        while True:
            await asyncio.sleep(_OUTCOME_POLL_INTERVAL_SEC)
            try:
                await self._poll_pending_outcomes()
            except Exception:
                logger.exception("Outcome polling failed")

    async def calibration_loop(self) -> None:
        """Periodically refit the isotonic calibrator from settled decisions.

        This is the "online learning" piece: no gradient descent, since the
        underlying predictors are closed-form/rule-based, but the probability
        they output is continuously recalibrated against realized outcomes
        (isotonic regression / PAVA) as the bot's track record grows.
        """
        while True:
            await asyncio.sleep(self.settings.calibration_refit_interval_sec)
            try:
                pairs = self.store.calibration_pairs(limit=self.settings.calibration_window)
                self.calibrator.fit(pairs)
                if self.calibrator.is_fitted:
                    logger.info("Recalibrated model probabilities on %d settled decisions", self.calibrator.fitted_n)
            except Exception:
                logger.exception("Calibration refit failed")

    async def logistic_training_loop(self) -> None:
        """Periodically refit the logistic "stacking" shadow model.

        This is real gradient descent (see `prediction/logistic.py`), unlike
        the isotonic calibrator. It stays a shadow challenger - never affects
        live decisions - until it's promoted the same way v3 was: prove it
        first via `shadow-comparison`.
        """
        while True:
            await asyncio.sleep(self.settings.logistic_refit_interval_sec)
            try:
                rows = self.store.decision_feature_outcome_pairs(limit=self.settings.logistic_training_window)
                fit_logistic_model(self.logistic_model, rows, RegularizedSettlementPredictor())
                if self.logistic_model.is_fitted:
                    logger.info(
                        "Refit logistic shadow model on %d settled decisions", self.logistic_model.fitted_n
                    )
            except Exception:
                logger.exception("Logistic shadow model refit failed")

    async def _poll_pending_outcomes(self) -> None:
        pending = self.store.markets_pending_outcome(max_age_ms=_OUTCOME_MAX_AGE_MS)
        for ticker in pending:
            try:
                data = await self.rest.get_market(ticker)
            except Exception:
                logger.warning("Failed to fetch settlement result for %s", ticker)
                continue
            result = data.get("market", {}).get("result", "")
            if result in ("yes", "no"):
                self.store.record_outcome(ticker, result)
                logger.info("Recorded settlement outcome for %s: %s", ticker, result)

    async def whale_polling_loop(self) -> None:
        """Surface unusually large individual fills ("big bets") on active markets.

        Kalshi's public trade feed does not expose trader identity - this only
        flags large single fills, not specific people or accounts.
        """
        while True:
            for ticker in list(self.markets):
                try:
                    await self._poll_whale_trades(ticker)
                except Exception:
                    logger.exception("Whale-trade polling failed for %s", ticker)
            await asyncio.sleep(self.settings.whale_poll_interval_sec)

    async def _poll_whale_trades(self, ticker: str) -> None:
        since_ts_ms = self.store.latest_whale_trade_ts(ticker)
        min_ts = int(since_ts_ms / 1000) + 1 if since_ts_ms is not None else None
        data = await self.rest.get_trades(ticker=ticker, min_ts=min_ts, limit=200)
        for trade in data.get("trades", []):
            ts_ms = _parse_ts_ms(trade.get("created_time"))
            side = trade.get("taker_side") or trade.get("taker_outcome_side")
            if ts_ms is None or side not in ("yes", "no"):
                continue

            count_raw = (
                trade.get("count_fp")
                if trade.get("count_fp") is not None
                else trade.get("count", 0)
            )
            try:
                count = float(count_raw or 0)
            except (ValueError, TypeError):
                count = 0.0

            yes_dollars = trade.get("yes_price_dollars")
            no_dollars = trade.get("no_price_dollars")
            try:
                if side == "yes":
                    if yes_dollars is not None:
                        price_cents = float(yes_dollars) * 100.0
                    else:
                        price_cents = float(trade.get("yes_price", 0))
                else:
                    if no_dollars is not None:
                        price_cents = float(no_dollars) * 100.0
                    else:
                        price_cents = float(trade.get("no_price", 0))
            except (ValueError, TypeError):
                price_cents = 0.0

            notional_usd = count * price_cents / 100.0
            if notional_usd < self.settings.whale_min_usd:
                continue

            self.store.insert_whale_trade(
                trade_id=str(trade["trade_id"]),
                ticker=ticker,
                ts_ms=ts_ms,
                side=side,
                count=count,
                price_cents=price_cents,
                notional_usd=notional_usd,
            )

    async def run(self) -> None:
        await self.discover_and_subscribe()
        try:
            self.calibrator.fit(self.store.calibration_pairs(limit=self.settings.calibration_window))
        except Exception:
            logger.exception("Initial calibration fit failed; predictions stay uncalibrated for now")
        try:
            fit_logistic_model(
                self.logistic_model,
                self.store.decision_feature_outcome_pairs(limit=self.settings.logistic_training_window),
                RegularizedSettlementPredictor(),
            )
        except Exception:
            logger.exception("Initial logistic shadow model fit failed; it stays a passthrough for now")
        external_tick_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=_TICK_QUEUE_MAXSIZE)

        uv_config = uvicorn.Config(
            create_app(
                self.store,
                closed_grace_sec=self.settings.closed_grace_sec,
                decision_lead_sec=self.settings.decision_lead_sec,
                broadcaster=self.broadcaster,
            ),
            host=self.settings.dashboard_host,
            port=self.settings.dashboard_port,
            log_level="warning",
        )
        server = uvicorn.Server(uv_config)

        await asyncio.gather(
            self.prediction_loop(),
            self.rediscovery_loop(),
            self.outcome_polling_loop(),
            self.calibration_loop(),
            self.logistic_training_loop(),
            self.whale_polling_loop(),
            collect_coinbase(external_tick_queue, asyncio.Event()),
            collect_kraken(external_tick_queue, asyncio.Event()),
            persist_external_ticks(self.store, external_tick_queue),
            server.serve(),
        )


async def main() -> None:
    settings = Settings.load()
    app = BotApp(settings)
    try:
        await app.run()
    finally:
        await app.rest.aclose()
        app.store.close()


if __name__ == "__main__":
    asyncio.run(main())
