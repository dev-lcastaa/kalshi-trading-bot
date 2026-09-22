"""FastAPI dashboard serving live signals from the local store."""
from __future__ import annotations

import time
import threading
from typing import Callable, TypeVar

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect

from ..data.store import Store
from .. import __version__
from .broadcaster import Broadcaster

_T = TypeVar("_T")


class _TtlCache:
    def __init__(self) -> None:
        self._values: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, ttl_sec: float, factory: Callable[[], _T]) -> _T:
        now = time.monotonic()
        with self._lock:
            cached = self._values.get(key)
            if cached is not None and now - cached[0] < ttl_sec:
                return cached[1]  # type: ignore[return-value]
            value = factory()
            self._values[key] = (now, value)
            return value


def create_app(
    store: Store,
    closed_grace_sec: int = 300,
    decision_lead_sec: int = 390,
    broadcaster: Broadcaster | None = None,
) -> FastAPI:
    app = FastAPI(title="Kalshi 15-Min Crypto Signals")
    broadcaster = broadcaster if broadcaster is not None else Broadcaster()
    telemetry_cache = _TtlCache()

    @app.get("/api/version")
    def get_version() -> dict:
        """Identifies which build is actually running behind a given deployment."""
        return {"version": __version__}

    @app.get("/api/config")
    def get_config() -> dict:
        return {"decision_lead_sec": decision_lead_sec}

    @app.get("/api/signals")
    def get_signals(limit: int = 50) -> list[dict]:
        return store.latest_signals(limit=limit)

    @app.get("/api/latest")
    def get_latest() -> list[dict]:
        return store.latest_signal_per_ticker()

    @app.get("/api/active")
    def get_active() -> list[dict]:
        """Active markets, plus markets closed within the last `closed_grace_sec`."""
        return store.dashboard_markets(grace_period_sec=closed_grace_sec)

    @app.get("/api/closed")
    def get_closed(
        limit: int = Query(default=200, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict]:
        return store.closed_markets_history(limit=limit, offset=offset)

    @app.get("/api/price-history")
    def get_price_history(index_id: str, minutes: int = 10) -> list[dict]:
        since_ms = int(time.time() * 1000) - minutes * 60_000
        ticks = store.recent_index_ticks(index_id, since_ms)
        return [{"ts_ms": ts_ms, "value": value} for ts_ms, value in ticks]

    @app.get("/api/live-prices")
    def get_live_prices() -> dict:
        """Latest raw index tick per coin, updated far more often than signals are recomputed."""
        return store.latest_index_prices()

    @app.get("/api/external-status")
    def get_external_status() -> dict:
        """Health and latest snapshot data for the external shadow feed."""
        return store.external_feed_status()

    @app.get("/api/calibration-summary")
    def get_calibration_summary(limit: int = Query(default=200, ge=1, le=10000)) -> dict:
        def build_summary() -> dict:
            return {
                "overall": store.calibration_stats(limit=limit),
                "BRTI": store.calibration_stats(limit=limit, index_id="BRTI"),
                "SOLUSD_RTI": store.calibration_stats(limit=limit, index_id="SOLUSD_RTI"),
            }

        return telemetry_cache.get(f"calibration-summary:{limit}", 30.0, build_summary)

    @app.get("/api/calibration")
    def get_calibration(
        limit: int = Query(default=200, ge=1, le=10000), index_id: str | None = None,
    ) -> dict:
        return store.calibration_stats(limit=limit, index_id=index_id)

    @app.get("/api/shadow-decisions")
    def get_shadow_decisions(
        limit: int = Query(default=200, ge=1, le=10000), index_id: str | None = None,
    ) -> list[dict]:
        return store.shadow_decisions(limit=limit, index_id=index_id)

    @app.get("/api/decision-snapshots")
    def get_decision_snapshots(
        limit: int = Query(default=200, ge=1, le=10000), index_id: str | None = None,
    ) -> list[dict]:
        """Live-decision snapshots for offline replay/analysis, e.g.
        `backtest.runner`'s retrospective parameter grid search."""
        return store.decision_snapshots(limit=limit, index_id=index_id)

    @app.get("/api/shadow-active")
    def get_shadow_active() -> list[dict]:
        return store.shadow_dashboard_markets(grace_period_sec=closed_grace_sec)

    @app.get("/api/shadow-comparison")
    def get_shadow_comparison(
        limit: int = Query(default=10000, ge=1, le=10000), index_id: str | None = None,
    ) -> dict:
        return store.shadow_comparison(limit=limit, index_id=index_id)

    @app.get("/api/whale-trades")
    def get_whale_trades(
        ticker: str,
        limit: int = Query(default=20, ge=1, le=200),
        min_usd: float = Query(default=0, ge=0, le=1_000_000_000),
    ) -> list[dict]:
        """Recent large fills for one market. Anonymous - Kalshi's public trade
        feed does not expose who made a trade, only the fill's side/size/price."""
        return store.recent_whale_trades(ticker, limit=limit, min_usd=min_usd)

    @app.websocket("/ws/live")
    async def ws_live(websocket: WebSocket) -> None:
        """Pushes live index ticks (and other events) as soon as the bot receives them."""
        await websocket.accept()
        await broadcaster.register(websocket)
        try:
            while True:
                await websocket.receive_text()  # unused; just detects client disconnect
        except WebSocketDisconnect:
            pass
        finally:
            await broadcaster.unregister(websocket)

    return app
