import time

import pytest

from kalshi_bot.data.store import Store


def _make_store(tmp_path) -> Store:
    return Store(str(tmp_path / "test.db"))


def test_shadow_decision_is_immutable_and_survives_restart(tmp_path):
    store = _make_store(tmp_path)
    decision = dict(
        ticker="BTC-SHADOW", ts_ms=1000, seconds_to_expiry=390, index_price=101,
        strike=100, model_p_yes=0.9, market_p_yes=0.6, edge=0.3,
        recommendation="BUY_YES", confidence=0.9,
    )
    snapshot = {"index_id": "BRTI", "experiment_id": "test-v1", "features": {"index_price": 101}}
    store.record_decision(**decision, shadow_snapshot=snapshot)
    store.record_decision(**decision, shadow_snapshot={**snapshot, "experiment_id": "changed"})
    store.close()
    store = _make_store(tmp_path)
    rows = store.shadow_decisions()
    assert len(rows) == 1
    assert rows[0]["snapshot"] == snapshot
    assert rows[0]["result"] is None
    assert store.shadow_decisions(index_id="SOLUSD_RTI") == []
    store.upsert_active_market("BTC-SHADOW", "BRTI", 100, 391000, 1000)
    store.record_outcome("BTC-SHADOW", "yes")
    assert store.shadow_decisions()[0]["result"] == "yes"
    store.close()


def test_decision_snapshots_pairs_features_with_settlement_result(tmp_path):
    store = _make_store(tmp_path)
    decision = dict(
        ticker="BTC-LIVE", ts_ms=1000, seconds_to_expiry=390, index_price=101,
        strike=100, model_p_yes=0.9, market_p_yes=0.6, edge=0.3,
        recommendation="BUY_YES", confidence=0.9,
    )
    snapshot = {"index_id": "BRTI", "features": {"index_price": 101}}
    store.record_decision(**decision, decision_snapshot=snapshot)
    store.upsert_active_market("BTC-LIVE", "BRTI", 100, 391000, 1000)
    store.record_outcome("BTC-LIVE", "yes")

    rows = store.decision_snapshots()

    assert len(rows) == 1
    assert rows[0]["snapshot"] == snapshot
    assert rows[0]["result"] == "yes"
    assert rows[0]["experiment_id"] == "live"
    assert store.decision_snapshots(index_id="SOLUSD_RTI") == []
    store.close()


def test_calibration_pairs_use_pre_calibration_probability_and_skip_legacy_rows(tmp_path):
    store = _make_store(tmp_path)
    now_ms = int(time.time() * 1000)
    for ticker, pre_calibration_probability in (("CALIBRATED", 0.65), ("LEGACY", None)):
        store.upsert_active_market(ticker, "BRTI", 100.0, close_ts_ms=now_ms - 60_000, now_ms=now_ms)
        store.mark_closed(ticker, closed_at_ms=now_ms)
        store.record_outcome(ticker, "yes", checked_at_ms=now_ms)
        live = {"model_p_yes": 0.3}
        if pre_calibration_probability is not None:
            live["pre_calibration_model_p_yes"] = pre_calibration_probability
        store.record_decision(
            ticker=ticker, ts_ms=now_ms - 400_000, seconds_to_expiry=390.0,
            index_price=101.0, strike=100.0, model_p_yes=0.3, market_p_yes=0.55,
            edge=-0.25, recommendation="NO_EDGE", confidence=0.7,
            decision_snapshot={"index_id": "BRTI", "live": live},
        )

    assert store.calibration_pairs() == [(0.65, 1.0)]
    assert store.calibration_pairs(index_id="SOLUSD_RTI") == []
    store.close()


def test_market_outcome_pairs_skip_degraded_and_unsettled_decisions(tmp_path):
    store = _make_store(tmp_path)
    now_ms = int(time.time() * 1000)
    for ticker, market_p, flags, result in (("CLEAN", 0.8, [], "no"), ("DEGRADED", 0.7, ["stale_quote"], "yes"),
                                            ("MISSING", None, [], "yes"), ("OPEN", 0.6, [], None)):
        store.upsert_active_market(ticker, "BRTI", 100.0, close_ts_ms=now_ms - 60_000, now_ms=now_ms)
        if result:
            store.mark_closed(ticker, closed_at_ms=now_ms)
            store.record_outcome(ticker, result, checked_at_ms=now_ms)
        store.record_decision(
            ticker=ticker, ts_ms=now_ms - 400_000, seconds_to_expiry=390.0,
            index_price=101.0, strike=100.0, model_p_yes=0.3, market_p_yes=market_p or 0.5,
            edge=0.0, recommendation="NO_EDGE", confidence=0.7,
            decision_snapshot={"index_id": "BRTI", "quality_flags": flags, "live": {"market_p_yes": market_p}},
        )

    assert store.market_outcome_pairs() == [(0.8, 0.0)]
    store.close()


