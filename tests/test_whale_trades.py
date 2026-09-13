from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kalshi_bot.data.store import Store
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
