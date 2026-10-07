import asyncio
import time
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import httpx

from kalshi_bot.auto_trader import AutoTrader, order_payload
from kalshi_bot.data.store import Store
from kalshi_bot.paper import PaperExchange


@pytest.mark.asyncio
async def test_run_publishes_completed_checks_with_absolute_market_deadlines(tmp_path):
    store = Store(str(tmp_path / "stream.db"))
    closes_at = int(time.time() * 1000) + 117000
    trader = AutoTrader(store, None, market_feed=lambda: [
        {"ticker": "KXBTC15M-STREAM", "close_ts_ms": closes_at},
    ])
    updates = []

    async def publish():
        updates.append(trader.snapshot())
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await trader.run(publish)
    assert updates[0]["last_cycle_ms"] is not None
    assert updates[0]["watch"][0]["close_ts_ms"] == closes_at
    assert not trader.enabled
    store.close()


@pytest.mark.parametrize("side,action,book,price", [
    ("yes", "buy", "bid", "0.45"), ("yes", "sell", "ask", "0.45"),
    ("no", "buy", "ask", "0.55"), ("no", "sell", "bid", "0.55"),
])
def test_order_direction_uses_yes_pricing(side, action, book, price):
    payload = order_payload("BTC", side, action, Decimal("2"), Decimal("0.45"), "id")
    assert payload["side"] == book
    assert payload["price"] == price
    assert payload["reduce_only"] == (action == "sell")
    assert payload["time_in_force"] == "immediate_or_cancel"


@pytest.mark.asyncio
async def test_controls_save_thresholds_and_restart_paused(tmp_path):
    store = Store(str(tmp_path / "trader.db"))
    trader = AutoTrader(store, AsyncMock(), execution_allowed=True, account_identity="test-account")
    await trader.cycle()
    await trader.save_settings(rules(budget="1.25", take_profit="0.25", stop_loss="0.05"))
    with pytest.raises(ValueError, match="Confirm"):
        await trader.control(True, False)
    assert (await trader.control(True, True))["enabled"]
    with pytest.raises(ValueError, match="Pause"):
        await trader.save_settings(rules(budget="1"))
    assert not (await trader.control(False, False))["enabled"]
    restarted = AutoTrader(store, AsyncMock(), execution_allowed=True, account_identity="test-account")
    assert restarted.snapshot()["settings"]["rules"][0]["take_profit"] == "0.25"
    assert not restarted.enabled
    store.close()


def position(side="yes"):
    return {"ticker": "BTC", "side": side, "status": "open", "quantity": "2",
            "account_identity": "test-account",
            "entry_cost": "0.94", "exit_credit": "0", "pending": None, "net_pnl": None,
            "policy": {"budget": "1", "take_profit": "0.50", "stop_loss": "0.10"}}


@pytest.mark.asyncio
async def test_paused_trader_monitors_and_exits_stop_loss(tmp_path):
    rest = AsyncMock()
    rest.get_market.return_value = {"market": {"status": "active"}}
    rest.get_positions.return_value = {"market_positions": [{"ticker": "BTC", "position_fp": "2"}]}
    rest.get_market_orderbook.return_value = {"orderbook_fp": {"yes_dollars": [["0.40", "2"]]}}
    rest.create_event_order.return_value = {"order_id": "order"}
    rest.get_order.return_value = {"order": {"ticker": "BTC", "status": "executed", "fill_count_fp": "2"}}
    rest.get_fills.return_value = {"fills": [{"fill_id": "fill", "order_id": "order", "ticker": "BTC",
        "count_fp": "2", "yes_price_dollars": "0.40", "fee_cost": "0.04"}]}
    store = Store(str(tmp_path / "exit.db"))
    trader = AutoTrader(store, rest, execution_allowed=True, account_identity="test-account")
    trader.save_position(position())

    async def submit(payload):
        rest.get_order.return_value["order"]["client_order_id"] = payload["client_order_id"]
        return {"order_id": "order", "fill_count": "2"}

    rest.create_event_order.side_effect = submit
    await trader.cycle()
    assert rest.create_event_order.call_args.args[0]["reduce_only"]
    assert trader.positions()[0]["status"] == "closed"
    assert Decimal(trader.positions()[0]["net_pnl"]) == Decimal("-0.18")
    tracking = trader.positions()[0]["pnl_tracking"]
    assert Decimal(tracking["low"]["net_pnl"]) == Decimal("-0.18")
    assert tracking["low"]["source"] == "liquidation_quote"
    assert tracking["samples"] == 2
    assert not trader.enabled
    store.close()


