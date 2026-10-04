from decimal import Decimal

import pytest

from kalshi_bot.trading import TradingPolicy, dollars, fee_reserve
from kalshi_bot.config import Settings


def test_one_dollar_budget_includes_fees_and_target_is_reachable():
    policy = TradingPolicy()
    assert policy.entry_count(Decimal("0.45")) == 2
    assert Decimal("0.45") * 2 + fee_reserve(2) <= policy.budget
    assert policy.entry_count(Decimal("0.75")) == 0
    assert policy.entry_count(Decimal("0.50")) == 0


def test_thresholds_are_net_dollars_not_percentages():
    policy = TradingPolicy()
    assert policy.exit_reason(Decimal("0.49")) is None
    assert policy.exit_reason(Decimal("0.50")) == "take_profit"
    assert policy.exit_reason(Decimal("-0.09")) is None
    assert policy.exit_reason(Decimal("-0.10")) == "stop_loss"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_money_rejected(value):
    with pytest.raises(ValueError):
        dollars(value)


@pytest.mark.parametrize("kwargs", [
    {"budget": Decimal("2")}, {"budget": Decimal("0")},
    {"take_profit": Decimal("0.001")}, {"stop_loss": Decimal("1")},
])
def test_unsafe_policy_rejected(kwargs):
    with pytest.raises(ValueError):
        TradingPolicy(**kwargs)


@pytest.mark.parametrize("ask", ["0", "1", "-1", "NaN"])
def test_invalid_entry_quotes_are_skipped(ask):
    assert TradingPolicy().entry_count(Decimal(ask)) == 0


@pytest.mark.parametrize("profit,loss", [("0.50", "0.10"), ("0.25", "0.05")])
def test_custom_thresholds_loaded_from_environment(monkeypatch, profit, loss):
    monkeypatch.setenv("KALSHI_ENV", "demo")
    monkeypatch.setenv("KALSHI_TRADE_BUDGET_USD", "1.50")
    monkeypatch.setenv("KALSHI_TAKE_PROFIT_USD", profit)
    monkeypatch.setenv("KALSHI_STOP_LOSS_USD", loss)
    policy = Settings.load().trading_policy
    assert policy.budget == Decimal("1.50")
    assert policy.take_profit == Decimal(profit)
    assert policy.stop_loss == Decimal(loss)
    assert policy.exit_reason(Decimal(profit) - Decimal("0.01")) is None
    assert policy.exit_reason(Decimal(profit)) == "take_profit"
    assert policy.exit_reason(-Decimal(loss) + Decimal("0.01")) is None
    assert policy.exit_reason(-Decimal(loss)) == "stop_loss"


@pytest.mark.parametrize("value", ["0", "-0.10", "0.001", "NaN", "Infinity", "abc"])
def test_invalid_custom_threshold_rejected_on_load(monkeypatch, value):
    monkeypatch.setenv("KALSHI_ENV", "demo")
    monkeypatch.setenv("KALSHI_TRADE_BUDGET_USD", "1.00")
    monkeypatch.setenv("KALSHI_STOP_LOSS_USD", "0.10")
    monkeypatch.setenv("KALSHI_TAKE_PROFIT_USD", value)
    with pytest.raises((ValueError, ArithmeticError)):
        Settings.load()