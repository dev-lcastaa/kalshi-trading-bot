"""Phase 3, H4: order-book depth and trade flow (see docs/PHASE3_PREREGISTRATION.md; every setting is fixed there).

    python -m aqlabs.research.phase3_h4 --root <eventstore> --stage validate
    python -m aqlabs.research.phase3_h4 --root <eventstore> --stage confirm --confirm-holdout --holdout-end YYYY-MM-DD

Needs the collector's `orderbook_depth` and `trades` tables, so it only has data from Oct 6 2026 on. The model is fit on
the first 7 days of the forward split and evaluated on the rest (stage 2), or on the holdout window (stage 3).
`--allow-partial` runs the same code on whatever exists and never writes to the registry.
"""
from __future__ import annotations

import argparse
import json
import time

import numpy as np

from ..costs import taker_fee_array as fee
from ..store import EventStore
from . import phase3 as P3
from . import registry as REG
from . import replay as R

FIT_DAYS = 7
TOP_PCT = 95
FLOW_WINDOW_S = 30
HORIZON_S = 10
MIN_GAP_S = 30
LEVELS = 5
DELAY_S = 2
MIN_COVERAGE = 0.9  # share of evaluated markets that must have depth data


def depth_imbalance(yes_json: str | None, no_json: str | None, levels: int = LEVELS) -> float:
    """(sum of the top YES bids - sum of the top NO bids) / (sum of both). Positive = more demand for YES."""
    try:
        yes = sum(q for _, q in json.loads(yes_json)[:levels])
        no = sum(q for _, q in json.loads(no_json)[:levels])
    except (TypeError, ValueError):
        return float("nan")
    return (yes - no) / (yes + no) if yes + no > 0 else float("nan")


def signed_flow_feature(f) -> np.ndarray:
    f = np.asarray(f, dtype=float)
    return np.sign(f) * np.log1p(np.abs(f))


def attach_features(store: EventStore, markets: list[dict]) -> None:
    """Add per-second `imb` (depth imbalance, NaN if the snapshot is older than 5 s) and `flow` (signed taker flow over
    the previous 30 s, using only trades from earlier seconds) arrays to each market."""
    con = store.connect()
    if not (store.has_data("orderbook_depth") and store.has_data("trades")):
        raise REG.RegistryError("the event store has no orderbook_depth / trades data yet (collector data needed)")
    wanted = {m["ticker"] for m in markets}
    depth: dict[str, tuple[list, list]] = {}
    trades: dict[str, tuple[list, list]] = {}
    for day in sorted({m["day"] for m in markets}):
        tickers = [m["ticker"] for m in markets if m["day"] == day]
        lo = int(np.datetime64(day, "ms").astype("int64"))
        # a market's quotes start 15 minutes before it closes, so widen the partition-pruning window; each ticker
        # belongs to exactly one day, so no row is loaded twice
        span = [lo - 1_800_000, lo + 86_400_000 + 1_800_000]
        for t, ts, yb, nb in con.execute(
                "select market_ticker, ts_ms, yes_bids, no_bids from orderbook_depth "
                "where ts_ms >= ? and ts_ms < ? and list_contains(?, market_ticker) order by market_ticker, ts_ms",
                span + [tickers]).fetchall():
            depth.setdefault(t, ([], []))[0].append(ts)
            depth[t][1].append(depth_imbalance(yb, nb))
        for t, ts, side, cnt in con.execute(
                "select market_ticker, ts_ms, taker_side, count from trades "
                "where ts_ms >= ? and ts_ms < ? and list_contains(?, market_ticker) order by market_ticker, ts_ms",
                span + [tickers]).fetchall():
            if cnt is not None and side in ("yes", "no"):
                trades.setdefault(t, ([], []))[0].append(ts)
                trades[t][1].append(cnt if side == "yes" else -cnt)
    for m in markets:
        s = m["s"]
        n = len(s)
        imb = np.full(n, np.nan)
        if m["ticker"] in depth:
            ts, val = np.array(depth[m["ticker"]][0]), np.array(depth[m["ticker"]][1])
            idx = np.searchsorted(ts, s * 1000, side="right") - 1
            ic = np.clip(idx, 0, None)
            fresh = (idx >= 0) & (s * 1000 - ts[ic] <= R.MAX_AGE_MS)
            imb = np.where(fresh, val[ic], np.nan)
        flow = np.zeros(n)
        if m["ticker"] in trades:
            ts, qty = np.array(trades[m["ticker"]][0]), np.array(trades[m["ticker"]][1])
            base = int(s[0]) - FLOW_WINDOW_S
            b = ts // 1000 - base
            keep = (b >= 0) & (b < n + FLOW_WINDOW_S)
            binned = np.zeros(n + FLOW_WINDOW_S)
            np.add.at(binned, b[keep], qty[keep])
            c = np.r_[0.0, np.cumsum(binned)]
            flow = c[FLOW_WINDOW_S:FLOW_WINDOW_S + n] - c[:n]  # seconds t-30 .. t-1
        m["imb"], m["flow"] = imb, signed_flow_feature(flow)