@pytest.mark.asyncio
async def test_unknown_order_blocks_entries_and_is_not_resubmitted(tmp_path):
    rest = AsyncMock()
    rest.get_orders.return_value = {"orders": [], "cursor": ""}
    store = Store(str(tmp_path / "unknown.db"))
    trader = AutoTrader(store, rest, execution_allowed=True, account_identity="test-account")
    pending = position()
    pending["pending"] = {"client_order_id": "unknown", "action": "buy", "quantity": "2"}
    pending["status"] = "pending"
    trader.save_position(pending)
    await trader.cycle()
    await trader.cycle()
    rest.create_event_order.assert_not_called()
    assert trader.positions()[0]["pending"] is not None
    assert trader.error
    with pytest.raises(ValueError):
        await trader.control(True, True)
    store.close()


@pytest.mark.asyncio
async def test_stop_loss_attempts_available_partial_liquidity(tmp_path):
    rest = AsyncMock()
    rest.get_market.return_value = {"market": {"status": "active"}}
    rest.get_positions.return_value = {"market_positions": [{"ticker": "BTC", "position_fp": "2"}]}
    rest.get_market_orderbook.return_value = {"orderbook_fp": {"yes_dollars": [["0.40", "0.50"]]}}
    store = Store(str(tmp_path / "partial.db"))
    trader = AutoTrader(store, rest, execution_allowed=True, account_identity="test-account")
    trader.submit = AsyncMock()
    held = position()
    await trader.monitor(held)
    trader.submit.assert_awaited_once()
    assert trader.submit.call_args.args[2] == Decimal("0.50")
    assert trader.submit.call_args.args[-1] == "stop_loss"
    assert held["net_pnl"] is None
    assert held["pnl_tracking"]["samples"] == 0
    assert held["pnl_tracking"]["unavailable_samples"] == 1
    assert held["pnl_tracking"]["low"] is None
    store.close()


def test_fee_reserve_covers_separate_one_contract_fills():
    from kalshi_bot.trading import fee_reserve

    assert fee_reserve(4) >= 4 * Decimal("0.02")


@pytest.mark.asyncio
async def test_disable_supersedes_queued_enable(tmp_path):
    store = Store(str(tmp_path / "race.db"))
    trader = AutoTrader(store, AsyncMock(), execution_allowed=True, account_identity="test-account")
    await trader.cycle()
    await trader.lock.acquire()
    queued = asyncio.create_task(trader.control(True, True))
    await asyncio.sleep(0)
    await trader.control(False, False)
    trader.lock.release()
    with pytest.raises(ValueError, match="superseded"):
        await queued
    assert not trader.enabled
    store.close()


@pytest.mark.asyncio
async def test_changed_credentials_do_not_touch_old_positions(tmp_path):
    store = Store(str(tmp_path / "account.db"))
    rest = AsyncMock()
    trader = AutoTrader(store, rest, execution_allowed=True, account_identity="different-account")
    trader.save_position(position())
    await trader.cycle()
    rest.get_market.assert_not_called()
    rest.create_event_order.assert_not_called()
    assert trader.error
    store.close()


def test_only_one_worker_can_own_database_until_close(tmp_path):
    path = str(tmp_path / "exclusive.db")
    first = Store(path)
    second = Store(path)
    assert first.claim_trading_worker("one")
    assert first.claim_trading_worker("one")
    assert not first.claim_trading_worker("two")
    assert not second.claim_trading_worker("two")
    first.close()
    assert second.claim_trading_worker("two")
    second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["closed", "determined", "finalized"])
