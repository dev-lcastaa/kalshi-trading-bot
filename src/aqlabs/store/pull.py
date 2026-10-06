"""Pull the collector's data from the server and build one research store (read-only on the server).

    python -m aqlabs.store.pull --legacy-root C:\\data\\eventstore --work C:\\data\\collector_pull

Every run rebuilds `<work>/combined` from scratch, so it is idempotent and cannot create duplicates:

    combined = legacy store (never modified)
             + the collector's Parquet files (docker exec tar of /data/eventstore)
             + the Postgres rows that fall in the gap between them (market_ticks, index_ticks, external_ticks) and
               the full `markets` table (docker exec psql COPY)

`registry.jsonl` is carried over from the previous combined store (or from the legacy store the first time), so the
Phase 3 record of what has been run survives a rebuild. Needs key-based `ssh` access and Docker on the host.
"""
from __future__ import annotations

import argparse
import gzip
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace

import duckdb

from .cli import cmd_report
from .event_store import EventStore
from .schema import TABLES

GAP_TABLES = ("market_ticks", "index_ticks", "external_ticks")
SKIP_NAMES = {"registry.jsonl", "MANIFEST.json", "collector_status.json"}
# Natural key per table; a repeated key after the rebuild means a bound was wrong.
DUPLICATE_KEYS = {"market_ticks": "market_ticker, ts_ms", "index_ticks": "index_id, ts_ms",
                  "external_ticks": "source, index_id, symbol, ts_ms", "trades": "trade_id"}


def ssh(host: str, command: str, stdout, check_codes=(0,)) -> None:
    """Run `command` on the host with its stdout going to a real file handle."""
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", host, command], stdout=stdout, stderr=subprocess.PIPE)
    if result.returncode not in check_codes:
        raise RuntimeError(f"ssh failed ({result.returncode}): {result.stderr.decode(errors='replace')[-500:]}")


def ssh_to_gzip(host: str, command: str, out: Path) -> None:
    """Stream the command's stdout through a pipe into a gzip file (a GzipFile cannot be handed to a subprocess:
    the child would write straight to the underlying file and skip the compression)."""
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(["ssh", "-o", "BatchMode=yes", host, command], stdout=subprocess.PIPE, stderr=err)
        with gzip.open(out, "wb") as fh:
            shutil.copyfileobj(proc.stdout, fh, length=1 << 20)
        code = proc.wait()
        if code != 0:
            err.seek(0)
            raise RuntimeError(f"ssh failed ({code}): {err.read().decode(errors='replace')[-500:]}")


def pull_collector(host: str, container: str, dest: Path) -> Path:
    """Stream the collector's event store out of its container into `dest` (tar exit code 1 = a file changed while
    being read, which happens when the collector writes or compacts; the files are written atomically so it is safe)."""
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest.parent / "collector_store.tgz"
    with archive.open("wb") as fh:
        ssh(host, f"docker exec {container} tar czf - --exclude=*.tmp -C /data/eventstore .", fh, check_codes=(0, 1))
    shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with tarfile.open(archive) as tar:
        tar.extractall(dest, filter="data")
    return archive


def export_postgres(host: str, container: str, sql: str, out: Path) -> None:
    command = f"docker exec {container} psql -U kalshi -d kalshi -c {shlex.quote(f'COPY ({sql}) TO STDOUT WITH CSV HEADER')}"
    ssh_to_gzip(host, command, out)


def table_bounds(store: EventStore, table: str) -> tuple[int | None, int | None]:
    m = store.manifest().get(table)
    return (m["min_ms"], m["max_ms"]) if m else (None, None)


def merge_tree(src: Path, dst: Path) -> int:
    """Copy every Parquet partition file from `src` into `dst` (names are unique, nothing is overwritten)."""
    copied = 0
    for file in src.rglob("*.parquet"):
        target = dst / file.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(file, target)
            copied += 1
    return copied


