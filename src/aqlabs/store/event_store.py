"""Append-only Parquet event store with DuckDB for queries.

Layout: <root>/<table>/date=YYYY-MM-DD/part-*.parquet (UTC dates from the table's time column).
Writers only ever add new part files; they never rewrite existing ones. Dimension tables
(`replace=True`, e.g. markets) are replaced wholesale by an import.
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Iterable, Mapping

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from .schema import TABLES, Table

_ARROW = {"VARCHAR": pa.string(), "BIGINT": pa.int64(), "DOUBLE": pa.float64()}
_DATE_SQL = "strftime(epoch_ms({col}), '%Y-%m-%d')"


def _date_of_ms(ts_ms: int) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ts_ms / 1000, dt.UTC).strftime("%Y-%m-%d")


class EventStore:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ writing
    def table_dir(self, table: str) -> Path:
        return self.root / table

    def append(self, table: str, rows: Iterable[Mapping]) -> list[Path]:
        """Atomically add `rows` as new part files, one per UTC date. Returns the files written."""
        spec = TABLES[table]
        by_date: dict[str, list[Mapping]] = {}
        for row in rows:
            by_date.setdefault(_date_of_ms(int(row[spec.time_column])), []).append(row)
        written = []
        for date, group in by_date.items():
            arrays = {c: pa.array([r.get(c) for r in group], type=_ARROW[t]) for c, t in spec.columns.items()}
            folder = self.table_dir(table) / f"date={date}"
            folder.mkdir(parents=True, exist_ok=True)
            final = folder / f"part-{uuid.uuid4().hex}.parquet"
            tmp = final.with_suffix(".tmp")
            pq.write_table(pa.table(arrays), tmp, compression="zstd")
            os.replace(tmp, final)
            written.append(final)
        return written

    def import_csv(self, table: str, csv_path: str | os.PathLike) -> dict:
        """Load a CSV or CSV.GZ export (header row, Postgres COPY style) into `table`."""
        spec = TABLES[table]
        con = duckdb.connect()
        cols = ", ".join(f"'{c}': '{t}'" for c, t in spec.columns.items())
        src = (f"read_csv('{Path(csv_path).as_posix()}', header=true, nullstr='', columns={{{cols}}})")
        n_csv = con.execute(f"select count(*) from {src}").fetchone()[0]
        before = self.count(table)
        target = self.table_dir(table)
        if spec.replace and target.exists():
            shutil.rmtree(target)
            before = 0
        target.mkdir(parents=True, exist_ok=True)
        date_expr = _DATE_SQL.format(col=spec.time_column)
        order = ", ".join(spec.sort_by)
        con.execute(
            f"COPY (select *, {date_expr} as date from {src} order by {order}) TO '{target.as_posix()}' "
            f"(FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (date), FILENAME_PATTERN 'part-import-{{uuid}}', APPEND)"
        )
        after = self.count(table)
        if after - before != n_csv:
            raise RuntimeError(f"{table}: imported {after - before} rows but the CSV had {n_csv}")
        return {"table": table, "csv_rows": n_csv, "store_rows": after}

    # ------------------------------------------------------------------ reading
    def has_data(self, table: str) -> bool:
        return any(self.table_dir(table).glob("date=*/*.parquet"))

    def connect(self) -> duckdb.DuckDBPyConnection:
        """In-memory DuckDB connection with one view per table that has data."""
        con = duckdb.connect()
        for name in TABLES:
            if self.has_data(name):
                glob = (self.table_dir(name) / "date=*" / "*.parquet").as_posix()
                con.execute(f"create view {name} as select * exclude (date) from "
                            f"read_parquet('{glob}', hive_partitioning=true, hive_types_autocast=false)")
        return con

    def query(self, sql: str, params: list | None = None) -> dict:
        """Run SQL and return a dict of numpy arrays."""
        return self.connect().execute(sql, params or []).fetchnumpy()

    def count(self, table: str) -> int:
        if not self.has_data(table):
            return 0
        glob = (self.table_dir(table) / "date=*" / "*.parquet").as_posix()
        return duckdb.connect().execute(f"select count(*) from read_parquet('{glob}')").fetchone()[0]

    # ------------------------------------------------------------------ integrity
    def manifest(self) -> dict:
        """Row counts, time range and an order-independent content fingerprint per table."""
        out = {}
        con = self.connect()
        for name, spec in TABLES.items():
            if not self.has_data(name):
                continue
            cols = ", ".join(spec.columns)
            n, lo, hi, fp = con.execute(
                f"select count(*), min({spec.time_column}), max({spec.time_column}), "
                f"cast(bit_xor(hash({cols})) as varchar) from {name}"
            ).fetchone()
            out[name] = {"rows": n, "min_ms": lo, "max_ms": hi, "fingerprint": fp}
        return out

    def write_manifest(self) -> Path:
        path = self.root / "MANIFEST.json"
        path.write_text(json.dumps(self.manifest(), indent=2))
        return path