async def test_finalized_only_settlement_is_recorded_once(tmp_path, status):
    store = Store(str(tmp_path / "lifecycle.db"))
    rest = AsyncMock()
    rest.get_market.return_value = {"market": {"status": status, "result": "yes"}}
    trader = AutoTrader(store, rest, execution_allowed=True, account_identity="test-account")
    trader.save_position(position())
    await trader.cycle()
    await trader.cycle()
    held = trader.positions()[0]
    assert held["status"] == ("closed" if status == "finalized" else "open")
    assert held["exit_credit"] == ("2" if status == "finalized" else "0")
    rest.create_event_order.assert_not_called()
    store.close()


def entry_rest():
    rest = AsyncMock()
    rest.get_market.return_value = {"market": {"status": "active", "event_ticker": "EVENT",
        "market_type": "binary", "notional_value_dollars": "1.00", "settlement_bounds_type": "default"}}
    rest.get_event.return_value = {"event": {"series_ticker": "SERIES"}}
    rest.get_series.return_value = {"series": {"fee_type": "quadratic", "fee_multiplier": 1}}
    rest.get_positions.return_value = {"market_positions": []}
    rest.get_orders.return_value = {"orders": [], "cursor": ""}
    rest.get_market_orderbook.return_value = {"orderbook_fp": {"yes_dollars": [["0.45", "10"]], "no_dollars": [["0.55", "10"]]}}
    return rest


def live_market(ticker="BTC", model_p_yes=0.9, yes_bid=0.44, yes_ask=0.45, seconds_left=360, **extra):
    now_ms = int(time.time() * 1000)
    row = {"ticker": ticker, "index_id": "BRTI", "ts_ms": now_ms, "close_ts_ms": now_ms + seconds_left * 1000,
           "model_p_yes": model_p_yes, "market_p_yes": (yes_bid + yes_ask) / 2,
           "yes_bid": yes_bid, "yes_ask": yes_ask, "recommendation": "BUY_YES", "quality_flags": []}
    row.update(extra)
    return row


def rules(**overrides):
    rule = {"name": "Test", "side": "model", "min_price": "0.01", "max_price": "0.99", "min_confidence": "0",
            "min_edge": None, "min_seconds_left": 0, "max_seconds_left": 900,
            "budget": "1.00", "take_profit": "0", "stop_loss": "0"}
    rule.update(overrides)
    return {"rules": [rule]}


def feed_trader(store, rest, markets=None, **kwargs):
    feed = markets if markets is not None else [live_market()]
    kwargs.setdefault("execution_allowed", True)
    kwargs.setdefault("account_identity", "test-account")
    return AutoTrader(store, rest, market_feed=lambda: [dict(row, ts_ms=int(time.time() * 1000)) for row in feed],
                      **kwargs)


@pytest.mark.asyncio
async def test_fresh_entry_uses_saved_settings_partial_fills_and_no_duplicates(tmp_path):
    store = Store(str(tmp_path / "entry.db"))
    rest = entry_rest()
    trader = feed_trader(store, rest)
    await trader.cycle()
    await trader.save_settings(rules(budget="1.25", take_profit="0.25", stop_loss="0.10"))
    await trader.control(True, True)

    async def submit(payload):
        rest.get_order.return_value = {"order": {"ticker": "BTC", "order_id": "buy", "status": "canceled",
            "client_order_id": payload["client_order_id"], "fill_count_fp": "1.5"}}
        rest.get_fills.return_value = {"fills": [{"fill_id": "one", "order_id": "buy", "ticker": "BTC",
            "count_fp": "1.5", "yes_price_dollars": "0.44", "fee_cost": "0.03"}], "cursor": ""}
        return {"order_id": "buy", "fill_count": "1.5"}

    rest.create_event_order.side_effect = submit
    await trader.cycle()
    held = trader.positions()[0]
    assert held["quantity"] == "1.5"
    assert Decimal(held["entry_cost"]) == Decimal("0.69")
    assert held["policy"]["take_profit"] == "0.25"
    assert rest.create_event_order.call_args.args[0]["count"] == "2"
    assert rest.create_event_order.call_args.args[0]["price"] == "0.45"
    rest.get_market.return_value = {"market": {"status": "closed"}}
    await trader.cycle()
    rest.create_event_order.assert_awaited_once()
    store.close()


