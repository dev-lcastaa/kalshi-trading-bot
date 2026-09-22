"""Offline calibration check: replay historical index ticks through the model.

Computes Brier score and log loss vs a naive 0.5 baseline, to validate the
model before trusting its live signals.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

from ..features.engine import Features, build_features
from ..prediction.model import Predictor
from ..signals.confirmation import check_confirmation
from ..signals.generator import generate_signal

_EPS = 1e-6
_LOOKBACK_MS = 5 * 60 * 1000  # trailing window used to estimate vol/momentum


@dataclass(frozen=True)
class BacktestCase:
    """One historical 15-minute market: its index ticks and final outcome."""

    ticker: str
    strike: float
    close_ts_ms: int
    index_ticks: list[tuple[int, float]]  # (ts_ms, value), ascending, spanning the window
    outcome_yes: bool  # whether index_price > strike at close_ts_ms


@dataclass(frozen=True)
class ScoreResult:
    n: int
    brier_score: float
    log_loss: float
    baseline_brier_score: float  # naive p=0.5 for comparison


def _predictions_for_case(case: BacktestCase, predictor: Predictor) -> list[tuple[float, float]]:
    """Return (model_p, outcome) pairs sampled at every tick in the case."""
    pairs: list[tuple[float, float]] = []
    outcome = 1.0 if case.outcome_yes else 0.0
    for i, (ts_ms, _value) in enumerate(case.index_ticks):
        seconds_to_expiry = (case.close_ts_ms - ts_ms) / 1000.0
        if seconds_to_expiry < 0:
            continue
        window_start = ts_ms - _LOOKBACK_MS
        window = [t for t in case.index_ticks[: i + 1] if t[0] >= window_start]
        if len(window) < 2:
            continue
        features = build_features(
            window, strike=case.strike, seconds_to_expiry=seconds_to_expiry, close_ts_ms=case.close_ts_ms
        )
        p = predictor.predict(features)
        pairs.append((p, outcome))
    return pairs


def run_backtest(cases: list[BacktestCase], predictor: Predictor) -> ScoreResult:
    all_pairs: list[tuple[float, float]] = []
    for case in cases:
        all_pairs.extend(_predictions_for_case(case, predictor))

    if not all_pairs:
        return ScoreResult(n=0, brier_score=float("nan"), log_loss=float("nan"), baseline_brier_score=float("nan"))

    n = len(all_pairs)
    brier = sum((p - y) ** 2 for p, y in all_pairs) / n
    baseline_brier = sum((0.5 - y) ** 2 for _p, y in all_pairs) / n
    log_loss = (
        -sum(
            y * math.log(max(p, _EPS)) + (1 - y) * math.log(max(1 - p, _EPS))
            for p, y in all_pairs
        )
        / n
    )
    return ScoreResult(n=n, brier_score=brier, log_loss=log_loss, baseline_brier_score=baseline_brier)


def evaluate_snapshots(rows: list[dict], predictor: Predictor, market_blend_weight: float = 0.0) -> dict:
    """Retrospective paired evaluation; no fitting or claim of an unseen holdout.

    Uses recorded features and quotes, not post-decision ticks. Gross returns
    assume one contract purchased at the recorded ask and held to settlement;
    fees, slippage, latency, and available size are not modeled.
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    seen: set[tuple[str, str]] = set()
    excluded = 0
    for row in sorted(rows, key=lambda item: (item["ts_ms"], item["ticker"])):
        if row["result"] not in ("yes", "no") or not row["ts_ms"] < row["close_ts_ms"]:
            excluded += 1
            continue
        snapshot = row["snapshot"]
        ticks = snapshot["index_ticks"]
        quotes = snapshot["quotes"]
        key = (row["experiment_id"], row["ticker"])
        if key in seen or not ticks or any(timestamp > row["ts_ms"] for timestamp, _ in ticks):
            excluded += 1
            continue
        if any(right[0] <= left[0] for left, right in zip(ticks, ticks[1:])):
            excluded += 1
            continue
        if any(not math.isfinite(price) or price <= 0 for _, price in ticks):
            excluded += 1
            continue
        if any(quotes.get(field) is not None and quotes[field] > row["ts_ms"] for field in ("ts_ms", "received_at_ms")):
            excluded += 1
            continue
        bid, ask = quotes["yes_bid_dollars"], quotes["yes_ask_dollars"]
        if not (0 <= bid <= ask <= 1):
            excluded += 1
            continue
        features = replace(
            Features(**snapshot["features"]),
            history_span_sec=(ticks[-1][0] - ticks[0][0]) / 1000,
            history_tick_count=len(ticks),
        )
        if not math.isclose(features.seconds_to_expiry, (row["close_ts_ms"] - row["ts_ms"]) / 1000, abs_tol=0.01):
            excluded += 1
            continue
        signal = generate_signal(
            ticker=row["ticker"], index_id=row["index_id"], features=features,
            predictor=predictor, yes_bid_dollars=bid, yes_ask_dollars=ask,
            edge_threshold=snapshot["parameters"]["edge_threshold"], ts_ms=row["ts_ms"],
            market_blend_weight=market_blend_weight,
        )
        recommendation = signal.recommendation if check_confirmation(features, signal.model_p_yes >= 0.5).confirmed else "NO_EDGE"
        forecasts = {
            "live": (snapshot["live"]["model_p_yes"], snapshot["live"]["recommendation"]),
            "recorded_shadow": (snapshot["shadow"]["model_p_yes"], snapshot["shadow"]["recommendation"]),
            "candidate": (signal.model_p_yes, recommendation),
            "market": (snapshot["market_p_yes"], "NO_EDGE"),
        }
        if any(not math.isfinite(probability) or not 0 <= probability <= 1 for probability, _ in forecasts.values()):
            excluded += 1
            continue
        seen.add(key)
        groups.setdefault((row["experiment_id"], row["index_id"]), []).append({
            "close_ts_ms": row["close_ts_ms"], "outcome": row["result"] == "yes",
            "bid": bid, "ask": ask, "forecasts": forecasts,
        })

    def score(items: list[dict], name: str) -> dict:
        squared = loss = cost = gross = 0.0
        trades = wins = correct = 0
        for item in items:
            probability, recommendation = item["forecasts"][name]
            outcome = item["outcome"]
            squared += (probability - outcome) ** 2
            clipped = min(max(probability, _EPS), 1 - _EPS)
            loss -= math.log(clipped if outcome else 1 - clipped)
            correct += int((probability >= 0.5) == outcome)
            if recommendation in ("BUY_YES", "BUY_NO"):
                yes = recommendation == "BUY_YES"
                entry = item["ask"] if yes else 1 - item["bid"]
                won = yes == outcome
                trades += 1
                wins += int(won)
                cost += entry
                gross += int(won) - entry
        count = len(items)
        return {
            "n": count, "brier": squared / count if count else None,
            "log_loss": loss / count if count else None,
            "accuracy": correct / count if count else None,
            "trades": trades, "wins": wins, "ask_cost": cost, "ask_gross_pnl": gross,
        }

    comparisons = []
    for (experiment, coin), items in sorted(groups.items()):
        windows = sorted({item["close_ts_ms"] for group_key, group in groups.items()
                          if group_key[0] == experiment for item in group})
        cutoff = windows[len(windows) // 2] if len(windows) > 1 else None
        subsets = {
            "all": items,
            "earlier": [item for item in items if cutoff is not None and item["close_ts_ms"] < cutoff],
            "later": [item for item in items if cutoff is not None and item["close_ts_ms"] >= cutoff],
        }
        comparisons.append({
            "experiment_id": experiment, "index_id": coin, "split_close_ts_ms": cutoff,
            "scores": {label: {name: score(subset, name) for name in ("live", "recorded_shadow", "candidate", "market")}
                       for label, subset in subsets.items()},
        })
    return {
        "evaluation": "retrospective; one contract per call; before fees/slippage; no fill guarantee",
        "candidate": type(predictor).__name__, "candidate_parameters": vars(predictor),
        "market_blend_weight": market_blend_weight,
        "input_rows": len(rows), "excluded_rows": excluded, "groups": comparisons,
    }


def grid_search_market_blend_weight(
    rows: list[dict],
    predictor_factory,
    weights: tuple[float, ...] = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
) -> list[dict]:
    """Replay `rows` once per candidate blend weight and rank by combined Brier.

    Reuses `evaluate_snapshots` for each weight (a fresh predictor instance per
    run, since predictors are stateless but shouldn't be reused across calls
    that mutate nothing but should stay independent). Returns weights sorted
    best (lowest combined Brier) first.
    """
    results = []
    for weight in weights:
        evaluation = evaluate_snapshots(rows, predictor_factory(), market_blend_weight=weight)
        per_coin = {}
        total_n = total_squared_error = 0.0
        for group in evaluation["groups"]:
            candidate = group["scores"]["all"]["candidate"]
            if candidate["n"]:
                per_coin[group["index_id"]] = {
                    "n": candidate["n"], "brier": candidate["brier"], "accuracy": candidate["accuracy"],
                }
                total_n += candidate["n"]
                total_squared_error += candidate["brier"] * candidate["n"]
        results.append({
            "market_blend_weight": weight,
            "n": int(total_n),
            "combined_brier": total_squared_error / total_n if total_n else None,
            "per_coin": per_coin,
        })
    return sorted(results, key=lambda r: (r["combined_brier"] is None, r["combined_brier"]))


if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path
    from urllib.request import urlopen

    from ..prediction.model import RegularizedSettlementPredictor

    parser = argparse.ArgumentParser(description="Evaluate v3 on saved decision snapshots without fitting.")
    parser.add_argument(
        "source",
        help="JSON export file or /api/shadow-decisions (or /api/decision-snapshots) URL",
    )
    parser.add_argument(
        "--grid-search-blend", action="store_true",
        help="Grid-search market_blend_weight against `source` instead of a single evaluation.",
    )
    parser.add_argument(
        "--weights", type=float, nargs="+", default=None,
        help="Candidate market_blend_weight values (default: 0.0 to 1.0 in steps of 0.1).",
    )
    args = parser.parse_args()
    if args.source.startswith(("http://", "https://")):
        with urlopen(args.source, timeout=15) as response:
            snapshots = json.load(response)
    else:
        snapshots = json.loads(Path(args.source).read_text(encoding="utf-8"))
    if args.grid_search_blend:
        weights = tuple(args.weights) if args.weights else (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
        result = grid_search_market_blend_weight(snapshots, RegularizedSettlementPredictor, weights=weights)
    else:
        result = evaluate_snapshots(snapshots, RegularizedSettlementPredictor())
    print(json.dumps(result, indent=2, allow_nan=False))
