"""Read-only, market-level walk-forward research; never promotes a model or sends orders."""
from __future__ import annotations

import argparse
import bisect
import json
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from ..prediction.fair_value import HISTORY_MS, coin_z_score, fair_value_p_yes
from ..prediction.logistic import LogisticRegressionModel
from ..prediction.market_recal import MarketRecalibrator
from ..trading import EntryRule, ONE, dollars, taker_fee

FEATURE_NAMES = ["market_logit", "coin_logit", "btc_coin_logit", "market_time", "coin_time"]


@dataclass(frozen=True)
class Quote:
    ts_ms: int
    bid: float
    ask: float
    bid_size: float | None
    ask_size: float | None


@dataclass(frozen=True)
class Observation:
    ticker: str
    index_id: str
    ts_ms: int
    close_ts_ms: int
    outcome: float
    z: float
    minutes_left: float
    quote: Quote
    executions: dict[int, Quote | None]
    label_available_ms: int | None = None

    @property
    def market_p(self) -> float:
        return (self.quote.bid + self.quote.ask) / 2

    @property
    def features(self) -> list[float]:
        def logit(p: float) -> float:
            p = min(max(p, 1e-4), 1 - 1e-4)
            return math.log(p / (1 - p))

        market = logit(self.market_p)
        coin = logit(0.5 * (1 + math.erf(self.z / math.sqrt(2))))
        time = self.minutes_left / 15
        return [market, coin, coin * (self.index_id == "BRTI"), market * time, coin * time]


def load_observations(
    database: str, lead_sec: int = 390, delays_ms: tuple[int, ...] = (0, 2000, 10000),
    max_quote_age_ms: int = 5000,
) -> tuple[list[Observation], dict[str, int]]:
    path = Path(database).resolve()
    if not path.is_file():
        raise FileNotFoundError(database)
    if not 60 <= lead_sec <= 900 or max_quote_age_ms <= 0 or any(delay < 0 for delay in delays_ms):
        raise ValueError("Invalid decision lead, delay, or quote age")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    excluded: Counter[str] = Counter()
    observations = []
    try:
        ticks: dict[str, list[tuple[int, float]]] = defaultdict(list)
        for index, ts, value in connection.execute("SELECT index_id, ts_ms, value FROM index_ticks ORDER BY ts_ms"):
            ticks[index].append((ts, value))
        tick_times = {index: [ts for ts, _ in values] for index, values in ticks.items()}
        markets = connection.execute("""
            SELECT ticker, index_id, strike, close_ts_ms, result, outcome_checked_at_ms FROM markets
            WHERE result IN ('yes', 'no') ORDER BY close_ts_ms, ticker
        """).fetchall()
        for ticker, index, strike, close, outcome, label_available in markets:
            if index not in ("BRTI", "SOLUSD_RTI") or strike is None or close is None:
                excluded["missing_market_metadata"] += 1
                continue
            decision = close - lead_sec * 1000
            values = ticks[index]
            times = tick_times[index]
            lo, hi = bisect.bisect_left(times, decision - HISTORY_MS), bisect.bisect_right(times, decision)
            scored = coin_z_score(values[lo:hi], strike, close, decision)
            if scored is None:
                excluded["insufficient_index_history"] += 1
                continue
            rows = connection.execute("""
                SELECT ts_ms, yes_bid_dollars, yes_ask_dollars, yes_bid_size, yes_ask_size
                FROM market_ticks WHERE market_ticker = ? AND ts_ms <= ? ORDER BY ts_ms
            """, (ticker, decision + max(delays_ms, default=0))).fetchall()
            quotes = [
                Quote(ts, bid, ask, bid_size, ask_size)
                for ts, bid, ask, bid_size, ask_size in rows
                if bid is not None and ask is not None and math.isfinite(bid) and math.isfinite(ask)
                and 0 <= bid <= ask <= 1
            ]
            quote_times = [quote.ts_ms for quote in quotes]

            def at(timestamp: int) -> Quote | None:
                i = bisect.bisect_right(quote_times, timestamp) - 1
                if i < 0 or timestamp - quotes[i].ts_ms > max_quote_age_ms:
                    return None
                return quotes[i]

            quote = at(decision)
            if quote is None:
                excluded["missing_or_stale_decision_quote"] += 1
                continue
            executions = {delay: at(decision + delay) if decision + delay < close else None for delay in delays_ms}
            observations.append(Observation(
                ticker, index, decision, close, float(outcome == "yes"), *scored, quote, executions, label_available,
            ))
    finally:
        connection.close()
    return observations, dict(excluded)