@pytest.mark.asyncio
async def test_no_side_profit_exit_uses_net_fills_and_price_protection(tmp_path):
    store = Store(str(tmp_path / "profit.db"))
    rest = AsyncMock()
    rest.get_market.return_value = {"market": {"status": "active"}}
    rest.get_positions.return_value = {"market_positions": [{"ticker": "BTC", "position_fp": "-2"}]}
    rest.get_market_orderbook.return_value = {"orderbook_fp": {"no_dollars": [["0.80", "1"], ["0.75", "1"]]}}
    trader = AutoTrader(store, rest, execution_allowed=True, account_identity="test-account")
    trader.save_position(position("no"))

    async def submit(payload):
        rest.get_order.return_value = {"order": {"ticker": "BTC", "status": "executed", "fill_count_fp": "2",
            "client_order_id": payload["client_order_id"]}}
        rest.get_fills.return_value = {"fills": [{"fill_id": "exit", "order_id": "sell", "ticker": "BTC",
            "count_fp": "2", "no_price_dollars": "0.75", "fee_cost": "0.04"}], "cursor": ""}
        return {"order_id": "sell", "fill_count": "2"}

    rest.create_event_order.side_effect = submit
    await trader.cycle()
    payload = rest.create_event_order.call_args.args[0]
    assert payload["side"] == "bid"
    assert payload["price"] == "0.25"
    assert payload["reduce_only"]
    assert Decimal(trader.positions()[0]["net_pnl"]) == Decimal("0.52")
    store.close()


@pytest.mark.asyncio
async def test_delayed_fill_history_keeps_pending_and_recovery_is_not_double_counted(tmp_path):
    store = Store(str(tmp_path / "delayed.db"))
    rest = AsyncMock()
    held = position()
    held.update(status="pending", quantity="0", entry_cost="0", pending={"client_order_id": "id",
        "order_id": "order", "action": "buy", "quantity": "2", "ack_fill_count": "2", "reason": "Decision"})
    rest.get_order.return_value = {"order": {"ticker": "BTC", "client_order_id": "id", "status": "executed", "fill_count_fp": "0"}}
    rest.get_fills.return_value = {"fills": [], "cursor": ""}
    rest.get_market.return_value = {"market": {"status": "closed"}}
    trader = AutoTrader(store, rest, execution_allowed=True, account_identity="test-account")
    trader.save_position(held)
    await trader.cycle()
    assert trader.positions()[0]["pending"]
    rest.get_order.return_value["order"]["fill_count_fp"] = "2"
    await trader.cycle()
    assert trader.positions()[0]["pending"]
    fill = {"fill_id": "fill", "order_id": "order", "ticker": "BTC", "count_fp": "2", "yes_price_dollars": "0.45", "fee_cost": "0.04"}
    rest.get_fills.return_value = {"fills": [fill, fill], "cursor": ""}
    await trader.cycle()
    await trader.cycle()
    assert trader.positions()[0]["pending"] is None
    assert Decimal(trader.positions()[0]["entry_cost"]) == Decimal("0.94")
    assert len(trader.positions()[0]["execution_history"]) == 1
    rest.create_event_order.assert_not_called()
    store.close()


@pytest.mark.asyncio
async def test_disable_during_entry_checks_prevents_submission(tmp_path):
    store = Store(str(tmp_path / "stop_entry.db"))
    rest = entry_rest()
    trader = feed_trader(store, rest)
    await trader.cycle()
    await trader.save_settings(rules())
    await trader.control(True, True)

    async def orderbook(ticker):
        await trader.control(False, False)
        return {"orderbook_fp": {"no_dollars": [["0.55", "10"]], "yes_dollars": [["0.45", "10"]]}}

    rest.get_market_orderbook.side_effect = orderbook
    await trader.cycle()
    rest.create_event_order.assert_not_called()
    assert not trader.enabled
    store.close()


