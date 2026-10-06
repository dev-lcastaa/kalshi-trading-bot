"""Import legacy CSV exports and report data quality.

    python -m aqlabs.store.cli import --csv-dir C:\\data\\exports --root C:\\data\\eventstore
    python -m aqlabs.store.cli report --root C:\\data\\eventstore
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from .event_store import EventStore
from .quality import feed_report, quote_report
from .schema import TABLES


def _fmt(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%Y-%m-%d %H:%M") if ms else "-"


def cmd_import(args) -> None:
    store = EventStore(args.root)
    for name in TABLES:
        path = Path(args.csv_dir) / f"{name}.csv.gz"
        if not path.exists():
            path = Path(args.csv_dir) / f"{name}.csv"
        if not path.exists():
            print(f"skip {name}: no CSV found")
            continue
        print(store.import_csv(name, path, since_ms=args.since_ms, until_ms=args.until_ms))
    print("manifest:", store.write_manifest())


def cmd_report(args) -> None:
    store = EventStore(args.root)
    print("== manifest")
    for name, m in store.manifest().items():
        print(f"{name:15} rows={m['rows']:>9,} {_fmt(m['min_ms'])} -> {_fmt(m['max_ms'])} fp={m['fingerprint']}")
    print("== feeds (per-feed gap limit%s)" % (f", overridden to {args.max_gap_sec}s" if args.max_gap_sec else ""))
    for r in feed_report(store, args.max_gap_sec * 1000 if args.max_gap_sec else None):
        print(f"{r['feed']:22} ticks={r['ticks']:>9,} limit={r['limit_sec']:>3}s gaps={r['gaps']:>4} "
              f"lost_h={r['gap_hours']:>6} longest_min={r['longest_gap_min']:>6} uptime={r['uptime']:.4f}")
    print("== quotes", quote_report(store))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("import")
    a.add_argument("--csv-dir", required=True)
    a.add_argument("--root", required=True)
    a.add_argument("--since-ms", type=int, help="only import rows after this time (fills a gap without duplicates)")
    a.add_argument("--until-ms", type=int, help="only import rows before this time")
    a.set_defaults(fn=cmd_import)
    b = sub.add_parser("report")
    b.add_argument("--root", required=True)
    b.add_argument("--max-gap-sec", type=int, default=None,
                   help="apply one gap limit to every feed instead of the per-feed limits")
    b.set_defaults(fn=cmd_report)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
