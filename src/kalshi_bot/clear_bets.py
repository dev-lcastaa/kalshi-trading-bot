"""Clear one trading mode's placed bets and diary so results can be measured from a clean slate.

Saved strategy settings are kept. A JSON backup is written before anything is deleted.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .data.store import Store


def open_bets(store: Store, mode: str) -> int:
    kinds = ["position:paper"] if mode == "paper" else ["position:demo", "position:prod"]
    return sum(position.get("status") in ("pending", "open")
               for kind in kinds for position in store.trading_records(kind))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["paper", "live"], required=True,
                        help="paper = practice bets; live = real-money (or Kalshi demo) bets")
    parser.add_argument("--database", default=os.environ.get("DATABASE_URL", "./data/kalshi_bot.db"))
    parser.add_argument("--backup", help="where to write the JSON backup (default: ./data/bets_backup_<mode>_<time>.json)")
    parser.add_argument("--yes", action="store_true", help="delete without asking for confirmation")
    parser.add_argument("--force", action="store_true", help="clear even if bets are still running (not recommended)")
    args = parser.parse_args(argv)

    store = Store(args.database)
    try:
        running = open_bets(store, args.mode)
        if running and not args.force:
            print(f"{running} {args.mode} bet(s) are still running. Turn the bot off and let them finish, "
                  "or pass --force.", file=sys.stderr)
            return 1
        if not args.yes and input(f"Delete ALL {args.mode} bets and diary entries? Type 'clear' to confirm: ") != "clear":
            print("Cancelled.")
            return 1
        backup_path = Path(args.backup or f"./data/bets_backup_{args.mode}_{int(time.time())}.json")

        def write_backup(data: dict) -> None:
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.write_text(json.dumps(data, indent=2))

        counts = store.clear_trading_history(args.mode, backup=write_backup)
        print(f"Cleared {counts['records']} record(s) and {counts['events']} diary event(s) for {args.mode}. "
              f"Backup: {backup_path}")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