@pytest.mark.asyncio
async def test_zero_quantity_book_does_not_journal_nonexistent_exit(tmp_path):
    store = Store(str(tmp_path / "zero.db"))
    rest = AsyncMock()
    rest.get_market.return_value = {"market": {"status": "active"}}
    rest.get_positions.return_value = {"market_positions": [{"ticker": "BTC", "position_fp": "2"}]}
    rest.get_market_orderbook.return_value = {"orderbook_fp": {"yes_dollars": [["0.40", "0"]]}}
    trader = AutoTrader(store, rest, execution_allowed=True, account_identity="test-account")
    trader.save_position(position())
    await trader.cycle()
    assert trader.positions()[0]["pending"] is None
    assert not trader.error
    rest.create_event_order.assert_not_called()
    trader.submit = AsyncMock()
    rest.get_market_orderbook.return_value = {"orderbook_fp": {"yes_dollars": [["0.40", "2"]]}}
    await trader.cycle()
    trader.submit.assert_awaited_once()
    store.close()


@pytest.mark.asyncio
async def test_lost_acknowledgment_zero_fill_read_stays_pending(tmp_path):
    store = Store(str(tmp_path / "lost_ack.db"))
    rest = AsyncMock()
    held = position()
    held.update(status="pending", quantity="0", entry_cost="0", pending={"client_order_id": "id",
        "order_id": "order", "action": "buy", "quantity": "2", "reason": "Decision"})
    rest.get_order.return_value = {"order": {"ticker": "BTC", "client_order_id": "id", "status": "canceled", "fill_count_fp": "0"}}
    rest.get_fills.return_value = {"fills": [], "cursor": ""}
    trader = AutoTrader(store, rest, execution_allowed=True, account_identity="test-account")
    trader.save_position(held)
    await trader.cycle()
    assert trader.positions()[0]["pending"]
    assert "acknowledgment" in trader.error
    rest.create_event_order.assert_not_called()
    store.close()


@pytest.mark.asyncio
async def test_entry_is_skipped_if_fees_and_spread_already_reach_loss_limit(tmp_path):
    store = Store(str(tmp_path / "entry_costs.db"))
    rest = entry_rest()
    rest.get_market_orderbook.return_value["orderbook_fp"]["yes_dollars"] = [["0.40", "10"]]
    trader = feed_trader(store, rest)
    await trader.cycle()
    await trader.save_settings(rules(take_profit="0.50", stop_loss="0.10"))
    await trader.control(True, True)
    await trader.cycle()
    rest.create_event_order.assert_not_called()
    assert trader.positions() == []
    assert any("already reach the stop loss" in event["reason"] for event in trader.snapshot()["events"])
    store.close()


@pytest.mark.asyncio
async def test_accepted_entry_lost_response_reconciles_without_second_post(tmp_path):
    store = Store(str(tmp_path / "lost_response.db"))
    rest = entry_rest()
    trader = feed_trader(store, rest)
    await trader.cycle()
    await trader.save_settings(rules())
    await trader.control(True, True)

    async def lost_response(payload):
        rest.get_orders.return_value = {"orders": [{"order_id": "accepted", "client_order_id": payload["client_order_id"]}], "cursor": ""}
        rest.get_order.return_value = {"order": {"ticker": "BTC", "status": "executed", "fill_count_fp": "2",
            "client_order_id": payload["client_order_id"]}}
        rest.get_fills.return_value = {"fills": [{"fill_id": "fill", "order_id": "accepted", "ticker": "BTC",
            "count_fp": "2", "yes_price_dollars": "0.45", "fee_cost": "0.04"}], "cursor": ""}
        raise httpx.ReadTimeout("Response lost")

    rest.create_event_order.side_effect = lost_response
    await trader.cycle()
    assert trader.positions()[0]["status"] == "pending"
    assert not trader.enabled
    rest.get_market.return_value = {"market": {"status": "closed"}}
    await trader.cycle()
    assert trader.positions()[0]["status"] == "open"
    assert Decimal(trader.positions()[0]["entry_cost"]) == Decimal("0.94")
    assert trader.positions()[0]["pending"] is None
    rest.create_event_order.assert_awaited_once()
    store.close()


