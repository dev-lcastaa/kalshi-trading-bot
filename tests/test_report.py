import json

import pytest

from kalshi_bot.backtest.report import build_report
from kalshi_bot.data.store import Store


def _snapshot(recommendation="BUY_YES", model=0.8, market=0.5, flags=None):
    return {
        "index_id": "BRTI",
        "live": {"model_p_yes": model, "market_p_yes": market, "recommendation": recommendation},
        "quotes": {"yes_ask_dollars": 0.6, "no_ask_dollars": 0.5},
        "quality_flags": flags or [],
    }


def _record(store, ticker, result, ts, snapshot):
    store.upsert_active_market(ticker, "BRTI", 100, ts + 390_000, ts)
    store.record_decision(
        ticker=ticker, ts_ms=ts, seconds_to_expiry=390, index_price=100, strike=100,
        model_p_yes=snapshot["live"]["model_p_yes"], market_p_yes=snapshot["live"]["market_p_yes"],
        edge=0.1, recommendation=snapshot["live"]["recommendation"], confidence=0.8,
        decision_snapshot={**snapshot, "index_id": "BRTI"},
    )
    store.record_outcome(ticker, result)


def test_report_scores_clean_snapshots_and_ask_returns(tmp_path):
    path = tmp_path / "report.db"
    store = Store(str(path))
    _record(store, "YES", "yes", 1000, _snapshot())
    _record(store, "NO", "no", 2000, _snapshot("BUY_NO", 0.2, 0.5))
    report = build_report(str(path), fee_per_contract=0.01, slippage=0.02)
    assert report["clean_snapshots"] == 2
    assert report["overall"]["model"]["brier"] == pytest.approx(0.04)
    assert report["overall"]["trades"]["issued"] == 2
    assert report["overall"]["trades"]["wins"] == 2
    assert report["overall"]["trades"]["gross_pnl"] == pytest.approx(0.84)
    assert report["overall"]["abstention_rate"] == 0
    store.close()


def test_report_quarantines_flagged_snapshots(tmp_path):
    path = tmp_path / "report.db"
    store = Store(str(path))
    _record(store, "GOOD", "yes", 1000, _snapshot())
    _record(store, "BAD", "no", 2000, _snapshot("BUY_NO", 0.2, 0.5, ["stale_quote"]))
    report = build_report(str(path))
    assert report["settled_snapshots"] == 2
    assert report["clean_snapshots"] == 1
    assert report["quarantined_snapshots"] == 1
    assert report["quality_flags"] == {"stale_quote": 1}
    store.close()


def test_report_requires_nonnegative_costs(tmp_path):
    with pytest.raises(ValueError):
        build_report(str(tmp_path / "missing.db"), fee_per_contract=-1)