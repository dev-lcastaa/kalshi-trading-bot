import json
import sqlite3
from dataclasses import replace

import pytest

from kalshi_bot.backtest.walk_forward import Observation, Quote, _trade_score, load_observations, walk_forward
from kalshi_bot.trading import EntryRule, dollars


def observation(i=0, **overrides):
    timestamp = 10_000_000 + i * 900_000
    quote = Quote(timestamp, 0.79, 0.80, 2, 2)
    values = dict(
        ticker=f"BTC-{i}", index_id="BRTI", ts_ms=timestamp, close_ts_ms=timestamp + 390000,
        outcome=float(i % 2), z=float(i % 3 - 1), minutes_left=6.5,
        quote=quote, executions={0: quote, 2000: replace(quote, ts_ms=timestamp + 2000),
                                10000: replace(quote, ts_ms=timestamp + 10000)},
        label_available_ms=timestamp + 391000,
    )
    values.update(overrides)
    return Observation(**values)


def test_folds_train_only_on_previously_settled_markets_and_keep_simultaneous_markets_together():
    items = []
    for i in range(12):
        item = observation(i)
        items.extend([item, replace(item, ticker=f"SOL-{i}", index_id="SOLUSD_RTI")])
    report = walk_forward(items, min_train=4, fold_size=2)
    assert all(fold["test_markets"] == 4 for fold in report["folds"])
    assert all(fold["last_train_close_ms"] < fold["decision_start_ms"]
               for fold in report["folds"] if fold["train_markets"])
    assert report["models"]["frozen_fair_value"]["overall"]["markets"] == 24
    assert report["models"]["walk_forward_logistic"]["overall"]["markets"] == 20
    assert report["promotion_allowed"] is False
    json.dumps(report, allow_nan=False)


def test_future_labels_do_not_change_an_earlier_fold_predictions():
    items = [observation(i) for i in range(8)]
    first = walk_forward(items[:4], min_train=2, fold_size=2)
    later = walk_forward(items[:4] + [replace(item, outcome=1 - item.outcome) for item in items[4:]],
                         min_train=2, fold_size=2)
    assert first["folds"] == later["folds"][:2]
    assert "walk_forward_logistic" in first["folds"][1]["scores"]


def test_open_training_markets_are_embargoed_even_if_their_decision_precedes_test():
    first = observation(0, close_ts_ms=observation(2).ts_ms + 100, label_available_ms=observation(2).ts_ms + 1000)
    report = walk_forward([first, observation(1), observation(2), observation(3)], min_train=2, fold_size=1)
    fold = report["folds"][2]
    assert fold["train_markets"] == 1
    assert not fold["challengers_fitted"]


def test_outcomes_not_yet_recorded_cannot_train_challengers():
    items = [observation(0, label_available_ms=observation(3).ts_ms), observation(1),
             observation(2, label_available_ms=None), observation(3)]
    report = walk_forward(items, min_train=2, fold_size=1)
    assert report["folds"][2]["train_markets"] == 1
    assert report["folds"][3]["train_markets"] == 1


@pytest.mark.parametrize("bad", [
    {"outcome": 2}, {"z": float("nan")}, {"minutes_left": 0.5},
    {"quote": Quote(10_000_001, 0.79, 0.80, 2, 2)},
    {"executions": {0: Quote(10_000_001, 0.79, 0.80, 2, 2)}},
])
def test_invalid_or_future_observations_are_rejected(bad):
    with pytest.raises(ValueError):
        walk_forward([observation(**bad)])


def test_duplicate_market_samples_are_not_counted_as_independent_trades():
    with pytest.raises(ValueError, match="one decision"):
        walk_forward([observation(), observation()])


def test_trade_score_uses_asks_exact_quadratic_fees_and_delay_not_midpoints():
    item = observation(outcome=1)
    rows = [{"observation": item, "p": 0.95}]
    rule = EntryRule(min_price=dollars("0.60"), min_edge=dollars("0.03"))
    score = _trade_score(rows, 0, rule, buffer=0.02, slippage=0.01, max_spread=0.03)
    assert score["trades"] == 1
    assert score["fees"] == pytest.approx(0.02)
    assert score["net_pnl"] == pytest.approx(0.17)
    delayed = replace(item, executions={10000: Quote(item.ts_ms + 10000, 0.93, 0.94, 2, 2)})
    score = _trade_score([{"observation": delayed, "p": 0.95}], 10000, rule, 0.02, 0.01, 0.03)
    assert score["trades"] == 0
    assert score["excluded_entries"]["edge_or_rule_expired"] == 1


def test_unknown_size_is_excluded_not_assumed_to_fill():
    item = observation(outcome=1, executions={0: Quote(10_000_000, 0.79, 0.80, None, None)})
    score = _trade_score([{"observation": item, "p": 0.95}], 0, EntryRule(), 0, 0, 0)
    assert score["trades"] == 0
    assert score["excluded_entries"] == {"unknown_or_insufficient_size": 1}


def test_loader_is_as_of_and_read_only(tmp_path):
    path = tmp_path / "history.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE markets(ticker, index_id, strike, close_ts_ms, result, outcome_checked_at_ms);
        CREATE TABLE index_ticks(index_id, ts_ms, value);
        CREATE TABLE market_ticks(market_ticker, ts_ms, yes_bid_dollars, yes_ask_dollars, yes_bid_size, yes_ask_size);
    """)
    now = 10_000_000
    connection.execute("INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?)", ("BTC", "BRTI", 100, now + 390000, "yes", now + 391000))
    connection.executemany("INSERT INTO index_ticks VALUES (?, ?, ?)", [
        ("BRTI", now - second * 1000, 100 * (1.001 if (second // 60) % 2 else 0.999))
        for second in range(1860, -1, -1)
    ])
    connection.executemany("INSERT INTO market_ticks VALUES (?, ?, ?, ?, ?, ?)", [
        ("BTC", now - 1000, .79, .80, 2, 2), ("BTC", now + 2000, .89, .90, 2, 2),
        ("BTC", now + 10001, .99, 1, 2, 2),
    ])
    connection.commit()
    connection.close()
    before = path.read_bytes()
    items, excluded = load_observations(str(path))
    assert not excluded
    assert len(items) == 1
    assert items[0].quote.ask == .80
    assert items[0].executions[2000].ask == .90
    assert items[0].executions[10000] is None  # Last usable quote is too old; no lookahead.
    assert path.read_bytes() == before