@pytest.mark.asyncio
async def test_partial_exit_continues_after_restart_without_double_cost(tmp_path):
    path = str(tmp_path / "partial_restart.db")
    store = Store(path)
    rest = AsyncMock()
    rest.get_market.return_value = {"market": {"status": "active"}}
    rest.get_market_orderbook.return_value = {"orderbook_fp": {"yes_dollars": [["0.40", "2"]]}}
    rest.get_positions.return_value = {"market_positions": [{"ticker": "BTC", "position_fp": "2"}]}
    trader = AutoTrader(store, rest, execution_allowed=True, account_identity="test-account")
    trader.save_position(position())
    counts = iter([("0.50", "0.01"), ("1.50", "0.03")])

    async def partial_submit(payload):
        count, fee = next(counts)
        rest.get_order.return_value = {"order": {"ticker": "BTC", "status": "canceled", "fill_count_fp": count,
            "client_order_id": payload["client_order_id"]}}
        rest.get_fills.return_value = {"fills": [{"fill_id": payload["client_order_id"], "order_id": "exit",
            "ticker": "BTC", "count_fp": count, "yes_price_dollars": "0.40", "fee_cost": fee}], "cursor": ""}
        return {"order_id": "exit", "fill_count": count}

    rest.create_event_order.side_effect = partial_submit
    await trader.cycle()
    assert trader.positions()[0]["quantity"] == "1.50"
    assert Decimal(trader.positions()[0]["exit_credit"]) == Decimal("0.19")
    store.close()
    store = Store(path)
    restarted = AutoTrader(store, rest, execution_allowed=True, account_identity="test-account")
    rest.get_positions.return_value = {"market_positions": [{"ticker": "BTC", "position_fp": "1.50"}]}
    await restarted.cycle()
    held = restarted.positions()[0]
    assert held["status"] == "closed"
    assert Decimal(held["entry_cost"]) == Decimal("0.94")
    assert Decimal(held["net_pnl"]) == Decimal("-0.18")
    assert not restarted.enabled
    assert rest.create_event_order.await_count == 2
    assert held["pnl_tracking"]["samples"] == 3
    assert Decimal(held["pnl_tracking"]["low"]["net_pnl"]) == Decimal("-0.19")
    assert Decimal(held["pnl_tracking"]["high"]["net_pnl"]) == Decimal("-0.18")
    store.close()


@pytest.mark.asyncio
async def test_paper_mode_simulates_fees_and_take_profit_without_real_orders(tmp_path):
    store = Store(str(tmp_path / "paper.db"))
    rest = entry_rest()
    trader = feed_trader(store, PaperExchange(store, rest), mode="paper", account_identity="paper")
    await trader.cycle()
    await trader.save_settings(rules(take_profit="0.50", stop_loss="0.10"))
    await trader.control(True, True)
    await trader.cycle()
    held = trader.positions()[0]
    assert held["status"] == "open"
    assert held["quantity"] == "2"
    assert Decimal(held["entry_cost"]) == Decimal("0.94")
    assert store.trading_record("paper_account")["BTC"] == "2"
    rest.get_market_orderbook.return_value = {"orderbook_fp": {"yes_dollars": [["0.75", "10"]], "no_dollars": [["0.55", "10"]]}}
    await trader.cycle()
    held = trader.positions()[0]
    assert held["status"] == "closed"
    assert Decimal(held["net_pnl"]) == Decimal("0.53")
    assert held["closed_by"] == "take_profit" and "result" not in held
    assert held["pnl_tracking"]["from_entry"]
    assert Decimal(held["pnl_tracking"]["high"]["net_pnl"]) == Decimal("0.53")
    assert held["pnl_tracking"]["high"]["source"] == "exit_fill"
    assert store.trading_record("paper_account")["BTC"] == "0"
    rest.create_event_order.assert_not_called()
    snapshot = trader.snapshot()
    assert snapshot["mode"] == "paper"
    assert any("take_profit" in event["reason"] for event in snapshot["events"] if event["action"] == "filled")
    assert all(event["mode"] == "paper" for event in snapshot["events"])
    store.close()