def _record_settled_snapshot(store, ticker, coin, model_p, market_p, recommendation, result, bid=0.55, ask=0.6):
    now_ms = int(time.time() * 1000)
    store.upsert_active_market(ticker, coin, 100.0, close_ts_ms=now_ms - 60_000, now_ms=now_ms)
    store.mark_closed(ticker, closed_at_ms=now_ms)
    store.record_outcome(ticker, result, checked_at_ms=now_ms)
    store.record_decision(
        ticker=ticker, ts_ms=now_ms - 400_000, seconds_to_expiry=390.0, index_price=101.0,
        strike=100.0, model_p_yes=model_p, market_p_yes=market_p, edge=model_p - market_p,
        recommendation=recommendation, confidence=max(model_p, 1 - model_p),
        decision_snapshot={
            "index_id": coin,
            "live": {"model_p_yes": model_p, "market_p_yes": market_p, "recommendation": recommendation},
            "quotes": {"yes_bid_dollars": bid, "yes_ask_dollars": ask},
        },
    )


def test_trade_readiness_blocks_until_every_criterion_is_met(tmp_path):
    store = _make_store(tmp_path)
    # Model better calibrated than market and the actionable call wins after fees...
    _record_settled_snapshot(store, "BTC-WIN", "BRTI", 0.9, 0.6, "BUY_YES", "yes")
    # ...but sample and trade counts are far below the gate.
    report = store.trade_readiness(min_settled=300, min_trades=50)

    assert report["ready"] is False
    coin = report["coins"]["BRTI"]
    assert coin["ready"] is False
    assert coin["settled"] == 1
    assert coin["trades"] == 1
    assert coin["net_pnl_after_fees"] == pytest.approx(1.0 - 0.6 - 0.02)
    assert any("settled decisions" in blocker for blocker in coin["blockers"])
    assert any("actionable calls (need" in blocker for blocker in coin["blockers"])
    store.close()


def test_trade_readiness_passes_when_thresholds_met_and_flags_losing_coins(tmp_path):
    store = _make_store(tmp_path)
    for index in range(3):
        _record_settled_snapshot(store, f"BTC-{index}", "BRTI", 0.9, 0.6, "BUY_YES", "yes")
    # SOL: model worse than market and its only call loses after fees.
    _record_settled_snapshot(store, "SOL-0", "SOLUSD_RTI", 0.9, 0.6, "BUY_YES", "no")

    report = store.trade_readiness(min_settled=3, min_trades=3)

    assert report["coins"]["BRTI"]["ready"] is True
    assert report["coins"]["BRTI"]["blockers"] == []
    sol = report["coins"]["SOLUSD_RTI"]
    assert sol["ready"] is False
    assert "actionable calls lose money after fees" in sol["blockers"]
    assert "model is not better calibrated than the market price" in sol["blockers"]
    assert report["ready"] is False
    store.close()


def test_trade_readiness_empty_store_is_not_ready(tmp_path):
    store = _make_store(tmp_path)
    report = store.trade_readiness()
    assert report["ready"] is False
    assert report["coins"] == {}
    store.close()


def test_shadow_does_not_backfill_existing_decision(tmp_path):
    store = _make_store(tmp_path)
    decision = dict(
        ticker="OLD", ts_ms=1000, seconds_to_expiry=390, index_price=101,
        strike=100, model_p_yes=0.9, market_p_yes=0.6, edge=0.3,
        recommendation="BUY_YES", confidence=0.9,
    )
    store.record_decision(**decision)
    store.record_decision(**decision, shadow_snapshot={"index_id": "BRTI", "experiment_id": "test"})
    assert store.shadow_decisions() == []
    store.close()


