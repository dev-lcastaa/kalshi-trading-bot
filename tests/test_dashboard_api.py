from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from kalshi_bot import __version__
from kalshi_bot.dashboard.server import create_app
from kalshi_bot.data.store import Store


def test_version_endpoint_reports_package_version(tmp_path):
    store = Store(str(tmp_path / "version.db"))
    client = TestClient(create_app(store))

    response = client.get("/api/version")

    assert response.status_code == 200
    assert response.json() == {"version": __version__}
    store.close()


def test_decision_snapshots_endpoint_validates_limit_and_returns_rows(tmp_path):
    store = Store(str(tmp_path / "decision_snapshots.db"))
    store.record_decision(
        ticker="BTC-1", ts_ms=1000, seconds_to_expiry=390, index_price=101, strike=100,
        model_p_yes=0.9, market_p_yes=0.6, edge=0.3, recommendation="BUY_YES", confidence=0.9,
        decision_snapshot={"index_id": "BRTI", "features": {"index_price": 101}},
    )
    client = TestClient(create_app(store))

    response = client.get("/api/decision-snapshots")

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["experiment_id"] == "live"
    assert client.get("/api/decision-snapshots?limit=0").status_code == 422
    assert client.get("/api/decision-snapshots?limit=10001").status_code == 422
    store.close()


def test_calibration_summary_combines_and_caches_scopes(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "summary.db"))
    calls = []

    def calibration_stats(limit=200, index_id=None):
        calls.append((limit, index_id))
        return {"n": 1, "settled_count": 1, "model_brier": 0.1, "market_brier": 0.2}

    monkeypatch.setattr(store, "calibration_stats", calibration_stats)
    client = TestClient(create_app(store))

    first = client.get("/api/calibration-summary?limit=50")
    second = client.get("/api/calibration-summary?limit=50")

    assert first.status_code == 200
    assert second.json() == first.json()
    assert set(first.json()) == {"overall", "BRTI", "SOLUSD_RTI"}
    assert calls == [(50, None), (50, "BRTI"), (50, "SOLUSD_RTI")]
    assert client.get("/api/calibration-summary?limit=0").status_code == 422
    store.close()


def test_shared_store_buffers_concurrent_query_results(tmp_path):
    store = Store(str(tmp_path / "concurrent.db"))
    for index in range(20):
        store.upsert_active_market(
            f"BTC-{index}", "BRTI", 100 + index, close_ts_ms=10_000 + index, now_ms=index,
        )

    def read_markets():
        return len(store.dashboard_markets(grace_period_sec=0))

    with ThreadPoolExecutor(max_workers=8) as pool:
        counts = list(pool.map(lambda _: read_markets(), range(40)))

    assert counts == [20] * 40
    store.close()