@pytest.mark.asyncio
async def test_paper_and_live_settings_are_independent(tmp_path):
    store = Store(str(tmp_path / "modes.db"))
    live = AutoTrader(store, AsyncMock(), execution_allowed=True, account_identity="test-account")
    paper = AutoTrader(store, AsyncMock(), execution_allowed=True, mode="paper",
                       account_identity="paper", worker_id=live.worker_id)
    await live.cycle()
    await paper.cycle()
    await paper.save_settings(rules(budget="1.50", take_profit="0.75", stop_loss="0.20"))
    assert live.snapshot()["settings"]["rules"][0]["budget"] == "1.00"
    assert paper.snapshot()["settings"]["rules"][0]["budget"] == "1.50"
    assert not live.blockers()
    assert not paper.blockers()
    store.close()

@pytest.mark.asyncio
async def test_paper_scale_in_adds_buys_up_to_the_rule_limit(tmp_path):
    store = Store(str(tmp_path / "scale.db"))
    rest = entry_rest()
    trader = feed_trader(store, PaperExchange(store, rest), mode="paper", account_identity="paper")
    await trader.cycle()
    await trader.save_settings(rules(max_entries=3, reentry_gap_sec=0))
    await trader.control(True, True)
    for _ in range(5):
        await trader.cycle()
    held = trader.positions()[0]
    assert held["status"] == "open" and held["entries"] == 3
    assert held["quantity"] == "6"
    assert Decimal(held["entry_cost"]) == Decimal("2.82")
    assert store.trading_record("paper_account")["BTC"] == "6"
    assert "3/3 buys" in trader.watch[0]["status"]
    assert held["pnl_tracking"]["from_entry"]
    store.close()


@pytest.mark.asyncio
async def test_scale_in_waits_between_buys(tmp_path):
    store = Store(str(tmp_path / "gap.db"))
    rest = entry_rest()
    trader = feed_trader(store, PaperExchange(store, rest), mode="paper", account_identity="paper")
    await trader.cycle()
    await trader.save_settings(rules(max_entries=2, reentry_gap_sec=60))
    await trader.control(True, True)
    await trader.cycle()
    await trader.cycle()
    held = trader.positions()[0]
    assert held["entries"] == 1 and held["quantity"] == "2"
    assert "next buy allowed" in trader.watch[0]["status"]
    store.close()


