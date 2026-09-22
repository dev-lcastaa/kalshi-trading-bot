from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from kalshi_bot.data.store import Store
from kalshi_bot.dashboard.server import create_app
from kalshi_bot.main import BotApp


@pytest.mark.asyncio
async def test_poll_whale_trades_parses_modern_kalshi_schema(tmp_path):
    db_path = tmp_path / "test_whales.db"
    store = Store(str(db_path))

    mock_settings = MagicMock()
    mock_settings.whale_min_usd = 50.0

    mock_rest = MagicMock()
    mock_rest.get_trades = AsyncMock(
        return_value={
            "trades": [
                {
                    "trade_id": "W1",
                    "ticker": "KXBTC15M-TEST",
                    "created_time": "2026-09-12T20:00:00.000000Z",
                    "taker_outcome_side": "yes",
                    "count_fp": "200.00",
                    "yes_price_dollars": "0.6000",
                },
                {
                    "trade_id": "W2",
                    "ticker": "KXBTC15M-TEST",
                    "created_time": "2026-09-12T20:01:00.000000Z",
                    "taker_side": "no",
                    "count_fp": "100.00",
                    "no_price_dollars": "0.3000",  # 100 * 30c = $30 (< $50 min)
                },
            ]
        }
    )

    app = BotApp.__new__(BotApp)
    app.store = store
    app.settings = mock_settings
    app.rest = mock_rest

    await app._poll_whale_trades("KXBTC15M-TEST")

    trades = store.recent_whale_trades("KXBTC15M-TEST")
    assert len(trades) == 1
    assert trades[0]["trade_id"] == "W1"
    assert trades[0]["side"] == "yes"
    assert trades[0]["count"] == 200.0
    assert trades[0]["price_cents"] == 60.0
    assert trades[0]["notional_usd"] == 120.0

    store.close()


def test_recent_whale_net_flow_usd_nets_yes_and_no_and_respects_cutoff(tmp_path):
    store = Store(str(tmp_path / "net_flow.db"))
    store.insert_whale_trade("YES-OLD", "BTC", 500, "yes", 10, 50, 300.0)  # before cutoff, excluded
    store.insert_whale_trade("YES-NEW", "BTC", 1_500, "yes", 10, 50, 300.0)
    store.insert_whale_trade("NO-NEW", "BTC", 1_800, "no", 10, 50, 100.0)
    store.insert_whale_trade("OTHER-TICKER", "SOL", 2_000, "yes", 10, 50, 999.0)

    net_flow = store.recent_whale_net_flow_usd("BTC", since_ms=1_000)

    assert net_flow == pytest.approx(300.0 - 100.0)
    store.close()


def test_whale_endpoint_filters_before_applying_limit(tmp_path):
    store = Store(str(tmp_path / "filtered_whales.db"))
    for trade_id, ts_ms, notional in (
        ("SMALL-NEW", 3_000, 200.0),
        ("BIG-MID", 2_000, 500.0),
        ("BIG-OLD", 1_000, 700.0),
    ):
        store.insert_whale_trade(trade_id, "BTC", ts_ms, "yes", 10, 50, notional)
    client = TestClient(create_app(store))

    response = client.get("/api/whale-trades", params={"ticker": "BTC", "min_usd": 500, "limit": 1})

    assert response.status_code == 200
    assert [trade["trade_id"] for trade in response.json()] == ["BIG-MID"]
    assert client.get("/api/whale-trades", params={"ticker": "BTC", "min_usd": -1}).status_code == 422
    assert client.get("/api/whale-trades", params={"ticker": "BTC", "limit": 0}).status_code == 422
    store.close()