def test_shadow_failure_rolls_back_both_records(tmp_path):
    store = _make_store(tmp_path)
    decision = dict(
        ticker="FAILED", ts_ms=1000, seconds_to_expiry=390, index_price=101,
        strike=100, model_p_yes=0.9, market_p_yes=0.6, edge=0.3,
        recommendation="BUY_YES", confidence=0.9,
    )
    with pytest.raises(KeyError):
        store.record_decision(**decision, shadow_snapshot={"index_id": "BRTI"})
    assert not store.has_decision("FAILED")
    assert store.shadow_decisions() == []
    store.record_decision(**decision)
    assert store.has_decision("FAILED")
    store.close()


def test_latest_index_prices_returns_most_recent_tick_per_index(tmp_path):
    store = _make_store(tmp_path)
    store.insert_index_tick("BRTI", 1_000, 100.0)
    store.insert_index_tick("BRTI", 2_000, 101.0)
    store.insert_index_tick("SOLUSD_RTI", 1_500, 50.0)

    prices = store.latest_index_prices()

    assert prices == {
        "BRTI": {"ts_ms": 2_000, "value": 101.0},
        "SOLUSD_RTI": {"ts_ms": 1_500, "value": 50.0},
    }


def test_latest_index_prices_empty_when_no_ticks(tmp_path):
    store = _make_store(tmp_path)
    assert store.latest_index_prices() == {}


def test_upsert_active_market_then_appears_in_active_tickers(tmp_path):
    store = _make_store(tmp_path)
    store.upsert_active_market("KXBTC15M-A", "BRTI", 100000.0, close_ts_ms=1_000_000, now_ms=1_000)
    assert store.get_active_tickers() == ["KXBTC15M-A"]


def test_mark_closed_removes_from_active_tickers(tmp_path):
    store = _make_store(tmp_path)
    store.upsert_active_market("KXBTC15M-A", "BRTI", 100000.0, close_ts_ms=1_000_000, now_ms=1_000)
    store.mark_closed("KXBTC15M-A", closed_at_ms=2_000)
    assert store.get_active_tickers() == []


def test_dashboard_markets_includes_active_and_recently_closed(tmp_path):
    store = _make_store(tmp_path)
    now_ms = int(time.time() * 1000)
    store.upsert_active_market("ACTIVE-1", "BRTI", 100.0, close_ts_ms=now_ms + 60_000, now_ms=now_ms)
    store.upsert_active_market("RECENTLY-CLOSED", "BRTI", 100.0, close_ts_ms=now_ms - 60_000, now_ms=now_ms)
    store.mark_closed("RECENTLY-CLOSED", closed_at_ms=now_ms - 10_000)  # closed 10s ago
    store.upsert_active_market("LONG-CLOSED", "BRTI", 100.0, close_ts_ms=now_ms - 600_000, now_ms=now_ms)
    store.mark_closed("LONG-CLOSED", closed_at_ms=now_ms - 600_000)  # closed 10 minutes ago

    rows = store.dashboard_markets(grace_period_sec=300)  # 5 minute grace window
    tickers = {r["ticker"] for r in rows}

    assert tickers == {"ACTIVE-1", "RECENTLY-CLOSED"}


def test_closed_markets_history_only_includes_closed(tmp_path):
    store = _make_store(tmp_path)
    now_ms = int(time.time() * 1000)
    store.upsert_active_market("ACTIVE-1", "BRTI", 100.0, close_ts_ms=now_ms + 60_000, now_ms=now_ms)
    store.upsert_active_market("CLOSED-1", "BRTI", 100.0, close_ts_ms=now_ms - 60_000, now_ms=now_ms)
    store.mark_closed("CLOSED-1", closed_at_ms=now_ms)

    rows = store.closed_markets_history()

    assert [r["ticker"] for r in rows] == ["CLOSED-1"]


def test_closed_markets_history_supports_pagination(tmp_path):
    store = _make_store(tmp_path)
    for offset in range(3):
        ticker = f"CLOSED-{offset}"
        store.upsert_active_market(ticker, "BRTI", 100.0, close_ts_ms=offset, now_ms=offset)
        store.mark_closed(ticker, closed_at_ms=offset)

    assert [row["ticker"] for row in store.closed_markets_history(limit=2)] == ["CLOSED-2", "CLOSED-1"]
    assert [row["ticker"] for row in store.closed_markets_history(limit=2, offset=2)] == ["CLOSED-0"]


