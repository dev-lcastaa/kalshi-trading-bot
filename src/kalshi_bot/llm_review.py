"""Small, fail-open LLM risk review for locked market decisions."""
from __future__ import annotations

import json
import math
import time
from typing import Any

import httpx

from .features.engine import Features
from .signals.generator import kalshi_taker_fee_per_contract

SYSTEM_PROMPT = """You are a conservative risk-review assistant for a 15-minute Kalshi crypto prediction system.

You are not the primary prediction model. Do not invent a new probability, predict the market direction yourself, or place trades.
Review only the structured data supplied by the user. Do not use outside information. Missing, stale, or unclear values are reasons for caution.

Return exactly one JSON object and no markdown or other text:
{"decision":"ALLOW|BLOCK|REDUCE_CONFIDENCE","confidence_adjustment":number,"reason":"short explanation"}

Rules:
- ALLOW means the supplied inputs are coherent and show no clear data-quality or risk problem.
- BLOCK means there is a serious data-quality problem or a clear, severe inconsistency. Do not block only because the model and market probabilities differ.
- REDUCE_CONFIDENCE means the signal may be usable, but evidence is weak, conflicting, or unusually uncertain.
- Do not recommend UP or DOWN and do not create a new probability.
- Prioritize stale inputs, invalid or wide quotes, insufficient depth, conflicting confirmation votes, and a non-positive purchase edge after costs.
- Use distance_sigma and expected_move_dollars_1sigma to judge whether the price is meaningfully separated from the strike; do not treat raw dollar distance alone as meaningful.
- Treat market probability and order-book imbalance as context, never as proof of the settlement outcome.
- The review is informational. It must not assume it can change the mathematical recommendation.
- confidence_adjustment must be between -0.25 and 0. Use 0 for ALLOW, -0.25 for BLOCK, and a value from -0.10 to -0.25 for REDUCE_CONFIDENCE.
- Use only the supplied values. Keep the reason under 160 characters.
"""

_DECISIONS = {"ALLOW", "BLOCK", "REDUCE_CONFIDENCE"}


class LlmReviewError(Exception):
    """The local LLM was unavailable or returned an invalid review."""


class LlmReviewer:
    def __init__(self, base_url: str, model: str = "", timeout_sec: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_sec = timeout_sec

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    async def review(
        self,
        *,
        stage: str,
        ticker: str,
        index_id: str,
        seconds_to_expiry: float,
        features: Features,
        model_p_yes: float,
        market_p_yes: float,
        recommendation: str,
        quality_flags: list[str],
        quote_age_ms: int | None,
        index_tick_age_ms: int | None,
        yes_bid_dollars: float | None = None,
        yes_ask_dollars: float | None = None,
        yes_bid_size: float | None = None,
        yes_ask_size: float | None = None,
        fee_multiplier: float = 1.0,
        slippage_per_contract: float = 0.0,
        confirmation_agree: int | None = None,
        confirmation_total: int | None = None,
        external_prices: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if stage not in {"early", "late"}:
            raise ValueError(f"unsupported LLM review stage: {stage}")
        if not self.enabled:
            raise LlmReviewError("LLM review is disabled")

        time_sigma = features.realized_vol_per_sqrt_sec * (max(seconds_to_expiry, 0.0) ** 0.5)
        log_distance = math.log(features.index_price / features.strike) if features.index_price > 0 and features.strike > 0 else None
        yes_ask = yes_ask_dollars
        no_ask = 1.0 - yes_bid_dollars if yes_bid_dollars is not None else None
        yes_fee = kalshi_taker_fee_per_contract(yes_ask, fee_multiplier) if yes_ask is not None else None
        no_fee = kalshi_taker_fee_per_contract(no_ask, fee_multiplier) if no_ask is not None else None
        yes_purchase_edge = model_p_yes - yes_ask - yes_fee - slippage_per_contract if yes_ask is not None else None
        no_purchase_edge = (1.0 - model_p_yes) - no_ask - no_fee - slippage_per_contract if no_ask is not None else None
        payload = {
            "stage": stage,
            "ticker": ticker,
            "asset_index": index_id,
            "minutes_to_expiry": round(seconds_to_expiry / 60.0, 3),
            "model_probability_yes": round(model_p_yes, 6),
            "market_probability_yes": round(market_p_yes, 6),
            "model_recommendation": recommendation,
            "index_price": round(features.index_price, 8),
            "strike": round(features.strike, 8),
            "distance_from_strike": round(features.index_price - features.strike, 8),
            "distance_sigma": round(log_distance / time_sigma, 6) if log_distance is not None and time_sigma > 1e-12 else None,
            "expected_move_dollars_1sigma": round(features.index_price * time_sigma, 8),
            "yes_bid_dollars": yes_bid_dollars,
            "yes_ask_dollars": yes_ask,
            "no_ask_dollars": no_ask,
            "spread_dollars": round(yes_ask - yes_bid_dollars, 6) if yes_ask is not None and yes_bid_dollars is not None else None,
            "yes_bid_size": yes_bid_size,
            "yes_ask_size": yes_ask_size,
            "yes_purchase_edge_after_costs": yes_purchase_edge,
            "no_purchase_edge_after_costs": no_purchase_edge,
            "fee_multiplier": fee_multiplier,
            "slippage_per_contract": slippage_per_contract,
            "realized_vol_per_sqrt_sec": round(features.realized_vol_per_sqrt_sec, 10),
            "momentum_ols_per_sec": round(features.momentum_ols_per_sec, 10),
            "momentum_short_per_sec": round(features.momentum_short_per_sec, 10),
            "book_imbalance": round(features.book_imbalance, 6) if features.book_imbalance is not None else None,
            "price_vs_strike_ratio": round(features.index_price / features.strike, 8) if features.strike else None,
            "quality_flags": quality_flags,
            "quote_age_ms": quote_age_ms,
            "index_tick_age_ms": index_tick_age_ms,
            "confirmation_agree": confirmation_agree,
            "confirmation_total": confirmation_total,
            "external_prices": external_prices or {},
        }
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
            model = self.model
            if not model:
                response = await client.get(f"{self.base_url}/v1/models")
                response.raise_for_status()
                models = response.json().get("data", [])
                model = next((item.get("id") for item in models if item.get("id")), "llama.cpp")
            response = await client.post(
                f"{self.base_url}/v1/chat/completions",
                json={
                    "model": model,
                    "temperature": 0,
                    "max_tokens": 120,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps(payload, separators=(",", ":"))},
                    ],
                },
            )
            response.raise_for_status()
            body = response.json()
        content = body.get("choices", [{}])[0].get("message", {}).get("content")
        try:
            result = json.loads(content.strip())
        except (AttributeError, json.JSONDecodeError) as exc:
            raise LlmReviewError("LLM response was not a JSON object") from exc
        decision = result.get("decision")
        adjustment = result.get("confidence_adjustment")
        reason = result.get("reason")
        if (
            not isinstance(result, dict)
            or set(result) != {"decision", "confidence_adjustment", "reason"}
            or decision not in _DECISIONS
            or not isinstance(adjustment, (int, float))
            or not -0.25 <= float(adjustment) <= 0
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise LlmReviewError("LLM response failed the strict schema")
        return {
            "stage": stage,
            "decision": decision,
            "confidence_adjustment": float(adjustment),
            "reason": reason.strip()[:160],
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "model": model,
        }