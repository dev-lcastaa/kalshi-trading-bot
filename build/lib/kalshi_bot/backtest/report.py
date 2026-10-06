"""Report forward decision quality from the live audit snapshots."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any


def _rows(database: str) -> list[dict[str, Any]]:
    path = Path(database)
    if not path.exists():
        raise FileNotFoundError(database)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("""
            SELECT d.ticker, d.ts_ms, d.seconds_to_expiry,
                   m.index_id, m.result, s.snapshot_json
            FROM decisions d
            INNER JOIN markets m ON m.ticker = d.ticker
            INNER JOIN decision_snapshots s ON s.ticker = d.ticker AND s.ts_ms = d.ts_ms
            WHERE m.result IN ('yes', 'no')
            ORDER BY d.ts_ms, d.ticker
        """).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _score(items: list[dict[str, Any]], probability_key: str) -> dict[str, Any]:
    if not items:
        return {"n": 0, "brier": None, "log_loss": None, "accuracy": None}
    probabilities = [float(item[probability_key]) for item in items]
    outcomes = [item["outcome"] for item in items]
    brier = sum((probability - outcome) ** 2 for probability, outcome in zip(probabilities, outcomes)) / len(items)
    log_loss = -sum(
        outcome * math.log(max(probability, 1e-6))
        + (1 - outcome) * math.log(max(1 - probability, 1e-6))
        for probability, outcome in zip(probabilities, outcomes)
    ) / len(items)
    accuracy = sum((probability >= 0.5) == bool(outcome) for probability, outcome in zip(probabilities, outcomes)) / len(items)
    return {"n": len(items), "brier": brier, "log_loss": log_loss, "accuracy": accuracy}


def _trade_score(items: list[dict[str, Any]], fee_per_contract: float, slippage: float) -> dict[str, Any]:
    trades = [item for item in items if item["recommendation"] in ("BUY_YES", "BUY_NO")]
    pnl = 0.0
    wins = 0
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for item in trades:
        yes = item["recommendation"] == "BUY_YES"
        ask = item["yes_ask"] if yes else item["no_ask"]
        if ask is None or not 0 <= ask <= 1:
            continue
        entry = ask + slippage + fee_per_contract
        won = yes == bool(item["outcome"])
        value = float(won) - entry
        pnl += value
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
        wins += int(won)
    return {
        "issued": len(trades),
        "wins": wins,
        "win_rate": wins / len(trades) if trades else None,
        "gross_pnl": pnl,
        "max_drawdown": max_drawdown,
        "fee_per_contract": fee_per_contract,
        "slippage": slippage,
    }


def _calibration(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        probability = min(max(float(item["model_probability"]), 0.0), 0.999999)
        buckets[min(int(probability * 10), 9)].append(item)
    result = []
    for index in range(10):
        bucket = buckets[index]
        result.append({
            "range": f"{index / 10:.1f}-{(index + 1) / 10:.1f}",
            "n": len(bucket),
            "mean_probability": sum(float(item["model_probability"]) for item in bucket) / len(bucket) if bucket else None,
            "observed_rate": sum(item["outcome"] for item in bucket) / len(bucket) if bucket else None,
        })
    return result


def _prepare(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared = []
    for raw in raw_rows:
        snapshot = json.loads(raw["snapshot_json"])
        live = snapshot["live"]
        quotes = snapshot.get("quotes", {})
        prepared.append({
            "ticker": raw["ticker"],
            "ts_ms": raw["ts_ms"],
            "index_id": raw["index_id"],
            "outcome": int(raw["result"] == "yes"),
            "model_probability": live["model_p_yes"],
            "market_probability": live.get("market_p_yes", snapshot.get("market_p_yes")),
            "recommendation": live["recommendation"],
            "yes_ask": quotes.get("yes_ask_dollars"),
            "no_ask": quotes.get("no_ask_dollars"),
            "quality_flags": snapshot.get("quality_flags", []),
        })
    return prepared


def build_report(database: str, fee_per_contract: float = 0.0, slippage: float = 0.0) -> dict[str, Any]:
    """Build a settled forward-quality report from one SQLite database."""
    if fee_per_contract < 0 or slippage < 0:
        raise ValueError("fee_per_contract and slippage must be non-negative")
    items = _prepare(_rows(database))
    clean = [item for item in items if not item["quality_flags"]]
    by_coin: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in clean:
        by_coin[item["index_id"]].append(item)
    flag_counts: dict[str, int] = defaultdict(int)
    for item in items:
        for flag in item["quality_flags"]:
            flag_counts[flag] += 1

    def section(section_items: list[dict[str, Any]]) -> dict[str, Any]:
        for item in section_items:
            item["market_probability_value"] = item["market_probability"]
        model = _score(section_items, "model_probability")
        market = _score(section_items, "market_probability_value")
        return {
            "n": len(section_items),
            "model": model,
            "market": market,
            "calibration": _calibration(section_items),
            "trades": _trade_score(section_items, fee_per_contract, slippage),
            "abstention_rate": sum(item["recommendation"] == "NO_EDGE" for item in section_items) / len(section_items) if section_items else None,
        }

    return {
        "database": str(Path(database)),
        "settled_snapshots": len(items),
        "clean_snapshots": len(clean),
        "quarantined_snapshots": len(items) - len(clean),
        "quality_flags": dict(sorted(flag_counts.items())),
        "overall": section(clean),
        "by_coin": {coin: section(coin_items) for coin, coin_items in sorted(by_coin.items())},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="data/kalshi_bot.db")
    parser.add_argument("--fee-per-contract", type=float, default=0.0)
    parser.add_argument("--slippage", type=float, default=0.0)
    args = parser.parse_args()
    print(json.dumps(build_report(args.database, args.fee_per_contract, args.slippage), indent=2, allow_nan=False))


if __name__ == "__main__":
    main()