def test_reupserting_a_closed_ticker_reactivates_it(tmp_path):
    store = _make_store(tmp_path)
    store.upsert_active_market("KXBTC15M-A", "BRTI", 100.0, close_ts_ms=1_000, now_ms=500)
    store.mark_closed("KXBTC15M-A", closed_at_ms=1_500)
    assert store.get_active_tickers() == []

    # A new 15-min window reuses the same series/ticker pattern in principle,
    # but if the same ticker reappears as open it should go active again.
    store.upsert_active_market("KXBTC15M-A", "BRTI", 100.0, close_ts_ms=2_000, now_ms=1_600)
    assert store.get_active_tickers() == ["KXBTC15M-A"]


def test_markets_pending_outcome_excludes_active_and_already_recorded(tmp_path):
    store = _make_store(tmp_path)
    now_ms = int(time.time() * 1000)
    store.upsert_active_market("ACTIVE-1", "BRTI", 100.0, close_ts_ms=now_ms + 60_000, now_ms=now_ms)
    store.upsert_active_market("CLOSED-PENDING", "BRTI", 100.0, close_ts_ms=now_ms - 60_000, now_ms=now_ms)
    store.mark_closed("CLOSED-PENDING", closed_at_ms=now_ms)
    store.upsert_active_market("CLOSED-RESOLVED", "BRTI", 100.0, close_ts_ms=now_ms - 60_000, now_ms=now_ms)
    store.mark_closed("CLOSED-RESOLVED", closed_at_ms=now_ms)
    store.record_outcome("CLOSED-RESOLVED", "yes", checked_at_ms=now_ms)

    pending = store.markets_pending_outcome(max_age_ms=24 * 3600 * 1000, now_ms=now_ms)

    assert pending == ["CLOSED-PENDING"]


def test_markets_pending_outcome_excludes_stale_closures(tmp_path):
    store = _make_store(tmp_path)
    now_ms = int(time.time() * 1000)
    store.upsert_active_market("OLD-CLOSED", "BRTI", 100.0, close_ts_ms=now_ms - 1000, now_ms=now_ms)
    store.mark_closed("OLD-CLOSED", closed_at_ms=now_ms - 48 * 3600 * 1000)  # closed 2 days ago

    pending = store.markets_pending_outcome(max_age_ms=24 * 3600 * 1000, now_ms=now_ms)

    assert pending == []


def test_calibration_stats_empty_when_no_resolved_markets(tmp_path):
    store = _make_store(tmp_path)
    stats = store.calibration_stats()
    assert stats["n"] == 0
    assert stats["model_brier"] is None


def test_calibration_stats_scores_the_locked_decision_not_the_last_signal(tmp_path):
    from kalshi_bot.kalshi_client.models import Signal

    store = _make_store(tmp_path)
    now_ms = int(time.time() * 1000)

    # Market settled YES. Decision (locked ~6.5 min out) was confident and correct;
    # a later live signal (closer to close) is intentionally different to prove
    # calibration scores the decision, not whatever the last signal happened to be.
    store.upsert_active_market("CASE-1", "BRTI", 100.0, close_ts_ms=now_ms - 60_000, now_ms=now_ms)
    store.mark_closed("CASE-1", closed_at_ms=now_ms)
    store.record_outcome("CASE-1", "yes", checked_at_ms=now_ms)
    store.record_decision(
        ticker="CASE-1", ts_ms=now_ms - 400_000, seconds_to_expiry=390.0, index_price=101.0,
        strike=100.0, model_p_yes=0.9, market_p_yes=0.55, edge=0.35, recommendation="BUY_YES", confidence=0.35,
    )
    store.insert_signal(
        Signal(
            ticker="CASE-1", ts_ms=now_ms, index_id="BRTI", index_price=99.0, strike=100.0,
            seconds_to_expiry=5.0, model_p_yes=0.1, market_p_yes=0.2, edge=-0.1,
            recommendation="BUY_NO", confidence=0.1,
        )
    )

    stats = store.calibration_stats()

    assert stats["n"] == 1
    assert stats["model_brier"] == (0.9 - 1.0) ** 2  # from the decision, not the later 0.1 signal
    assert stats["market_brier"] == (0.55 - 1.0) ** 2
    assert stats["baseline_brier"] == (0.5 - 1.0) ** 2
    assert stats["model_brier"] < stats["market_brier"]  # model was more accurate here


