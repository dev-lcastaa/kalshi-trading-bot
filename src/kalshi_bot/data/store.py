"""Storage for index ticks, market ticks, generated signals, and market lifecycle.

Supports two backends via the same portable SQL: SQLite (a plain file path -
used by the test suite and simple non-Docker runs) and PostgreSQL (a
`postgresql://` URL - used for the Docker Compose deployment). Both dialects
support the same standard `ON CONFLICT ... DO UPDATE/DO NOTHING` upsert syntax
used throughout, so almost no code branches on which backend is in use.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from pathlib import Path

import psycopg

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS index_ticks (
        index_id TEXT NOT NULL,
        ts_ms BIGINT NOT NULL,
        value DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (index_id, ts_ms)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS market_ticks (
        market_ticker TEXT NOT NULL,
        ts_ms BIGINT NOT NULL,
        price_dollars DOUBLE PRECISION,
        yes_bid_dollars DOUBLE PRECISION,
        yes_ask_dollars DOUBLE PRECISION,
        yes_bid_size DOUBLE PRECISION,
        yes_ask_size DOUBLE PRECISION,
        volume DOUBLE PRECISION,
        open_interest DOUBLE PRECISION,
        PRIMARY KEY (market_ticker, ts_ms)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS signals (
        ticker TEXT NOT NULL,
        ts_ms BIGINT NOT NULL,
        index_id TEXT,
        index_price DOUBLE PRECISION,
        strike DOUBLE PRECISION,
        seconds_to_expiry DOUBLE PRECISION,
        model_p_yes DOUBLE PRECISION,
        market_p_yes DOUBLE PRECISION,
        edge DOUBLE PRECISION,
        recommendation TEXT,
        confidence DOUBLE PRECISION,
        PRIMARY KEY (ticker, ts_ms)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS markets (
        ticker TEXT PRIMARY KEY,
        index_id TEXT,
        strike DOUBLE PRECISION,
        close_ts_ms BIGINT,
        first_seen_ts_ms BIGINT,
        last_seen_ts_ms BIGINT,
        status TEXT NOT NULL DEFAULT 'active',
        closed_at_ms BIGINT,
        result TEXT,
        outcome_checked_at_ms BIGINT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decisions (
        ticker TEXT PRIMARY KEY,
        ts_ms BIGINT NOT NULL,
        seconds_to_expiry DOUBLE PRECISION,
        index_price DOUBLE PRECISION,
        strike DOUBLE PRECISION,
        model_p_yes DOUBLE PRECISION,
        market_p_yes DOUBLE PRECISION,
        edge DOUBLE PRECISION,
        recommendation TEXT,
        confidence DOUBLE PRECISION,
        confirmation_agree INTEGER,
        confirmation_total INTEGER,
        confirmation_detail TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS whale_trades (
        trade_id TEXT PRIMARY KEY,
        ticker TEXT NOT NULL,
        ts_ms BIGINT NOT NULL,
        side TEXT NOT NULL,
        count DOUBLE PRECISION NOT NULL,
        price_cents DOUBLE PRECISION NOT NULL,
        notional_usd DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shadow_decisions (
        ticker TEXT PRIMARY KEY,
        ts_ms BIGINT NOT NULL,
        index_id TEXT NOT NULL,
        experiment_id TEXT NOT NULL,
        snapshot_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decision_snapshots (
        ticker TEXT PRIMARY KEY,
        ts_ms BIGINT NOT NULL,
        index_id TEXT NOT NULL,
        snapshot_json TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_index_ticks_id_ts ON index_ticks (index_id, ts_ms)",
    "CREATE INDEX IF NOT EXISTS idx_market_ticks_ticker_ts ON market_ticks (market_ticker, ts_ms)",
    "CREATE INDEX IF NOT EXISTS idx_markets_status ON markets (status, closed_at_ms)",
    "CREATE INDEX IF NOT EXISTS idx_whale_trades_ticker_ts ON whale_trades (ticker, ts_ms)",
]


class Store:
    def __init__(self, database_url: str):
        self._is_postgres = database_url.startswith(("postgresql://", "postgres://"))
        if self._is_postgres:
            self._conn = psycopg.connect(database_url)
        else:
            Path(database_url).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(database_url, check_same_thread=False)

        for statement in _SCHEMA_STATEMENTS:
            self._raw_execute(statement)
        self._migrate()
        self._conn.commit()
        # Neither a single sqlite3 connection nor a single psycopg connection is
        # safe for concurrent use across threads; the asyncio bot loop and
        # FastAPI's threadpool workers can both hit this connection at once.
        self._lock = threading.Lock()

    def _raw_execute(self, sql: str, params=()):
        """Execute SQL written with `?` placeholders against either backend."""
        if self._is_postgres:
            sql = sql.replace("?", "%s")
        return self._conn.execute(sql, params)

    def _execute(self, sql: str, params=()):
        with self._lock:
            cur = self._raw_execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params=()):
        with self._lock:
            return self._raw_execute(sql, params)

    def _migrate(self) -> None:
        """Add columns introduced after a DB may already exist on disk."""
        if self._is_postgres:
            for stmt in (
                "ALTER TABLE markets ADD COLUMN IF NOT EXISTS result TEXT",
                "ALTER TABLE markets ADD COLUMN IF NOT EXISTS outcome_checked_at_ms BIGINT",
                "ALTER TABLE decisions ADD COLUMN IF NOT EXISTS confirmation_agree INTEGER",
                "ALTER TABLE decisions ADD COLUMN IF NOT EXISTS confirmation_total INTEGER",
                "ALTER TABLE decisions ADD COLUMN IF NOT EXISTS confirmation_detail TEXT",
            ):
                self._raw_execute(stmt)
            return

        existing = {row[1] for row in self._raw_execute("PRAGMA table_info(markets)").fetchall()}
        if "result" not in existing:
            self._raw_execute("ALTER TABLE markets ADD COLUMN result TEXT")
        if "outcome_checked_at_ms" not in existing:
            self._raw_execute("ALTER TABLE markets ADD COLUMN outcome_checked_at_ms INTEGER")

        decisions_existing = {row[1] for row in self._raw_execute("PRAGMA table_info(decisions)").fetchall()}
        if "confirmation_agree" not in decisions_existing:
            self._raw_execute("ALTER TABLE decisions ADD COLUMN confirmation_agree INTEGER")
        if "confirmation_total" not in decisions_existing:
            self._raw_execute("ALTER TABLE decisions ADD COLUMN confirmation_total INTEGER")
        if "confirmation_detail" not in decisions_existing:
            self._raw_execute("ALTER TABLE decisions ADD COLUMN confirmation_detail TEXT")

    def close(self) -> None:
        self._conn.close()

    def insert_index_tick(self, index_id: str, ts_ms: int, value: float) -> None:
        self._execute(
            """INSERT INTO index_ticks (index_id, ts_ms, value) VALUES (?, ?, ?)
               ON CONFLICT (index_id, ts_ms) DO UPDATE SET value = excluded.value""",
            (index_id, ts_ms, value),
        )

    def insert_market_tick(
        self,
        market_ticker: str,
        ts_ms: int,
        price_dollars: float,
        yes_bid_dollars: float,
        yes_ask_dollars: float,
        yes_bid_size: float,
        yes_ask_size: float,
        volume: float,
        open_interest: float,
    ) -> None:
        self._execute(
            """INSERT INTO market_ticks
               (market_ticker, ts_ms, price_dollars, yes_bid_dollars, yes_ask_dollars,
                yes_bid_size, yes_ask_size, volume, open_interest)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (market_ticker, ts_ms) DO UPDATE SET
                 price_dollars = excluded.price_dollars,
                 yes_bid_dollars = excluded.yes_bid_dollars,
                 yes_ask_dollars = excluded.yes_ask_dollars,
                 yes_bid_size = excluded.yes_bid_size,
                 yes_ask_size = excluded.yes_ask_size,
                 volume = excluded.volume,
                 open_interest = excluded.open_interest""",
            (
                market_ticker,
                ts_ms,
                price_dollars,
                yes_bid_dollars,
                yes_ask_dollars,
                yes_bid_size,
                yes_ask_size,
                volume,
                open_interest,
            ),
        )

    def insert_signal(self, signal) -> None:  # signal: kalshi_client.models.Signal
        self._execute(
            """INSERT INTO signals
               (ticker, ts_ms, index_id, index_price, strike, seconds_to_expiry,
                model_p_yes, market_p_yes, edge, recommendation, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (ticker, ts_ms) DO UPDATE SET
                 index_id = excluded.index_id,
                 index_price = excluded.index_price,
                 strike = excluded.strike,
                 seconds_to_expiry = excluded.seconds_to_expiry,
                 model_p_yes = excluded.model_p_yes,
                 market_p_yes = excluded.market_p_yes,
                 edge = excluded.edge,
                 recommendation = excluded.recommendation,
                 confidence = excluded.confidence""",
            (
                signal.ticker,
                signal.ts_ms,
                signal.index_id,
                signal.index_price,
                signal.strike,
                signal.seconds_to_expiry,
                signal.model_p_yes,
                signal.market_p_yes,
                signal.edge,
                signal.recommendation,
                signal.confidence,
            ),
        )

    def recent_index_ticks(self, index_id: str, since_ms: int) -> list[tuple[int, float]]:
        cur = self._query(
            "SELECT ts_ms, value FROM index_ticks WHERE index_id = ? AND ts_ms >= ? ORDER BY ts_ms",
            (index_id, since_ms),
        )
        return cur.fetchall()

    def latest_index_prices(self) -> dict[str, dict]:
        """Most recent raw tick per index_id, independent of the signal/poll cadence."""
        cur = self._query(
            """SELECT t.index_id, t.ts_ms, t.value
               FROM index_ticks t
               INNER JOIN (
                   SELECT index_id, MAX(ts_ms) AS max_ts FROM index_ticks GROUP BY index_id
               ) latest ON t.index_id = latest.index_id AND t.ts_ms = latest.max_ts"""
        )
        return {row[0]: {"ts_ms": row[1], "value": row[2]} for row in cur.fetchall()}

    def latest_signals(self, limit: int = 50) -> list[dict]:
        cur = self._query(
            """SELECT ticker, ts_ms, index_id, index_price, strike, seconds_to_expiry,
                      model_p_yes, market_p_yes, edge, recommendation, confidence
               FROM signals ORDER BY ts_ms DESC LIMIT ?""",
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def latest_signal_per_ticker(self) -> list[dict]:
        """One row per active ticker: its most recent signal."""
        cur = self._query(
            """SELECT s.ticker, s.ts_ms, s.index_id, s.index_price, s.strike, s.seconds_to_expiry,
                      s.model_p_yes, s.market_p_yes, s.edge, s.recommendation, s.confidence
               FROM signals s
               INNER JOIN (
                   SELECT ticker, MAX(ts_ms) AS max_ts FROM signals GROUP BY ticker
               ) latest ON s.ticker = latest.ticker AND s.ts_ms = latest.max_ts
               ORDER BY s.ticker"""
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    # --- Market lifecycle (active/closed) -------------------------------------------

    def upsert_active_market(
        self, ticker: str, index_id: str, strike: float, close_ts_ms: int, now_ms: int | None = None
    ) -> None:
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        self._execute(
            """INSERT INTO markets (ticker, index_id, strike, close_ts_ms, first_seen_ts_ms,
                                     last_seen_ts_ms, status, closed_at_ms)
               VALUES (?, ?, ?, ?, ?, ?, 'active', NULL)
               ON CONFLICT (ticker) DO UPDATE SET
                 index_id = excluded.index_id,
                 strike = excluded.strike,
                 close_ts_ms = excluded.close_ts_ms,
                 last_seen_ts_ms = excluded.last_seen_ts_ms,
                 status = 'active',
                 closed_at_ms = NULL""",
            (ticker, index_id, strike, close_ts_ms, now_ms, now_ms),
        )

    def get_active_tickers(self) -> list[str]:
        cur = self._query("SELECT ticker FROM markets WHERE status = 'active'")
        return [row[0] for row in cur.fetchall()]

    def mark_closed(self, ticker: str, closed_at_ms: int | None = None) -> None:
        closed_at_ms = closed_at_ms if closed_at_ms is not None else int(time.time() * 1000)
        self._execute(
            "UPDATE markets SET status = 'closed', closed_at_ms = ? WHERE ticker = ? AND status = 'active'",
            (closed_at_ms, ticker),
        )

    _MARKETS_WITH_LATEST_SIGNAL_SQL = """
        SELECT m.ticker, m.index_id, m.strike, m.close_ts_ms, m.status, m.closed_at_ms, m.result,
               s.ts_ms, s.index_price, s.seconds_to_expiry, s.model_p_yes, s.market_p_yes,
               s.edge, s.recommendation, s.confidence,
               d.ts_ms AS decision_ts_ms, d.index_price AS decision_index_price,
               d.seconds_to_expiry AS decision_seconds_to_expiry,
               d.model_p_yes AS decision_model_p_yes, d.market_p_yes AS decision_market_p_yes,
               d.edge AS decision_edge, d.recommendation AS decision_recommendation,
               d.confidence AS decision_confidence,
               d.confirmation_agree AS decision_confirmation_agree,
               d.confirmation_total AS decision_confirmation_total,
               d.confirmation_detail AS decision_confirmation_detail
        FROM markets m
        LEFT JOIN (
            SELECT s1.* FROM signals s1
            INNER JOIN (SELECT ticker, MAX(ts_ms) AS max_ts FROM signals GROUP BY ticker) latest
              ON s1.ticker = latest.ticker AND s1.ts_ms = latest.max_ts
        ) s ON s.ticker = m.ticker
        LEFT JOIN decisions d ON d.ticker = m.ticker
    """

    def dashboard_markets(self, grace_period_sec: int) -> list[dict]:
        """Active markets, plus markets closed within the last `grace_period_sec`."""
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - grace_period_sec * 1000
        cur = self._query(
            self._MARKETS_WITH_LATEST_SIGNAL_SQL
            + " WHERE m.status = 'active' OR (m.status = 'closed' AND m.closed_at_ms >= ?)"
            + " ORDER BY m.status ASC, m.close_ts_ms ASC",
            (cutoff_ms,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def closed_markets_history(self, limit: int = 200) -> list[dict]:
        cur = self._query(
            self._MARKETS_WITH_LATEST_SIGNAL_SQL
            + " WHERE m.status = 'closed' ORDER BY m.closed_at_ms DESC LIMIT ?",
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    # --- Settlement outcomes and live calibration -----------------------------------

    def markets_pending_outcome(self, max_age_ms: int, now_ms: int | None = None) -> list[str]:
        """Closed markets (within `max_age_ms`) whose settlement result isn't recorded yet."""
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        cutoff_ms = now_ms - max_age_ms
        cur = self._query(
            "SELECT ticker FROM markets WHERE status = 'closed' AND result IS NULL AND closed_at_ms >= ?",
            (cutoff_ms,),
        )
        return [row[0] for row in cur.fetchall()]

    def record_outcome(self, ticker: str, result: str, checked_at_ms: int | None = None) -> None:
        checked_at_ms = checked_at_ms if checked_at_ms is not None else int(time.time() * 1000)
        self._execute(
            "UPDATE markets SET result = ?, outcome_checked_at_ms = ? WHERE ticker = ?",
            (result, checked_at_ms, ticker),
        )

    # --- Locked-in trade-call decisions ---------------------------------------------

    def has_decision(self, ticker: str) -> bool:
        cur = self._query("SELECT 1 FROM decisions WHERE ticker = ?", (ticker,))
        return cur.fetchone() is not None

    def record_decision(
        self,
        ticker: str,
        ts_ms: int,
        seconds_to_expiry: float,
        index_price: float,
        strike: float,
        model_p_yes: float,
        market_p_yes: float,
        edge: float,
        recommendation: str,
        confidence: float,
        confirmation_agree: int | None = None,
        confirmation_total: int | None = None,
        confirmation_detail: str | None = None,
        shadow_snapshot: dict | None = None,
        decision_snapshot: dict | None = None,
    ) -> None:
        """One-shot: does nothing if a decision was already recorded for this ticker."""
        snapshot_json = json.dumps(shadow_snapshot, allow_nan=False) if shadow_snapshot is not None else None
        with self._lock:
            try:
                cursor = self._raw_execute(
                    """INSERT INTO decisions
               (ticker, ts_ms, seconds_to_expiry, index_price, strike, model_p_yes,
                market_p_yes, edge, recommendation, confidence, confirmation_agree, confirmation_total,
                confirmation_detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (ticker) DO NOTHING""",
                    (
                        ticker, ts_ms, seconds_to_expiry, index_price, strike, model_p_yes,
                        market_p_yes, edge, recommendation, confidence, confirmation_agree, confirmation_total,
                        confirmation_detail,
                    ),
                )
                if cursor.rowcount == 1 and shadow_snapshot is not None:
                    self._raw_execute(
                        """INSERT INTO shadow_decisions
                           (ticker, ts_ms, index_id, experiment_id, snapshot_json)
                           VALUES (?, ?, ?, ?, ?)""",
                        (ticker, ts_ms, shadow_snapshot["index_id"], shadow_snapshot["experiment_id"], snapshot_json),
                    )
                if cursor.rowcount == 1 and decision_snapshot is not None:
                    self._raw_execute(
                        """INSERT INTO decision_snapshots (ticker, ts_ms, index_id, snapshot_json)
                           VALUES (?, ?, ?, ?)""",
                        (ticker, ts_ms, decision_snapshot["index_id"], json.dumps(decision_snapshot, allow_nan=False)),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def shadow_decisions(self, limit: int = 200, index_id: str | None = None) -> list[dict]:
        sql = """SELECT s.ticker, s.ts_ms, s.index_id, s.experiment_id, s.snapshot_json,
                        m.close_ts_ms, m.result
                 FROM shadow_decisions s
                 INNER JOIN decisions d ON d.ticker = s.ticker AND d.ts_ms = s.ts_ms
                 LEFT JOIN markets m ON m.ticker = s.ticker"""
        params: list = []
        if index_id is not None:
            sql += " WHERE s.index_id = ?"
            params.append(index_id)
        sql += " ORDER BY s.ts_ms DESC, s.ticker ASC LIMIT ?"
        params.append(limit)
        cursor = self._query(sql, tuple(params))
        columns = [column[0] for column in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        for row in rows:
            row["snapshot"] = json.loads(row.pop("snapshot_json"))
        return rows

    def shadow_comparison(self, limit: int = 10000, index_id: str | None = None) -> dict:
        rows = self.shadow_decisions(limit=limit, index_id=index_id)
        groups: dict[tuple[str, str], list[dict]] = {}
        for row in rows:
            groups.setdefault((row["experiment_id"], row["index_id"]), []).append(row)
        comparisons = []
        for (experiment_id, coin), group in groups.items():
            settled = [row for row in group if row["result"] in ("yes", "no")]
            scores = {}
            for name in ("live", "shadow", "market"):
                correct = actionable = actionable_correct = high_confidence = high_confidence_correct = 0
                squared_error = log_loss = confidence_sum = 0.0
                for row in settled:
                    snapshot = row["snapshot"]
                    probability = snapshot["market_p_yes"] if name == "market" else snapshot[name]["model_p_yes"]
                    outcome = float(row["result"] == "yes")
                    is_correct = (probability >= 0.5) == bool(outcome)
                    correct += int(is_correct)
                    squared_error += (probability - outcome) ** 2
                    clipped = min(max(probability, 1e-6), 1 - 1e-6)
                    log_loss -= outcome * math.log(clipped) + (1 - outcome) * math.log(1 - clipped)
                    confidence = max(probability, 1 - probability)
                    confidence_sum += confidence
                    if confidence >= 0.9:
                        high_confidence += 1
                        high_confidence_correct += int(is_correct)
                    recommendation = snapshot[name]["recommendation"] if name != "market" else "NO_EDGE"
                    if recommendation in ("BUY_YES", "BUY_NO"):
                        actionable += 1
                        actionable_correct += int((recommendation == "BUY_YES") == bool(outcome))
                count = len(settled)
                scores[name] = {
                    "n": count,
                    "correct": correct,
                    "accuracy": correct / count if count else None,
                    "brier": squared_error / count if count else None,
                    "log_loss": log_loss / count if count else None,
                    "mean_confidence": confidence_sum / count if count else None,
                    "high_confidence_n": high_confidence,
                    "high_confidence_accuracy": high_confidence_correct / high_confidence if high_confidence else None,
                    "actionable_n": actionable if name != "market" else None,
                    "actionable_correct": actionable_correct if name != "market" else None,
                }
            comparisons.append({
                "experiment_id": experiment_id,
                "index_id": coin,
                "recorded": len(group),
                "pending": len(group) - len(settled),
                "first_decision_ts_ms": min(row["ts_ms"] for row in group),
                "last_decision_ts_ms": max(row["ts_ms"] for row in group),
                "scores": scores,
            })
        return {"limit": limit, "recorded": len(rows), "groups": comparisons}

    def calibration_stats(self, limit: int = 200, index_id: str | None = None) -> dict:
        """Rolling Brier score / log loss of the model vs. the market, over the
        last `limit` markets with a recorded yes/no settlement result.

        Scored against the locked-in decision (made `KALSHI_DECISION_LEAD_SEC`
        before close) rather than the last live signal, since the decision is
        what's actually actionable/tradeable - that's the number that matters.

        Pass `index_id` (e.g. "BRTI", "SOLUSD_RTI") to scope the track record
        to a single coin instead of combining all monitored coins together.
        """
        sql = """
            SELECT m.result, d.model_p_yes, d.market_p_yes
            FROM markets m
            INNER JOIN decisions d ON d.ticker = m.ticker
            WHERE m.result IN ('yes', 'no')
        """
        params: list = []
        if index_id is not None:
            sql += " AND m.index_id = ?"
            params.append(index_id)
        sql += " ORDER BY m.closed_at_ms DESC LIMIT ?"
        params.append(limit)
        cur = self._query(sql, tuple(params))
        rows = cur.fetchall()

        n = len(rows)
        if n == 0:
            return {
                "n": 0,
                "model_brier": None,
                "model_log_loss": None,
                "market_brier": None,
                "market_log_loss": None,
                "baseline_brier": None,
            }

        eps = 1e-6
        model_sq = market_sq = baseline_sq = 0.0
        model_ll = market_ll = 0.0
        for result, model_p, market_p in rows:
            y = 1.0 if result == "yes" else 0.0
            model_sq += (model_p - y) ** 2
            market_sq += (market_p - y) ** 2
            baseline_sq += (0.5 - y) ** 2
            mp = min(max(model_p, eps), 1 - eps)
            ap = min(max(market_p, eps), 1 - eps)
            model_ll += -(y * math.log(mp) + (1 - y) * math.log(1 - mp))
            market_ll += -(y * math.log(ap) + (1 - y) * math.log(1 - ap))

        return {
            "n": n,
            "model_brier": model_sq / n,
            "model_log_loss": model_ll / n,
            "market_brier": market_sq / n,
            "market_log_loss": market_ll / n,
            "baseline_brier": baseline_sq / n,
        }

    # --- Big-bet ("whale") tracking ---------------------------------------------------

    def insert_whale_trade(
        self,
        trade_id: str,
        ticker: str,
        ts_ms: int,
        side: str,
        count: float,
        price_cents: float,
        notional_usd: float,
    ) -> None:
        """One row per large fill. Idempotent - safe to re-insert the same trade_id
        across polls since a market's trade feed is re-scanned each cycle."""
        self._execute(
            """INSERT INTO whale_trades
               (trade_id, ticker, ts_ms, side, count, price_cents, notional_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (trade_id) DO NOTHING""",
            (trade_id, ticker, ts_ms, side, count, price_cents, notional_usd),
        )

    def latest_whale_trade_ts(self, ticker: str) -> int | None:
        """Most recent trade timestamp already recorded for this ticker, so polling
        only needs to ask Kalshi for trades newer than this."""
        cur = self._query(
            "SELECT MAX(ts_ms) FROM whale_trades WHERE ticker = ?", (ticker,)
        )
        row = cur.fetchone()
        return row[0] if row and row[0] is not None else None

    def recent_whale_trades(
        self, ticker: str, limit: int = 20, min_usd: float = 0.0
    ) -> list[dict]:
        cur = self._query(
            """SELECT trade_id, ticker, ts_ms, side, count, price_cents, notional_usd
               FROM whale_trades
               WHERE ticker = ? AND notional_usd >= ?
               ORDER BY ts_ms DESC LIMIT ?""",
            (ticker, min_usd, limit),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

