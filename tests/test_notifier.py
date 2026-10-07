from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from kalshi_bot.auto_trader import AutoTrader
from kalshi_bot.data.store import Store
from kalshi_bot.notifier import DiscordNotifier, result_embed, trade_embed, valid_webhook

TRADE_URL = "https://discord.com/api/webhooks/1/trade-token"
RESULT_URL = "https://discord.com/api/webhooks/2/result-token"


def held(**overrides):
    position = {
        "ticker": "KXBTC15M-X", "side": "yes", "status": "open", "quantity": "2", "rule": "momentum",
        "account_identity": "test-account", "entry_cost": "0.94", "exit_credit": "0", "pending": None,
        "net_pnl": None, "policy": {"budget": "1", "take_profit": "0", "stop_loss": "0"},
        "entry_signal": {"confidence": "0.72"},
        "execution_history": [{"action": "buy", "order_id": "o1", "filled": "2", "gross": "0.90",
                               "fees": "0.04", "average_price": "0.45"}],
    }
    position.update(overrides)
    return position


def test_only_discord_webhook_urls_are_accepted():
    assert valid_webhook(f" {TRADE_URL} ") == TRADE_URL
    assert valid_webhook("https://evil.example/api/webhooks/1/x") == ""
    assert valid_webhook("") == ""


def test_embeds_describe_the_trade_and_the_result():
    trade = trade_embed(held(), "prod")
    values = {f["name"]: f["value"] for f in trade["fields"]}
    assert values["Side"] == "YES" and values["Contracts"] == "2" and values["Confidence"] == "72%"
    assert trade["footer"]["text"] == "Order o1"
    loss = result_embed(held(net_pnl="-0.94", exit_credit="0", result="no", closed_by="settled"), "prod", Decimal("-1"))
    assert loss["title"].startswith("LOSS") and {f["name"]: f["value"] for f in loss["fields"]}["Net P&L"] == "-$0.94"
    win = result_embed(held(net_pnl="1.06", exit_credit="2", result="yes", closed_by="settled"), "prod")
    assert win["title"].startswith("WIN") and win["color"] != loss["color"]


@pytest.mark.asyncio
async def test_settlement_goes_to_the_results_channel_only(tmp_path):
    rest = AsyncMock()
    rest.get_market.return_value = {"market": {"status": "finalized", "result": "yes"}}
    store = Store(str(tmp_path / "settle.db"))
    notifier = DiscordNotifier(TRADE_URL, RESULT_URL)
    notifier._send = MagicMock()
    trader = AutoTrader(store, rest, execution_allowed=True, account_identity="test-account", notifier=notifier)
    await trader.monitor(held())
    notifier._send.assert_called_once()
    url, embed = notifier._send.call_args.args
    assert url == RESULT_URL and embed["title"].startswith("WIN")
    store.close()


@pytest.mark.asyncio
async def test_buy_fill_goes_to_the_trade_channel_only(tmp_path):
    rest = AsyncMock()
    rest.get_order.return_value = {"order": {"ticker": "KXBTC15M-X", "status": "executed",
                                             "fill_count_fp": "2", "client_order_id": "c1"}}
    rest.get_fills.return_value = {"fills": [{"fill_id": "f", "order_id": "o1", "ticker": "KXBTC15M-X",
                                              "count_fp": "2", "yes_price_dollars": "0.45", "fee_cost": "0.04"}]}
    store = Store(str(tmp_path / "buy.db"))
    notifier = DiscordNotifier(TRADE_URL, RESULT_URL)
    notifier._send = MagicMock()
    trader = AutoTrader(store, rest, execution_allowed=True, account_identity="test-account", notifier=notifier)
    pending = held(status="pending", quantity="0", entry_cost="0", execution_history=[])
    pending["pending"] = {"client_order_id": "c1", "order_id": "o1", "action": "buy", "quantity": "2",
                          "reason": "test", "ack_fill_count": "2"}
    await trader.reconcile(pending)
    notifier._send.assert_called_once()
    url, embed = notifier._send.call_args.args
    assert url == TRADE_URL and embed["title"].startswith("Trade placed")
    store.close()


@pytest.mark.asyncio
async def test_paper_trades_are_never_announced(tmp_path):
    store = Store(str(tmp_path / "paper.db"))
    notifier = DiscordNotifier(TRADE_URL, RESULT_URL)
    notifier._send = MagicMock()
    trader = AutoTrader(store, AsyncMock(), mode="paper", account_identity="paper", notifier=notifier)
    trader.notify_closed(held(net_pnl="1", result="yes", closed_by="settled"))
    notifier._send.assert_not_called()
    store.close()


@pytest.mark.asyncio
async def test_a_discord_outage_never_raises(tmp_path):
    notifier = DiscordNotifier(TRADE_URL, "")
    notifier.trade(trade_embed(held(), "demo"))
    notifier.settlement({"title": "ignored: no URL configured"})
    for task in list(notifier._tasks):
        task.cancel()