def test_decision_feature_outcome_pairs_reads_features_from_snapshot(tmp_path):
    store = _make_store(tmp_path)
    now_ms = int(time.time() * 1000)
    features = {
        "index_price": 100.0, "strike": 100.0, "seconds_to_expiry": 300.0,
        "realized_vol_per_sqrt_sec": 0.001, "momentum_per_sec": 0.0,
        "book_imbalance": 0.1, "momentum_ols_per_sec": 0.0,
        "momentum_short_per_sec": 0.0, "window_ticks_observed": 0,
        "window_avg_so_far": None, "history_span_sec": 300.0, "history_tick_count": 300,
    }

    store.upsert_active_market("BTC-1", "BRTI", 100.0, close_ts_ms=now_ms - 60_000, now_ms=now_ms)
    store.mark_closed("BTC-1", closed_at_ms=now_ms)
    store.record_outcome("BTC-1", "yes", checked_at_ms=now_ms)
    store.record_decision(
        ticker="BTC-1", ts_ms=now_ms - 400_000, seconds_to_expiry=390.0, index_price=101.0,
        strike=100.0, model_p_yes=0.9, market_p_yes=0.55, edge=0.35, recommendation="BUY_YES", confidence=0.35,
        decision_snapshot={"index_id": "BRTI", "features": features},
    )

    # Unsettled market with a snapshot shouldn't show up in training pairs yet.
    store.upsert_active_market("BTC-2", "BRTI", 100.0, close_ts_ms=now_ms - 60_000, now_ms=now_ms)
    store.record_decision(
        ticker="BTC-2", ts_ms=now_ms - 400_000, seconds_to_expiry=390.0, index_price=101.0,
        strike=100.0, model_p_yes=0.9, market_p_yes=0.55, edge=0.35, recommendation="BUY_YES", confidence=0.35,
        decision_snapshot={"index_id": "BRTI", "features": features},
    )

    pairs = store.decision_feature_outcome_pairs()

    assert pairs == [(features, 1.0)]


def test_calibration_stats_filters_by_coin(tmp_path):
    store = _make_store(tmp_path)
    now_ms = int(time.time() * 1000)

    store.upsert_active_market("BTC-1", "BRTI", 100.0, close_ts_ms=now_ms - 60_000, now_ms=now_ms)
    store.mark_closed("BTC-1", closed_at_ms=now_ms)
    store.record_outcome("BTC-1", "yes", checked_at_ms=now_ms)
    store.record_decision(
        ticker="BTC-1", ts_ms=now_ms - 400_000, seconds_to_expiry=390.0, index_price=101.0,
        strike=100.0, model_p_yes=0.9, market_p_yes=0.55, edge=0.35, recommendation="BUY_YES", confidence=0.4,
    )

    store.upsert_active_market("SOL-1", "SOLUSD_RTI", 50.0, close_ts_ms=now_ms - 60_000, now_ms=now_ms)
    store.mark_closed("SOL-1", closed_at_ms=now_ms)
    store.record_outcome("SOL-1", "no", checked_at_ms=now_ms)
    store.record_decision(
        ticker="SOL-1", ts_ms=now_ms - 400_000, seconds_to_expiry=390.0, index_price=49.0,
        strike=50.0, model_p_yes=0.2, market_p_yes=0.3, edge=-0.1, recommendation="BUY_NO", confidence=0.3,
    )

    btc_stats = store.calibration_stats(index_id="BRTI")
    sol_stats = store.calibration_stats(index_id="SOLUSD_RTI")
    overall_stats = store.calibration_stats()

    assert btc_stats["n"] == 1
    assert btc_stats["model_brier"] == (0.9 - 1.0) ** 2
    assert sol_stats["n"] == 1
    assert sol_stats["model_brier"] == (0.2 - 0.0) ** 2
    assert overall_stats["n"] == 2


def test_calibration_stats_reports_total_settled_markets_separately_from_rolling_sample(tmp_path):
    store = _make_store(tmp_path)
    for index in range(3):
        ticker = f"SETTLED-{index}"
        store.upsert_active_market(ticker, "BRTI", 100.0, close_ts_ms=index, now_ms=index)
        store.record_decision(
            ticker=ticker, ts_ms=index, seconds_to_expiry=390.0, index_price=101.0,
            strike=100.0, model_p_yes=0.9, market_p_yes=0.55, edge=0.35,
            recommendation="BUY_YES", confidence=0.4,
        )
        store.record_outcome(ticker, "yes", checked_at_ms=index)

    stats = store.calibration_stats(limit=2)
    assert stats["n"] == 2
    assert stats["settled_count"] == 3


