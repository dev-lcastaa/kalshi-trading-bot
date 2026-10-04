from kalshi_bot.data.store import Store
from kalshi_bot.auto_trader import AutoTrader
from kalshi_bot.dashboard.server import create_app
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock
import asyncio


def test_dashboard_modes_have_independent_settings_and_controls(tmp_path):
    store = Store(str(tmp_path / "api.db"))
    live = AutoTrader(store, AsyncMock(), execution_allowed=True, account_identity="test-account")
    paper = AutoTrader(store, AsyncMock(), execution_allowed=True, mode="paper",
                       account_identity="paper", worker_id=live.worker_id)
    asyncio.run(live.cycle())
    asyncio.run(paper.cycle())
    client = TestClient(create_app(store, traders={"live": live, "paper": paper}))
    rule = {"name": "Favorites", "enabled": True, "coin": "BTC", "side": "model", "min_price": "0.60",
            "max_price": "0.90", "min_confidence": "0.65", "min_edge": "0.01", "min_seconds_left": 300,
            "max_seconds_left": 420, "budget": "12.50", "take_profit": "0.25", "stop_loss": "0.05"}
    settings = {"rules": [rule]}
    body = client.get("/api/trading").json()
    assert body["live"]["enabled"] is False
    assert body["paper"]["mode"] == "paper"
    assert body["paper"]["watch"] == []
    response = client.put("/api/trading/settings", json={"mode": "paper", **settings})
    assert response.status_code == 200
    assert response.json()["paper"]["settings"] == settings
    assert response.json()["live"]["settings"]["rules"][0]["budget"] == "1.00"
    too_big = {"rules": [{**rule, "budget": "25.01"}]}
    assert client.put("/api/trading/settings", json={"mode": "live", **too_big}).status_code == 422
    bad_range = {"rules": [{**rule, "min_price": "0.95", "max_price": "0.60"}]}
    assert client.put("/api/trading/settings", json={"mode": "live", **bad_range}).status_code == 422
    assert client.put("/api/trading/settings", json={"mode": "live", "rules": []}).status_code == 422
    assert client.put("/api/trading/settings", json=settings).status_code == 422
    assert client.post("/api/trading/control", json={"mode": "paper", "enabled": True, "settings": settings}).status_code == 409
    stale = {"rules": [{**rule, "take_profit": "0.50"}]}
    assert client.post("/api/trading/control", json={"mode": "paper", "enabled": True, "confirm": True, "settings": stale}).status_code == 409
    assert not paper.enabled
    response = client.post("/api/trading/control", json={"mode": "paper", "enabled": True, "confirm": True, "settings": settings})
    assert response.status_code == 200
    assert response.json()["paper"]["enabled"] is True
    assert response.json()["live"]["enabled"] is False
    assert client.put("/api/trading/settings", json={"mode": "paper", **settings}).status_code == 422
    response = client.post("/api/trading/control", json={"mode": "paper", "enabled": False})
    assert response.status_code == 200
    assert response.json()["paper"]["enabled"] is False
    assert all(event["mode"] == "paper" for event in response.json()["paper"]["events"])
    assert all(event["mode"] == "live" for event in response.json()["live"]["events"])
    store.close()


def test_dashboard_default_traders_cannot_enable(tmp_path):
    store = Store(str(tmp_path / "unconfigured.db"))
    client = TestClient(create_app(store))
    body = client.get("/api/trading").json()
    assert set(body) == {"live", "paper"}
    assert not body["live"]["enabled"]
    assert body["live"]["blockers"]
    defaults = body["live"]["settings"]
    assert client.post("/api/trading/control", json={"mode": "live", "enabled": True, "confirm": True, "settings": defaults}).status_code == 409
    assert client.post("/api/trading/control", json={"mode": "live", "enabled": True, "confirm": True}).status_code == 422
    store.close()


def test_trading_settings_and_activity_survive_restart(tmp_path):
    path = str(tmp_path / "trading.db")
    store = Store(path)
    settings = {"budget": "1.25", "take_profit": "0.30", "stop_loss": "0.08"}
    store.save_trading_record("settings", "settings", settings)
    store.record_trading_event("settings_saved", "Custom targets saved", settings=settings)
    store.close()
    store = Store(path)
    assert store.trading_record("settings") == settings
    assert store.trading_events()[0]["settings"] == settings
    assert store.trading_events()[0]["action"] == "settings_saved"
    assert store.trading_record("missing") is None
    store.save_trading_record("settings", "settings", {**settings, "take_profit": "0.45"})
    assert store.trading_record("settings")["take_profit"] == "0.45"
    store.close()