"""Central configuration loaded from environment variables / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal

from dotenv import load_dotenv

from .trading import TradingPolicy, dollars

load_dotenv()

_REST_BASES = {
    "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
    "prod": "https://external-api.kalshi.com/trade-api/v2",
}
_WS_BASES = {
    "demo": "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
    "prod": "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
}


def _split_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    env: str
    key_id: str
    private_key_path: str
    rest_base: str
    ws_url: str
    index_ids: list[str]
    coin_ticks: list[str]
    poll_interval_sec: float
    edge_threshold: float
    database_url: str
    dashboard_host: str
    dashboard_port: int
    predictor_version: str
    closed_grace_sec: int
    decision_lead_sec: int
    whale_min_usd: float
    whale_poll_interval_sec: float
    min_index_history_sec: float
    min_index_history_ticks: int
    max_input_age_ms: int
    min_quote_size: float
    fee_multiplier: float
    slippage_per_contract: float
    llm_base_url: str
    llm_model: str
    llm_timeout_sec: float
    market_blend_weight: float
    min_confidence_buy_yes: float
    calibration_min_samples: int
    calibration_refit_interval_sec: float
    calibration_window: int
    logistic_min_samples: int
    logistic_refit_interval_sec: float
    logistic_training_window: int
    trading_policy: TradingPolicy = field(default_factory=TradingPolicy)
    order_execution_enabled: bool = False
    daily_loss_limit: Decimal = Decimal("0")
    decision_model: str = "fair-value"
    market_recal_min_samples: int = 300
    market_recal_window: int = 5000
    market_recal_edge_threshold: float = 0.0
    confirmation_gate: bool = False
    discord_trade_webhook_url: str = ""
    discord_settlement_webhook_url: str = ""
    discord_notify_paper: bool = True

    def load() -> "Settings":
        env = os.environ.get("KALSHI_ENV", "demo").strip().lower()
        if env not in _REST_BASES:
            raise ValueError(f"KALSHI_ENV must be 'demo' or 'prod', got {env!r}")
        return Settings(
            env=env,
            key_id=os.environ.get("KALSHI_KEY_ID", ""),
            private_key_path=os.environ.get("KALSHI_PRIVATE_KEY_PATH", ""),
            rest_base=_REST_BASES[env],
            ws_url=_WS_BASES[env],
            index_ids=_split_csv(os.environ.get("KALSHI_INDEX_IDS", "BRTI,SOLUSD_RTI")),
            coin_ticks=_split_csv(os.environ.get("KALSHI_COIN_TICKS", "BTC,SOL")),
            poll_interval_sec=float(os.environ.get("KALSHI_POLL_INTERVAL_SEC", "2")),
            edge_threshold=float(os.environ.get("KALSHI_EDGE_THRESHOLD", "0.05")),
            # A plain path (e.g. ./data/kalshi_bot.db) uses SQLite; a postgresql:// URL uses Postgres.
            database_url=os.environ.get("DATABASE_URL", "./data/kalshi_bot.db"),
            dashboard_host=os.environ.get("KALSHI_DASHBOARD_HOST", "127.0.0.1"),
            dashboard_port=int(os.environ.get("KALSHI_DASHBOARD_PORT", "8000")),
            # v3 (regularized-settlement) beat live v2 on both accuracy and Brier
            # in shadow testing across ~950 settled markets; promoted as the default.
            predictor_version=os.environ.get("KALSHI_PREDICTOR_VERSION", "v3").strip().lower(),
            closed_grace_sec=int(os.environ.get("KALSHI_CLOSED_GRACE_SEC", "10")),
            decision_lead_sec=int(os.environ.get("KALSHI_DECISION_LEAD_SEC", "390")),
            # Fills at or above this dollar size are surfaced as a "big bet".
            whale_min_usd=float(os.environ.get("KALSHI_WHALE_MIN_USD", "100")),
            whale_poll_interval_sec=float(os.environ.get("KALSHI_WHALE_POLL_INTERVAL_SEC", "20")),
            min_index_history_sec=float(os.environ.get("KALSHI_MIN_INDEX_HISTORY_SEC", "240")),
            min_index_history_ticks=int(os.environ.get("KALSHI_MIN_INDEX_HISTORY_TICKS", "120")),
            max_input_age_ms=int(os.environ.get("KALSHI_MAX_INPUT_AGE_MS", "5000")),
            min_quote_size=float(os.environ.get("KALSHI_MIN_QUOTE_SIZE", "1")),
            fee_multiplier=float(os.environ.get("KALSHI_FEE_MULTIPLIER", "1")),
            slippage_per_contract=float(os.environ.get("KALSHI_SLIPPAGE_PER_CONTRACT", "0")),
            llm_base_url=os.environ.get("KALSHI_LLM_BASE_URL", "http://192.168.1.229:8080").strip(),
            llm_model=os.environ.get("KALSHI_LLM_MODEL", "").strip(),
            llm_timeout_sec=float(os.environ.get("KALSHI_LLM_TIMEOUT_SEC", "15")),
            # Grid-searched offline against ~244 recent live decisions (see
            # backtest.runner --grid-search-blend): combined Brier improved
            # monotonically from 0.129 at weight=0.0 to 0.121 at weight=1.0, but
            # weight=1.0 means model_p == market_p exactly, i.e. zero purchase
            # edge ever, i.e. the bot never trades. 0.8 keeps most of that Brier
            # gain while leaving the model room to actually generate edge.
            market_blend_weight=float(os.environ.get("KALSHI_MARKET_BLEND_WEIGHT", "0.8")),
            # BUY_YES at 0.5-0.7 model confidence settled at ~48% (a losing bucket
            # after fees); BUY_NO had no such gap, so the floor is BUY_YES-only.
            min_confidence_buy_yes=float(os.environ.get("KALSHI_MIN_CONFIDENCE_BUY_YES", "0.7")),
            # Isotonic recalibration (PAVA) refit periodically from the settled
            # decision history; a cold-start below calibration_min_samples leaves
            # predictions unchanged (identity mapping).
            calibration_min_samples=int(os.environ.get("KALSHI_CALIBRATION_MIN_SAMPLES", "200")),
            calibration_refit_interval_sec=float(
                os.environ.get("KALSHI_CALIBRATION_REFIT_INTERVAL_SEC", "1800")
            ),
            calibration_window=int(os.environ.get("KALSHI_CALIBRATION_WINDOW", "2000")),
            # Logistic "stacking" shadow model: learns to combine the rule-based
            # model's own output with momentum/imbalance/vol/window-progress via
            # gradient descent, refit periodically from settled decisions.
            logistic_min_samples=int(os.environ.get("KALSHI_LOGISTIC_MIN_SAMPLES", "300")),
            logistic_refit_interval_sec=float(
                os.environ.get("KALSHI_LOGISTIC_REFIT_INTERVAL_SEC", "1800")
            ),
            logistic_training_window=int(os.environ.get("KALSHI_LOGISTIC_TRAINING_WINDOW", "5000")),
            trading_policy=TradingPolicy(
                budget=dollars(os.environ.get("KALSHI_TRADE_BUDGET_USD", "1.00")),
                # 0 disables the exit; with both 0 positions are held to settlement.
                take_profit=dollars(os.environ.get("KALSHI_TAKE_PROFIT_USD", "0")),
                stop_loss=dollars(os.environ.get("KALSHI_STOP_LOSS_USD", "0")),
            ),
            order_execution_enabled=os.environ.get("KALSHI_ORDER_EXECUTION_ENABLED", "false").strip().lower() == "true",
            # Pause new bets for the rest of the UTC day once closed bets lose this much (0 = off).
            daily_loss_limit=dollars(os.environ.get("KALSHI_DAILY_LOSS_LIMIT_USD", "0")),
            # "fair-value" (default): coin price vs strike blended with the market price
            # (prediction.fair_value); falls back to "market-recal" until enough recent
            # index history exists. "market-recal": recalibrated market price only.
            # "legacy": blended model + isotonic.
            decision_model=os.environ.get("KALSHI_DECISION_MODEL", "fair-value").strip().lower(),
            market_recal_min_samples=int(os.environ.get("KALSHI_MARKET_RECAL_MIN_SAMPLES", "300")),
            market_recal_window=int(os.environ.get("KALSHI_MARKET_RECAL_WINDOW", "5000")),
            market_recal_edge_threshold=float(os.environ.get("KALSHI_MARKET_RECAL_EDGE_THRESHOLD", "0")),
            # Momentum/book confirmation lowered after-fee PnL in walk-forward tests.
            confirmation_gate=os.environ.get("KALSHI_CONFIRMATION_GATE", "false").strip().lower() == "true",
            # Webhook URLs are secrets; in production Jenkins injects them from its credential store.
            discord_trade_webhook_url=os.environ.get("DISCORD_TRADE_WEBHOOK_URL", "").strip(),
            discord_settlement_webhook_url=os.environ.get("DISCORD_SETTLEMENT_WEBHOOK_URL", "").strip(),
            # Paper trades post to the same channels, labelled PAPER; set false once live trading is on.
            discord_notify_paper=os.environ.get("DISCORD_NOTIFY_PAPER", "true").strip().lower() != "false",
        )