def feature_rows(m: dict, h: int = HORIZON_S):
    """Indices and (X, y) for one market: X = [imbalance, flow], y = mid(t+h) - mid(t)."""
    n = len(m["s"])
    i = np.arange(n - h)
    sl = m["close_s"] - m["s"][i]
    ok = m["qok"][i] & m["qok"][i + h] & np.isfinite(m["imb"][i]) & (sl >= 240) & (sl <= 840)
    i = i[ok]
    return i, np.column_stack([m["imb"][i], m["flow"][i]]), m["mid"][i + h] - m["mid"][i]


def fit_h4(markets) -> dict:
    Xs, ys = [], []
    for m in markets:
        _, X, y = feature_rows(m)
        if len(y):
            Xs.append(X)
            ys.append(y)
    if not ys:
        raise REG.RegistryError("no usable rows to fit H4 (depth coverage too low?)")
    X, y = np.vstack(Xs), np.concatenate(ys)
    beta = np.linalg.solve(X.T @ X, X.T @ y)
    cutoff = float(np.percentile(np.abs(X @ beta), TOP_PCT))
    return {"beta": beta, "cutoff": cutoff, "n_fit": int(len(y))}


def predictions(m: dict, beta) -> np.ndarray:
    pred = np.full(len(m["s"]), np.nan)
    ok = np.isfinite(m["imb"])
    pred[ok] = np.column_stack([m["imb"][ok], m["flow"][ok]]) @ beta
    return pred


def run_h4(markets, preds, cutoff: float, delay: int = DELAY_S, h: int = HORIZON_S, gap: int = MIN_GAP_S):
    """Enter in the direction of the prediction when it is in the top tail, exit `h` seconds later by crossing back.
    If the exit cannot be filled the position is held to settlement instead (counted in `held`)."""
    trades, held = [], 0
    for m, pred in zip(markets, preds):
        sl = m["close_s"] - m["s"]
        bid, ask = m["bid"], m["ask"]
        cand = np.flatnonzero(np.isfinite(pred) & (np.abs(np.nan_to_num(pred)) >= cutoff) & (sl >= 240) & (sl <= 840))
        last = -10**9
        for i in cand:
            if i - last < gap:
                continue
            e, x = i + delay, i + delay + h
            if x >= len(bid) or not (m["qok"][e] and m["qok"][x]):
                continue
            yes = pred[i] > 0
            if yes:
                if not (m["asz"][e] >= 1 and ask[e] < 1):
                    continue
                entry = float(ask[e])
                can_exit = m["bsz"][x] >= 1 and bid[x] > 0
                exit_px = float(bid[x]) if can_exit else None
            else:
                if not (m["bsz"][e] >= 1 and bid[e] > 0):
                    continue
                entry = float(1 - bid[e])
                can_exit = m["asz"][x] >= 1 and ask[x] < 1
                exit_px = float(1 - ask[x]) if can_exit else None
            cost = entry + float(fee(entry))
            if exit_px is not None:
                pnl = exit_px - float(fee(exit_px)) - cost
            else:
                held += 1
                pnl = (m["y"] if yes else 1 - m["y"]) - cost
            trades.append(dict(pnl=pnl, market=m["ticker"], day=m["day"], split=m["split"], coin=m["coin"],
                               ml=float(sl[i]) / 60, side="yes" if yes else "no", price=entry))
            last = i
    return trades, held


def oos_r2(markets, preds) -> float:
    num = den = 0.0
    for m, pred in zip(markets, preds):
        i, _, y = feature_rows(m)
        p = pred[i]
        ok = np.isfinite(p)
        num += float(np.sum((y[ok] - p[ok]) ** 2))
        den += float(np.sum(y[ok] ** 2))
    return 1 - num / den if den > 0 else float("nan")