def _score(rows: list[dict]) -> dict:
    if not rows:
        return {"markets": 0, "brier": None, "market_brier": None, "log_loss": None}
    return {
        "markets": len(rows),
        "brier": sum((row["p"] - row["observation"].outcome) ** 2 for row in rows) / len(rows),
        "market_brier": sum((row["observation"].market_p - row["observation"].outcome) ** 2 for row in rows) / len(rows),
        "log_loss": -sum(math.log(min(max(row["p"] if row["observation"].outcome else 1 - row["p"], 1e-6), 1 - 1e-6))
                         for row in rows) / len(rows),
    }


def _trade_score(rows: list[dict], delay_ms: int, rule: EntryRule, buffer: float, slippage: float, max_spread: float) -> dict:
    pnl = []
    excluded: Counter[str] = Counter()
    fees = cost = cumulative = peak = drawdown = 0.0
    wins = 0
    for row in rows:
        item, probability = row["observation"], dollars(row["p"])
        side = rule.pick_side(probability, dollars(item.quote.ask), ONE - dollars(item.quote.bid))
        original_ask = item.quote.ask if side == "yes" else 1 - item.quote.bid
        if rule.check(item.ticker, item.minutes_left * 60, probability, side, dollars(original_ask)):
            continue
        quote = item.executions.get(delay_ms)
        if quote is None:
            excluded["missing_or_stale_execution_quote"] += 1
            continue
        if max_spread > 0 and quote.ask - quote.bid > max_spread + 1e-9:
            excluded["wide_spread"] += 1
            continue
        # A frozen decision signal is deliberately stressed against delayed executable quotes.
        price = (quote.ask if side == "yes" else 1 - quote.bid) + slippage
        if not 0 < price < 1:
            excluded["invalid_execution_price"] += 1
            continue
        size = quote.ask_size if side == "yes" else quote.bid_size
        if size is None or not math.isfinite(size) or size < 1:
            excluded["unknown_or_insufficient_size"] += 1
            continue
        confidence = float(probability if side == "yes" else ONE - probability)
        fee = float(taker_fee(dollars(price)))
        if (rule.check(item.ticker, item.minutes_left * 60 - delay_ms / 1000, probability, side, dollars(price))
                or confidence - buffer - price - fee < float(rule.min_edge or 0)):
            excluded["edge_or_rule_expired"] += 1
            continue
        won = (side == "yes") == bool(item.outcome)
        value = float(won) - price - fee
        pnl.append(value)
        fees += fee
        cost += price + fee
        wins += int(won)
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    mean = sum(pnl) / len(pnl) if pnl else None
    return {
        "trades": len(pnl), "wins": wins, "win_rate": wins / len(pnl) if pnl else None,
        "net_pnl": sum(pnl), "entry_cost": cost, "fees": fees,
        "mean_net_per_contract": mean, "max_drawdown": drawdown,
        "excluded_entries": dict(excluded),
    }


