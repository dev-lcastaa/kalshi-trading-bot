"""Public Kalshi market/candle backfill into an isolated research SQLite file."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

import httpx

from ..kalshi_client.rest import KalshiRestClient

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
SERIES = ("KXBTC15M", "KXSOL15M")
APPLICATION_ID = 1262634578


def timestamp(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return int(parsed.timestamp())


def number(value: object) -> float | None:
    if value is None or value == "":
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite candle value")
    return result


def normalize_candle(candle: dict) -> tuple:
    def close(side: str) -> float | None:
        values = candle.get(side) or {}
        return number(values.get("close_dollars", values.get("close")))

    return (
        int(candle["end_period_ts"]), close("yes_bid"), close("yes_ask"), close("price"),
        number(candle.get("volume_fp", candle.get("volume"))),
        number(candle.get("open_interest_fp", candle.get("open_interest"))),
        json.dumps(candle, allow_nan=False),
    )


def market_quality(market: dict, series: str) -> list[str]:
    issues = []
    if market.get("strike_type") != "greater_or_equal" or not market.get("rules_primary"):
        issues.append("unsupported_or_missing_rules")
    digits = (market.get("custom_strike") or {}).get("round_digits")
    if str(digits) != ("2" if series == "KXBTC15M" else "4"):
        issues.append("unexpected_rounding")
    try:
        goal = Decimal(str(market.get("floor_strike")))
        value = str(market.get("expiration_value"))
        if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", value):
            value = value.replace(",", "")
        settlement = Decimal(value)
        if not goal.is_finite() or not settlement.is_finite() or goal <= 0 or settlement <= 0:
            raise InvalidOperation
        if (settlement >= goal) != (market["result"] == "yes"):
            issues.append("settlement_result_mismatch")
    except InvalidOperation:
        issues.append("missing_or_invalid_goal_or_settlement")
    return issues


class BackfillClient(KalshiRestClient):
    async def cutoff(self) -> dict:
        return await self._get("/historical/cutoff")

    async def batch_candles(self, tickers: list[str], start: int, end: int) -> dict:
        return await self._get("/markets/candlesticks", {
            "market_tickers": ",".join(tickers), "start_ts": start, "end_ts": end,
            "period_interval": 1,
        })

    async def markets(self, series: str, start: int, end: int, historical: bool, max_pages: int = 100):
        cursor = None
        cursors = set()
        for _ in range(max_pages):
            params = {"series_ticker": series, "limit": 1000}
            if not historical:
                params.update(min_close_ts=start, max_close_ts=end)
            if cursor:
                params["cursor"] = cursor
            page = await self._get("/historical/markets" if historical else "/markets", params)
            for market in page["markets"]:
                if start < timestamp(market["close_time"]) <= end:
                    yield market
            cursor = page.get("cursor")
            if not cursor:
                return
            if cursor in cursors:
                raise RuntimeError("Repeated market pagination cursor")
            cursors.add(cursor)
        raise RuntimeError("Market pagination limit reached; narrow the date range")

    async def candles(self, series: str, ticker: str, start: int, end: int, historical: bool) -> dict:
        ticker = quote(ticker, safe="")
        paths = [f"/series/{series}/markets/{ticker}/candlesticks", f"/historical/markets/{ticker}/candlesticks"]
        if historical:
            paths.reverse()
        params = {"start_ts": start, "end_ts": end, "period_interval": 1}
        try:
            return await self._get(paths[0], params)
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                raise
            return await self._get(paths[1], params)


class ResearchStore:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        existing = self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        identity = self.connection.execute("PRAGMA application_id").fetchone()[0]
        if existing and identity != APPLICATION_ID:
            self.connection.close()
            raise ValueError("Refusing to write to a non-research database (including the live bot database)")
        self.connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS research_markets (
                ticker TEXT PRIMARY KEY, series TEXT NOT NULL, open_ts INTEGER NOT NULL,
                close_ts INTEGER NOT NULL, result TEXT NOT NULL, source TEXT NOT NULL,
                fetched_at INTEGER NOT NULL, raw_json TEXT NOT NULL,
                candle_status TEXT NOT NULL DEFAULT 'pending', error TEXT
            );
            CREATE TABLE IF NOT EXISTS research_candles (
                ticker TEXT NOT NULL, end_ts INTEGER NOT NULL,
                yes_bid REAL, yes_ask REAL, trade_price REAL, volume REAL, open_interest REAL,
                raw_json TEXT NOT NULL, PRIMARY KEY(ticker, end_ts)
            );
            CREATE TABLE IF NOT EXISTS research_runs (
                run_id INTEGER PRIMARY KEY, started_at INTEGER, start_ts INTEGER, end_ts INTEGER,
                source_url TEXT, cutoff_json TEXT, finished_at INTEGER, summary_json TEXT
            );
            CREATE TABLE IF NOT EXISTS research_market_quality (
                ticker TEXT PRIMARY KEY, issues_json TEXT NOT NULL
            );
            CREATE VIEW IF NOT EXISTS research_examples AS
                WITH horizons(lead_sec) AS (VALUES(360),(180),(60))
                SELECT m.ticker, m.series, m.close_ts, m.result, h.lead_sec,
                       m.close_ts-h.lead_sec AS decision_ts, c.end_ts AS candle_end_ts,
                       c.yes_bid, c.yes_ask, (c.yes_bid+c.yes_ask)/2.0 AS market_p_yes,
                       c.volume, c.open_interest
                FROM research_markets m CROSS JOIN horizons h JOIN research_candles c
                  ON c.ticker=m.ticker AND c.end_ts=(
                      SELECT MAX(previous.end_ts) FROM research_candles previous
                      WHERE previous.ticker=m.ticker AND previous.end_ts<=m.close_ts-h.lead_sec
                  )
                WHERE m.close_ts-h.lead_sec>=m.open_ts
                  AND c.end_ts>m.open_ts AND m.close_ts-h.lead_sec-c.end_ts<60
                  AND c.yes_bid>=0 AND c.yes_ask<=1 AND c.yes_bid<=c.yes_ask;
                        CREATE VIEW IF NOT EXISTS research_training_examples AS
                                SELECT e.* FROM research_examples e
                                JOIN research_market_quality q ON q.ticker=e.ticker
                                WHERE q.issues_json='[]';
        """)

    def close(self):
        self.connection.close()

    def save_market(self, market: dict, series: str, source: str):
        self.connection.execute("""
            INSERT INTO research_markets(ticker,series,open_ts,close_ts,result,source,fetched_at,raw_json)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET
            result=excluded.result, source=excluded.source, fetched_at=excluded.fetched_at,
            raw_json=excluded.raw_json
        """, (market["ticker"], series, timestamp(market["open_time"]), timestamp(market["close_time"]),
              market["result"], source, int(time.time()), json.dumps(market, allow_nan=False)))
        self.connection.execute("""
            INSERT INTO research_market_quality VALUES(?,?) ON CONFLICT(ticker)
            DO UPDATE SET issues_json=excluded.issues_json
        """, (market["ticker"], json.dumps(market_quality(market, series))))
        self.connection.commit()

    def complete(self, ticker: str) -> bool:
        row = self.connection.execute("SELECT candle_status FROM research_markets WHERE ticker=?", (ticker,)).fetchone()
        return row is not None and row[0] == "complete"

    def save_candles(self, ticker: str, candles: list[dict], start: int, end: int):
        normalized = [normalize_candle(candle) for candle in candles]
        normalized = [row for row in normalized if start < row[0] <= end]
        expected = set(range((start // 60 + 1) * 60, end + 1, 60))
        observed = {row[0] for row in normalized}
        status = "complete" if expected and expected == observed else "sparse" if observed else "empty"
        with self.connection:
            self.connection.execute("DELETE FROM research_candles WHERE ticker=?", (ticker,))
            self.connection.executemany("INSERT OR REPLACE INTO research_candles VALUES(?,?,?,?,?,?,?,?)",
                                        [(ticker, *row) for row in normalized])
            self.connection.execute("UPDATE research_markets SET candle_status=?,error=NULL WHERE ticker=?", (status, ticker))

    def summary(self, start: int, end: int) -> dict:
        markets = self.connection.execute("""
            SELECT series,COUNT(*),MIN(close_ts),MAX(close_ts),
                   SUM(candle_status='complete'),SUM(candle_status='sparse'),
                   SUM(candle_status='empty'),SUM(candle_status='error')
            FROM research_markets WHERE close_ts>? AND close_ts<=? GROUP BY series
        """, (start, end)).fetchall()
        examples = self.connection.execute("""
            SELECT series,lead_sec,COUNT(*),AVG((market_p_yes-(result='yes'))*(market_p_yes-(result='yes')))
            FROM research_examples WHERE close_ts>? AND close_ts<=? GROUP BY series,lead_sec
        """, (start, end)).fetchall()
        candle_count = self.connection.execute("""
            SELECT COUNT(*) FROM research_candles c JOIN research_markets m ON m.ticker=c.ticker
            WHERE m.close_ts>? AND m.close_ts<=?
        """, (start, end)).fetchone()[0]
        return {
            "candles": candle_count,
            "quarantined_markets": self.connection.execute("""
                SELECT COUNT(*) FROM research_market_quality q JOIN research_markets m ON m.ticker=q.ticker
                WHERE m.close_ts>? AND m.close_ts<=? AND q.issues_json!='[]'
            """, (start, end)).fetchone()[0],
            "training_examples": self.connection.execute("""
                SELECT COUNT(*) FROM research_training_examples WHERE close_ts>? AND close_ts<=?
            """, (start, end)).fetchone()[0],
            "markets": [dict(zip(("series", "count", "first_close_ts", "last_close_ts", "complete", "sparse", "empty", "errors"), row)) for row in markets],
            "examples": [dict(zip(("series", "lead_sec", "count", "market_brier"), row)) for row in examples],
        }


async def backfill(client: BackfillClient, store: ResearchStore, start: int, end: int, max_markets: int = 10000) -> dict:
    if not 0 <= start < end or max_markets < 1:
        raise ValueError("Require 0 <= start < end and a positive market limit")
    cutoff = await client.cutoff()
    cutoff_ts = timestamp(cutoff["market_settled_ts"])
    run_id = store.connection.execute("""
        INSERT INTO research_runs(started_at,start_ts,end_ts,source_url,cutoff_json) VALUES(?,?,?,?,?)
    """, (int(time.time()), start, end, BASE_URL, json.dumps(cutoff))).lastrowid
    store.connection.commit()
    seen = set()
    skipped = failures = 0
    for series in SERIES:
        pending: dict[tuple[bool, int], list[dict]] = {}
        for historical in ([False, True] if start < cutoff_ts else [False]):
            async for market in client.markets(series, start, end, historical):
                ticker = market["ticker"]
                if ticker in seen or market.get("result") not in ("yes", "no"):
                    continue
                if not ticker.startswith(series + "-"):
                    raise ValueError("Unexpected series in market response")
                open_ts, close_ts = timestamp(market["open_time"]), timestamp(market["close_time"])
                if close_ts - open_ts != 900:
                    skipped += 1
                    continue
                if len(seen) >= max_markets:
                    raise RuntimeError("Market limit reached; rerun with a higher --max-markets or narrower dates")
                seen.add(ticker)
                store.save_market(market, series, "historical" if historical else "live")
                if store.complete(ticker):
                    continue
                archived = timestamp(market.get("settlement_ts") or market["close_time"]) < cutoff_ts
                pending.setdefault((archived, open_ts // 21600), []).append(market)
        processed = 0
        for (archived, _), records in sorted(pending.items()):
            batch = {}
            if not archived:
                try:
                    response = await client.batch_candles(
                        [record["ticker"] for record in records],
                        min(timestamp(record["open_time"]) for record in records),
                        max(timestamp(record["close_time"]) for record in records),
                    )
                    batch = {entry["market_ticker"]: entry["candlesticks"] for entry in response["markets"]}
                except (httpx.HTTPError, ValueError, KeyError):
                    batch = {}
            for record in records:
                ticker = record["ticker"]
                open_ts, close_ts = timestamp(record["open_time"]), timestamp(record["close_time"])
                try:
                    market_candles = batch.get(ticker)
                    if not market_candles:
                        response = await client.candles(series, ticker, open_ts, close_ts, archived)
                        market_candles = response["candlesticks"]
                    store.save_candles(ticker, market_candles, open_ts, close_ts)
                except (httpx.HTTPError, ValueError, KeyError) as error:
                    failures += 1
                    with store.connection:
                        store.connection.execute("UPDATE research_markets SET candle_status='error',error=? WHERE ticker=?", (str(error), ticker))
                processed += 1
            print(f"{series}: downloaded {processed} markets; {failures} candle failures", flush=True)
    result = dict(store.summary(start, end), discovered=len(seen), skipped_non_15m=skipped, request_failures=failures)
    with store.connection:
        store.connection.execute("UPDATE research_runs SET finished_at=?,summary_json=? WHERE run_id=?", (int(time.time()), json.dumps(result), run_id))
    return result


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="Exclusive market close boundary, ISO timestamp with timezone")
    parser.add_argument("--end", required=True, help="Inclusive market close boundary, ISO timestamp with timezone")
    parser.add_argument("--database", default="data/research_backfill.db")
    parser.add_argument("--max-markets", type=int, default=10000)
    args = parser.parse_args()
    start, end = timestamp(args.start), timestamp(args.end)
    if end > datetime.now(timezone.utc).timestamp():
        parser.error("--end must not be in the future")
    store = ResearchStore(args.database)
    try:
        async with BackfillClient(BASE_URL) as client:
            result = await backfill(client, store, start, end, args.max_markets)
        print(json.dumps(result, indent=2))
        if result["request_failures"]:
            raise SystemExit(1)
    finally:
        store.close()


if __name__ == "__main__":
    asyncio.run(main())