def test_has_decision_false_until_recorded(tmp_path):
    store = _make_store(tmp_path)
    assert store.has_decision("KXBTC15M-A") is False
    store.record_decision(
        ticker="KXBTC15M-A", ts_ms=1_000, seconds_to_expiry=390.0, index_price=100.0,
        strike=99.0, model_p_yes=0.7, market_p_yes=0.6, edge=0.1, recommendation="BUY_YES", confidence=0.1,
    )

    assert store.has_decision("KXBTC15M-A") is True


def test_record_decision_stores_confirmation_votes(tmp_path):
    store = _make_store(tmp_path)
    store.upsert_active_market("KXBTC15M-A", "BRTI", 99.0, close_ts_ms=10_000, now_ms=500)
    store.record_decision(
        ticker="KXBTC15M-A", ts_ms=1_000, seconds_to_expiry=390.0, index_price=100.0,
        strike=99.0, model_p_yes=0.7, market_p_yes=0.6, edge=0.1, recommendation="BUY_YES", confidence=0.1,
        confirmation_agree=2, confirmation_total=3,
    )

    rows = store.dashboard_markets(grace_period_sec=0)

    assert rows[0]["decision_confirmation_agree"] == 2
    assert rows[0]["decision_confirmation_total"] == 3


def test_record_decision_is_one_shot(tmp_path):
    store = _make_store(tmp_path)
    store.record_decision(
        ticker="KXBTC15M-A", ts_ms=1_000, seconds_to_expiry=390.0, index_price=100.0,
        strike=99.0, model_p_yes=0.7, market_p_yes=0.6, edge=0.1, recommendation="BUY_YES", confidence=0.1,
    )
    # A second call for the same ticker must not overwrite the first decision.
    store.record_decision(
        ticker="KXBTC15M-A", ts_ms=2_000, seconds_to_expiry=300.0, index_price=105.0,
        strike=99.0, model_p_yes=0.95, market_p_yes=0.9, edge=0.05, recommendation="BUY_YES", confidence=0.05,
    )

    rows = store.dashboard_markets(grace_period_sec=0)
    store.upsert_active_market("KXBTC15M-A", "BRTI", 99.0, close_ts_ms=10_000, now_ms=500)
    rows = store.dashboard_markets(grace_period_sec=0)
    assert rows[0]["decision_ts_ms"] == 1_000
    assert rows[0]["decision_model_p_yes"] == 0.7


def test_insert_whale_trade_is_idempotent_by_trade_id(tmp_path):
    store = _make_store(tmp_path)
    store.insert_whale_trade(
        trade_id="T1", ticker="KXBTC15M-A", ts_ms=1_000, side="yes", count=200.0,
        price_cents=60.0, notional_usd=120.0,
    )
    # Re-polling the same trade (already-seen fill) must not create a duplicate row.
    store.insert_whale_trade(
        trade_id="T1", ticker="KXBTC15M-A", ts_ms=1_000, side="yes", count=200.0,
        price_cents=60.0, notional_usd=120.0,
    )

    trades = store.recent_whale_trades("KXBTC15M-A")

    assert len(trades) == 1
    assert trades[0]["notional_usd"] == 120.0


def test_recent_whale_trades_scoped_to_ticker_and_ordered_newest_first(tmp_path):
    store = _make_store(tmp_path)
    store.insert_whale_trade(
        trade_id="T1", ticker="KXBTC15M-A", ts_ms=1_000, side="yes", count=200.0,
        price_cents=60.0, notional_usd=120.0,
    )
    store.insert_whale_trade(
        trade_id="T2", ticker="KXBTC15M-A", ts_ms=2_000, side="no", count=300.0,
        price_cents=40.0, notional_usd=120.0,
    )
    store.insert_whale_trade(
        trade_id="T3", ticker="KXSOL15M-A", ts_ms=1_500, side="yes", count=100.0,
        price_cents=50.0, notional_usd=50.0,
    )

    trades = store.recent_whale_trades("KXBTC15M-A")

    assert [t["trade_id"] for t in trades] == ["T2", "T1"]


def test_latest_whale_trade_ts_none_when_no_trades_recorded(tmp_path):
    store = _make_store(tmp_path)
    assert store.latest_whale_trade_ts("KXBTC15M-A") is None

    store.insert_whale_trade(
        trade_id="T1", ticker="KXBTC15M-A", ts_ms=1_000, side="yes", count=200.0,
        price_cents=60.0, notional_usd=120.0,
    )

    assert store.latest_whale_trade_ts("KXBTC15M-A") == 1_000