@pytest.mark.asyncio
async def test_daily_loss_limit_blocks_new_bets(tmp_path):
    store = Store(str(tmp_path / "limit.db"))
    trader = feed_trader(store, PaperExchange(store, entry_rest()), mode="paper", account_identity="paper",
                         daily_loss_limit=Decimal("2"))
    await trader.cycle()
    assert not any("Daily loss" in b for b in trader.blockers())
    trader.save_position(dict(position(), status="closed", quantity="0", net_pnl="-2.10",
                              closed_ms=int(time.time() * 1000)))
    assert any("Daily loss limit" in b for b in trader.blockers())
    with pytest.raises(ValueError, match="Daily loss"):
        await trader.control(True, True)
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,side", [("paper", "no"), ("live", "yes")])
async def test_observed_pnl_dip_recovery_and_settlement_persist_across_restart(tmp_path, mode, side):
    path = str(tmp_path / "range.db")
    store = Store(path)
    rest = AsyncMock()
    rest.get_market.return_value = {"market": {"status": "active"}}
    rest.get_positions.return_value = {"market_positions": [
        {"ticker": "BTC", "position_fp": "-2" if side == "no" else "2"},
    ]}
    trader = AutoTrader(store, rest, mode=mode, execution_allowed=True, account_identity="test-account")
    held = position(side)
    held["entry_cost"] = "1.00"
    held["policy"] = {"budget": "2", "take_profit": "0", "stop_loss": "0"}
    held["pnl_tracking"] = {"started_ms": 1000, "from_entry": True,
                            "samples": 0, "unavailable_samples": 0, "low": None, "high": None}
    # Value both bid levels, not the whole position at the best bid.
    rest.get_market_orderbook.return_value = {"orderbook_fp": {
        f"{side}_dollars": [["0.45", "1"], ["0.34", "1"]],
    }}
    await trader.monitor(held)
    assert Decimal(held["pnl_tracking"]["low"]["net_pnl"]) == Decimal("-0.25")
    low = dict(held["pnl_tracking"]["low"])
    rest.get_market_orderbook.return_value = {"orderbook_fp": {f"{side}_dollars": [["0.65", "2"]]}}
    await trader.monitor(held)
    assert Decimal(held["pnl_tracking"]["high"]["net_pnl"]) == Decimal("0.26")
    assert held["pnl_tracking"]["low"] == low
    high = dict(held["pnl_tracking"]["high"])
    await trader.monitor(held)
    assert held["pnl_tracking"]["high"] == high
    store.close()
    store = Store(path)
    restarted = AutoTrader(store, rest, mode=mode, execution_allowed=True, account_identity="test-account")
    held = restarted.positions()[0]
    assert held["pnl_tracking"]["started_ms"] == 1000
    rest.get_market.return_value = {"market": {"status": "finalized", "result": side}}
    await restarted.monitor(held)
    tracking = restarted.snapshot()["positions"][0]["pnl_tracking"]
    assert tracking["low"] == low
    assert Decimal(tracking["high"]["net_pnl"]) == Decimal("1.00")
    assert tracking["high"]["source"] == "settlement"
    assert tracking["samples"] == 4
    assert held["status"] == "closed"
    rest.create_event_order.assert_not_called()
    store.close()


@pytest.mark.asyncio
async def test_legacy_position_range_is_partial_and_no_liquidity_is_not_zero_pnl(tmp_path):
    store = Store(str(tmp_path / "unavailable.db"))
    rest = AsyncMock()
    rest.get_market.return_value = {"market": {"status": "active"}}
    rest.get_positions.return_value = {"market_positions": [{"ticker": "BTC", "position_fp": "2"}]}
    rest.get_market_orderbook.return_value = {"orderbook_fp": {"yes_dollars": []}}
    trader = AutoTrader(store, rest, execution_allowed=True, account_identity="test-account")
    held = position()
    await trader.monitor(held)
    await trader.monitor(held)
    tracking = trader.positions()[0]["pnl_tracking"]
    assert not tracking["from_entry"]
    assert tracking["samples"] == 0
    assert tracking["unavailable_samples"] == 2
    assert tracking["low"] is None and tracking["high"] is None
    assert held["net_pnl"] is None
    rest.create_event_order.assert_not_called()
    store.close()


@pytest.mark.asyncio
async def test_scalp_range_includes_prior_exit_credit_and_price_dependent_fees(tmp_path):
    store = Store(str(tmp_path / "scalp-range.db"))
    rest = AsyncMock()
    rest.get_market.return_value = {"market": {"status": "active"}}
    rest.get_positions.return_value = {"market_positions": [{"ticker": "BTC", "position_fp": "2"}]}
    rest.get_market_orderbook.return_value = {"orderbook_fp": {"yes_dollars": [["0.90", "2"]]}}
    trader = AutoTrader(store, rest, execution_allowed=True, account_identity="test-account")
    held = position()
    held.update(entry_cost="2.00", exit_credit="0.30",
                policy={"budget": "3", "take_profit": "0.50", "stop_loss": "0.50"},
                scalp={"market_loss_limit": "0.50"})
    await trader.monitor(held)
    assert Decimal(held["net_pnl"]) == Decimal("0.08")
    assert Decimal(held["pnl_tracking"]["low"]["net_pnl"]) == Decimal("0.08")
    assert held["pnl_tracking"]["low"] == held["pnl_tracking"]["high"]
    rest.create_event_order.assert_not_called()
    store.close()
