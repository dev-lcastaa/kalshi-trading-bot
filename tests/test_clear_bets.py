import json

from kalshi_bot.clear_bets import main, open_bets
from kalshi_bot.data.store import Store


def seed(store):
    store.save_trading_record("settings:paper", "settings", {"rules": []})
    store.save_trading_record("settings:live", "settings", {"rules": []})
    store.save_trading_record("position:paper:A", "position:paper", {"ticker": "A", "status": "closed"})
    store.save_trading_record("position:prod:B", "position:prod", {"ticker": "B", "status": "closed"})
    store.save_trading_record("paper_order:1", "paper_order", {"order": {}, "fills": []})
    store.save_trading_record("paper_account", "paper_account", {"A": "1"})
    store.record_trading_event("buy_submitted", "x", mode="paper")
    store.record_trading_event("buy_submitted", "y", mode="live")


def test_clear_paper_keeps_settings_and_live_and_writes_backup(tmp_path):
    db, backup = tmp_path / "t.db", tmp_path / "backup.json"
    store = Store(str(db))
    seed(store)
    store.close()
    assert main(["--mode", "paper", "--database", str(db), "--backup", str(backup), "--yes"]) == 0
    saved = json.loads(backup.read_text())
    assert {r["record_key"] for r in saved["records"]} == {"position:paper:A", "paper_order:1", "paper_account"}
    assert [e["reason"] for e in saved["events"]] == ["x"]
    store = Store(str(db))
    assert store.trading_records("position:paper") == [] and store.trading_record("paper_account") is None
    assert store.trading_record("settings:paper") is not None and store.trading_record("settings:live") is not None
    assert len(store.trading_records("position:prod")) == 1
    assert [e["reason"] for e in store.trading_events()] == ["y"]
    store.close()


def test_refuses_while_bets_are_running_unless_forced(tmp_path):
    db = tmp_path / "t.db"
    store = Store(str(db))
    store.save_trading_record("position:paper:A", "position:paper", {"ticker": "A", "status": "open"})
    assert open_bets(store, "paper") == 1
    store.close()
    args = ["--mode", "paper", "--database", str(db), "--backup", str(tmp_path / "b.json"), "--yes"]
    assert main(args) == 1
    assert main([*args, "--force"]) == 0