def duplicate_report(store: EventStore) -> dict[str, int]:
    con, out = store.connect(), {}
    for table, key in DUPLICATE_KEYS.items():
        if store.has_data(table):
            out[table] = int(con.execute(f"select count(*) - count(distinct ({key})) from {table}").fetchone()[0])
    return out


def build_combined(legacy: Path, collector_store: Path, gap_dir: Path, work: Path, previous: Path | None) -> Path:
    new = work / "combined.new"
    if new.exists():
        shutil.rmtree(new)
    shutil.copytree(legacy, new, ignore=shutil.ignore_patterns(*SKIP_NAMES))
    print(f"collector files merged: {merge_tree(collector_store, new)}")
    store = EventStore(new)
    legacy_store, collector = EventStore(legacy), EventStore(collector_store)
    for table in GAP_TABLES:
        csv = gap_dir / f"{table}.csv.gz"
        _, since = table_bounds(legacy_store, table)
        until, _ = table_bounds(collector, table)
        print(store.import_csv(table, csv, since_ms=since, until_ms=until), f"(gap {since} .. {until})")
    print(store.import_csv("markets", gap_dir / "markets.csv.gz"))
    registry = (previous / "registry.jsonl") if previous and (previous / "registry.jsonl").exists() \
        else legacy / "registry.jsonl"
    if registry.exists():
        shutil.copy2(registry, new / "registry.jsonl")
    dups = duplicate_report(store)
    print("duplicate keys per table (must all be 0):", dups)
    if any(dups.values()):
        raise RuntimeError(f"duplicate rows after the rebuild: {dups}; the previous combined store was left in place")
    store.write_manifest()
    return new


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--legacy-root", required=True, help="the original imported store (never modified)")
    ap.add_argument("--work", required=True)
    ap.add_argument("--host", default="lcastaa@192.168.1.208")
    ap.add_argument("--collector-container", default="aqlabs-kalshi-trading-bot-collector-1")
    ap.add_argument("--postgres-container", default="aqlabs-kalshi-trading-bot-postgres-1")
    ap.add_argument("--skip-report", action="store_true")
    args = ap.parse_args()
    work, legacy = Path(args.work), Path(args.legacy_root)
    work.mkdir(parents=True, exist_ok=True)

    collector_dir = work / "collector_store"
    print("pulling the collector's event store ...")
    archive = pull_collector(args.host, args.collector_container, collector_dir)
    print(f"  {archive.stat().st_size / 1e6:.1f} MB compressed")
    collector = EventStore(collector_dir)
    gap_dir = work / "gap_csv"
    gap_dir.mkdir(exist_ok=True)
    legacy_store = EventStore(legacy)
    for table in GAP_TABLES:
        _, since = table_bounds(legacy_store, table)
        until, _ = table_bounds(collector, table)
        where = f"ts_ms > {int(since)}" + (f" and ts_ms < {int(until)}" if until is not None else "")
        print(f"exporting {table} from Postgres where {where} ...")
        export_postgres(args.host, args.postgres_container, f"select * from {table} where {where} order by ts_ms",
                        gap_dir / f"{table}.csv.gz")
    export_postgres(args.host, args.postgres_container, "select * from markets order by close_ts_ms",
                    gap_dir / "markets.csv.gz")

    combined, previous = work / "combined", None
    if combined.exists():
        previous = work / "combined.prev"
        if previous.exists():
            shutil.rmtree(previous)
        combined.rename(previous)
    try:
        new = build_combined(legacy, collector_dir, gap_dir, work, previous)
    except Exception:
        if previous is not None:
            previous.rename(combined)  # keep the last good store
        raise
    new.rename(combined)
    if previous is not None:
        shutil.rmtree(previous)
    print(f"\ncombined research store: {combined}")
    if not args.skip_report:
        cmd_report(SimpleNamespace(root=str(combined), max_gap_sec=None))


if __name__ == "__main__":
    main()