def walk_forward(
    observations: list[Observation], min_train: int = 300, fold_size: int = 60,
    delays_ms: tuple[int, ...] = (0, 2000, 10000), buffer: float = 0.02,
    slippage: float = 0.01, max_spread: float = 0.03, rule: EntryRule | None = None,
) -> dict:
    if min_train < 2 or fold_size < 1 or any(delay < 0 for delay in delays_ms):
        raise ValueError("Training size, fold size, and delays must be valid")
    if any(not math.isfinite(value) or not 0 <= value < 1 for value in (buffer, slippage, max_spread)):
        raise ValueError("Costs and buffer must be finite and between zero and one")
    rule = rule or EntryRule(
        name="Conservative research", min_price=dollars("0.60"), max_price=dollars("0.95"),
        min_confidence=dollars("0"), min_edge=dollars("0.03"), min_seconds_left=240, max_seconds_left=660,
    )
    observations = sorted(observations, key=lambda item: (item.ts_ms, item.ticker))
    if len({item.ticker for item in observations}) != len(observations):
        raise ValueError("Walk-forward observations must contain only one decision per market")
    if any(item.ts_ms >= item.close_ts_ms or item.outcome not in (0, 1)
           or not math.isfinite(item.z) or not 1 <= item.minutes_left <= 15
           or item.quote.ts_ms > item.ts_ms or not 0 <= item.quote.bid <= item.quote.ask <= 1
           or (item.label_available_ms is not None and item.label_available_ms < item.close_ts_ms)
           or any(quote is not None and (quote.ts_ms > item.ts_ms + delay or
                                        not 0 <= quote.bid <= quote.ask <= 1)
                  for delay, quote in item.executions.items())
           for item in observations):
        raise ValueError("Invalid observation or future quote")
    times = sorted({item.ts_ms for item in observations})
    groups: dict[int, list[Observation]] = defaultdict(list)
    for item in observations:
        groups[item.ts_ms].append(item)
    scored: dict[str, list[dict]] = defaultdict(list)
    folds = []
    for start in range(0, len(times), fold_size):
        fold_times = times[start:start + fold_size]
        cutoff = fold_times[0]
        train = [item for item in observations if item.close_ts_ms < cutoff
                 and item.label_available_ms is not None and item.label_available_ms < cutoff][-5000:]
        test = [item for timestamp in fold_times for item in groups[timestamp]]
        recal = MarketRecalibrator(min_samples=min_train)
        recal.fit((item.market_p, item.outcome) for item in train)
        challenger = LogisticRegressionModel(feature_names=FEATURE_NAMES, min_samples=min_train)
        challenger.fit([(item.features, item.outcome) for item in train])
        fitted = recal.is_fitted and challenger.is_fitted
        folds.append({
            "decision_start_ms": cutoff, "decision_end_ms": fold_times[-1], "test_markets": len(test),
            "train_markets": len(train), "last_train_close_ms": max((item.close_ts_ms for item in train), default=None),
            "last_train_label_ms": max((item.label_available_ms for item in train), default=None),
            "challengers_fitted": fitted,
        })
        fold_scored: dict[str, list[dict]] = defaultdict(list)
        for item in test:
            probabilities = {
                "frozen_fair_value": fair_value_p_yes(item.market_p, item.z, item.minutes_left, item.index_id == "BRTI"),
            }
            if fitted:
                probabilities["walk_forward_recal"] = recal.predict(item.market_p)
                probabilities["walk_forward_logistic"] = challenger.predict_proba(item.features)
            for name, probability in probabilities.items():
                result = {"observation": item, "p": probability}
                scored[name].append(result)
                fold_scored[name].append(result)
        folds[-1]["scores"] = {name: _score(rows) for name, rows in fold_scored.items()}

    def section(rows: list[dict]) -> dict:
        return {**_score(rows), "latency_scenarios": {
            str(delay): _trade_score(rows, delay, rule, buffer, slippage, max_spread) for delay in delays_ms
        }}

    return {
        "status": "research_only",
        "promotion_allowed": False,
        "limitations": [
            "Frozen fair-value coefficients were fitted previously; replay is not an untouched holdout.",
            "Refitted challengers train only on markets closed and outcomes recorded strictly before each test fold.",
            "One decision and at most one one-contract settlement trade per market; no tick-level pseudoreplication.",
            "Recorded top-of-book size is required; queue position, impact, and actual fills are not modeled.",
            "Latency scenarios use a frozen decision probability and delayed quotes, not future coin data.",
            "No automatic live promotion, maker orders, or Kelly sizing; independent forward paper results are required.",
        ],
        "parameters": {"min_train": min_train, "fold_size": fold_size, "delays_ms": delays_ms,
                       "buffer": buffer, "slippage": slippage, "max_spread": max_spread, "rule": rule.to_json()},
        "folds": folds,
        "models": {
            name: {"overall": section(rows), "by_coin": {
                coin: section([row for row in rows if row["observation"].index_id == coin])
                for coin in sorted({row["observation"].index_id for row in rows})
            }} for name, rows in scored.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=r"data\kalshi_bot.db")
    parser.add_argument("--lead-sec", type=int, default=390)
    parser.add_argument("--min-train", type=int, default=300)
    parser.add_argument("--fold-size", type=int, default=60)
    parser.add_argument("--buffer", type=float, default=0.02)
    parser.add_argument("--slippage", type=float, default=0.01)
    args = parser.parse_args()
    observations, excluded = load_observations(args.database, args.lead_sec)
    report = walk_forward(observations, args.min_train, args.fold_size, buffer=args.buffer, slippage=args.slippage)
    report["excluded_markets"] = excluded
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