def evaluate(markets_all, stage: str, store: EventStore, holdout_end: str | None, allow_partial: bool, P):
    markets_all = [m for m in markets_all if m["coin"] in R.CORE_COINS]  # the pre-registered universe
    split = "fwd" if stage == "validate" else "holdout"
    fwd = sorted({m["day"] for m in markets_all if m["split"] == "fwd"})
    if len(fwd) <= FIT_DAYS:
        raise REG.RegistryError(f"H4 needs more than {FIT_DAYS} days of forward data to fit and evaluate; have {len(fwd)}")
    fit_days = fwd[:FIT_DAYS]
    fit = [m for m in markets_all if m["day"] in fit_days]
    if stage == "validate":
        ev_mk = [m for m in markets_all if m["split"] == "fwd" and m["day"] not in fit_days]
        want_last = R.SPLITS["fwd"][1]
    else:
        ev_mk = [m for m in markets_all if m["split"] == "holdout" and m["day"] <= holdout_end]
        want_last = holdout_end
    last_day = max((m["day"] for m in ev_mk), default=None)
    complete = last_day is not None and last_day >= want_last
    if not complete and not allow_partial:
        raise REG.RegistryError(f"{split} period is incomplete (last market day {last_day}, needs {want_last}); "
                                f"use --allow-partial for an unlogged dry run")
    attach_features(store, fit + ev_mk)
    model = fit_h4(fit)
    covered = [m for m in ev_mk if np.isfinite(m["imb"]).any()]
    coverage = len(covered) / len(ev_mk) if ev_mk else 0.0
    P(f"H4 {stage}: fit on {fit_days[0]}..{fit_days[-1]} ({model['n_fit']:,} rows, {len(fit)} markets), "
      f"evaluate {len(ev_mk)} markets {min(m['day'] for m in ev_mk)}..{last_day}; depth coverage {coverage:.0%}")
    P(f"beta(imbalance, flow) = {model['beta'][0]:+.5f}, {model['beta'][1]:+.5f}; trade cut-off |pred| >= {model['cutoff']:.5f}")
    preds = [predictions(m, model["beta"]) for m in ev_mk]
    r2 = oos_r2(ev_mk, preds)
    P(f"out-of-sample R^2 of the 10 s mid-change forecast: {r2:+.5f}")
    trades, held = run_h4(ev_mk, preds, model["cutoff"])
    placebo, _ = run_h4(ev_mk, P3.shuffle_within_coin(ev_mk, preds), model["cutoff"])
    delay5, _ = run_h4(ev_mk, preds, model["cutoff"], delay=5)
    P(R.stats(trades, "H4 trades"))
    P(R.stats(placebo, "H4 placebo (features shuffled)"))
    P(R.stats(delay5, "H4 delay 5 s"))
    P(f"exits that could not be filled and were held to settlement: {held} of {len(trades)}")
    chk = P3.validation_check(trades, placebo, delay5)
    chk["criteria"]["depth_coverage_ge_90pct"] = coverage >= MIN_COVERAGE
    chk["passed"] = all(bool(v) for v in chk["criteria"].values())
    chk.update(oos_r2=r2, coverage=coverage, held_to_settlement=held, beta=model["beta"].tolist(), cutoff=model["cutoff"])
    P("criteria: " + ", ".join(f"{k}={'ok' if v else 'FAIL'}" for k, v in chk["criteria"].items()))
    P(f"==> H4 {stage}: {'PASS' if chk['passed'] else 'FAIL'}" + ("" if complete else "   [DRY RUN, not logged]"))
    return chk, split, complete


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--stage", choices=("validate", "confirm"), required=True)
    ap.add_argument("--out")
    ap.add_argument("--allow-partial", action="store_true")
    ap.add_argument("--confirm-holdout", action="store_true")
    ap.add_argument("--holdout-end")
    args = ap.parse_args()
    if args.stage == "confirm":
        if not args.confirm_holdout or not args.holdout_end:
            raise SystemExit("stage 3 looks at the holdout: pass --confirm-holdout and --holdout-end YYYY-MM-DD")
        REG.require_prerequisite(args.root, "H4", "confirm")
    out = open(args.out, "w", encoding="utf-8") if args.out else None

    def P(s=""):
        print(s, flush=True)
        if out:
            out.write(s + "\n")

    t0 = time.time()
    store = EventStore(args.root)
    _, markets = R.load_all(store)
    chk, split, complete = evaluate(markets, args.stage, store, args.holdout_end, args.allow_partial, P)
    if complete:
        REG.log_run(args.root, "H4", args.stage, [split], {
            "fit_days": FIT_DAYS, "top_pct": TOP_PCT, "flow_window_s": FLOW_WINDOW_S, "horizon_s": HORIZON_S,
            "levels": LEVELS, "delay_s": DELAY_S, "min_gap_s": MIN_GAP_S}, P3._json_safe(chk), chk["passed"],
            {t: m["fingerprint"] for t, m in store.manifest().items()})
    P(f"\nfinished in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
