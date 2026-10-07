"""Phase 3, H7: favorite-longshot bias at extreme prices (see Addendum 2 in docs/PHASE3_PREREGISTRATION.md).

    python -m aqlabs.research.phase3_h7 --root <eventstore> --stage screen

A Platt recalibration of the market mid (fit on `train` only) is used to buy the favorite side when its executable
price is at least 0.85 and the after-fee edge is non-negative, one entry per market, held to settlement.
"""
from __future__ import annotations

import argparse
import time

import numpy as np

from ..costs import taker_fee_array as fee
from ..store import EventStore
from . import phase3 as P3
from . import registry as REG
from . import replay as R

FAVORITE_MIN_PRICE = 0.85
EDGE_MIN = 0.0
DELAY_S = 2
WINDOW = (60, 840)
_EPS = 1e-9


def fit_platt(markets) -> tuple[float, float, int]:
    """P(yes) = sigmoid(a + b * logit(mid)), no penalty, fit on the minute marks of the `train` markets."""
    xs, ys = [], []
    for m in markets:
        if m["split"] != "train":
            continue
        sl = m["close_s"] - m["s"]
        pick = np.flatnonzero(m["qok"] & (sl % 60 == 0) & (sl >= WINDOW[0]) & (sl <= WINDOW[1])
                              & (m["ask"] < 1) & (m["bid"] > 0))
        if len(pick):
            xs.append(R.logit(m["mid"][pick]))
            ys.append(np.full(len(pick), m["y"]))
    x, y = np.concatenate(xs), np.concatenate(ys)
    w = R.fit_ridge_logit(np.column_stack([np.ones(len(x)), x]), y, l2=0.0)
    return float(w[0]), float(w[1]), int(len(y))


def recalibrated(m: dict, a: float, b: float) -> np.ndarray:
    return 1 / (1 + np.exp(-(a + b * R.logit(m["mid"]))))


def run_h7(markets, probs, min_price=FAVORITE_MIN_PRICE, delay=DELAY_S, edge_min=EDGE_MIN):
    """One entry per market: the first second whose favorite side has a non-negative after-fee edge and fills."""
    trades = []
    for m, p in zip(markets, probs):
        sl = m["close_s"] - m["s"]
        bid, ask = m["bid"], m["ask"]
        win = m["qok"] & (sl >= WINDOW[0]) & (sl <= WINDOW[1])
        with np.errstate(invalid="ignore"):
            cy = win & (ask >= min_price - _EPS) & (ask < 1) & (m["asz"] >= 1) & (p - ask - fee(ask) >= edge_min - _EPS)
            cn = win & ((1 - bid) >= min_price - _EPS) & (bid > 0) & (m["bsz"] >= 1) \
                & ((1 - p) - (1 - bid) - fee(1 - bid) >= edge_min - _EPS)
        for i in np.flatnonzero(cy | cn):
            e = i + delay
            if e >= len(ask) or not m["qok"][e]:
                continue
            yes = bool(cy[i])
            if yes and not (m["asz"][e] >= 1 and ask[e] < 1):
                continue
            if not yes and not (m["bsz"][e] >= 1 and bid[e] > 0):
                continue
            price = float(ask[e]) if yes else float(1 - bid[e])
            won = m["y"] if yes else 1 - m["y"]
            f = float(fee(price))
            trades.append(dict(pnl=won - price - f, gross=won - price, fee=f, market=m["ticker"], day=m["day"],
                               split=m["split"], coin=m["coin"], ml=float(sl[i]) / 60, side="yes" if yes else "no",
                               price=price))
            break
    return trades


def calibration_table(markets, bins=((0.85, 0.90), (0.90, 0.95), (0.95, 1.0))) -> list[dict]:
    """Realised win rate of the favorite side against its executable price, at the minute marks."""
    rows = {b: [] for b in bins}
    for m in markets:
        sl = m["close_s"] - m["s"]
        pick = np.flatnonzero(m["qok"] & (sl % 60 == 0) & (sl >= WINDOW[0]) & (sl <= WINDOW[1]))
        for i in pick:
            if m["ask"][i] < 1 and m["ask"][i] >= FAVORITE_MIN_PRICE:
                price, won = float(m["ask"][i]), m["y"]
            elif m["bid"][i] > 0 and 1 - m["bid"][i] >= FAVORITE_MIN_PRICE and 1 - m["bid"][i] < 1:
                price, won = float(1 - m["bid"][i]), 1 - m["y"]
            else:
                continue
            for lo, hi in bins:
                if lo <= price < hi or (hi == 1.0 and lo <= price < 1.0):
                    rows[(lo, hi)].append((price, won))
                    break
    out = []
    for (lo, hi), v in rows.items():
        if v:
            arr = np.array(v)
            price, win = arr[:, 0].mean(), arr[:, 1].mean()
            out.append({"bin": f"{lo:.2f}-{hi:.2f}", "n": len(v), "mean_price": float(price), "win_rate": float(win),
                        "gross_c": float((win - price) * 100), "fee_c": float(np.mean(fee(arr[:, 0])) * 100)})
    return out


def screen(P, grids, markets, root, store) -> dict:
    usable = [m for m in markets if m["split"] in ("train", "val", "test")]
    a, b, n_fit = fit_platt(usable)
    P(f"stage 1 screen on development data: {len(usable)} markets; fwd and holdout untouched")
    P(f"Platt fit on train ({n_fit:,} rows): a = {a:+.4f}, b = {b:.4f}  (b > 1 means favorites win more than priced)")
    probs = [recalibrated(m, a, b) for m in usable]
    P("\ncalibration of the favorite side at the minute marks (all development days; cents per contract):")
    for r in calibration_table(usable):
        P(f"   price {r['bin']}: n={r['n']:>7,}  mean price {r['mean_price']:.4f}  win rate {r['win_rate']:.4f}  "
          f"gross edge {r['gross_c']:+.2f}c  fee {r['fee_c']:.2f}c  net {r['gross_c'] - r['fee_c']:+.2f}c")
    trades = run_h7(usable, probs)
    placebo = run_h7(usable, P3.shuffle_within_coin(usable, probs))
    P("\n== H7 stage 1 (favorite side at 0.85 or more, non-negative after-fee edge, one entry per market, hold)")
    P(R.stats(trades, "H7 trades | ALL"))
    for k in ("train", "val", "test"):
        part = [t for t in trades if t["split"] == k]
        if part:
            P(R.stats(part, f"   {k}"))
    for coin in ("BRTI", "SOLUSD_RTI"):
        P(R.stats([t for t in trades if t["coin"] == coin], f"   {coin}"))
    if trades:
        prices = np.array([t["price"] for t in trades])
        P(f"   entry price: median {np.median(prices):.3f}; gross {np.mean([t['gross'] for t in trades]) * 100:+.2f}c, "
          f"fee {np.mean([t['fee'] for t in trades]) * 100:.2f}c per trade")
    P(R.stats(placebo, "H7 placebo (probabilities shuffled) | ALL"))
    P(R.stats(run_h7(usable, probs, min_price=0.0), "reference: no price restriction (the original v0.8.0 form) | ALL"))
    chk = P3.screen_check(trades, placebo)
    P("criteria: " + ", ".join(f"{k}={'ok' if v else 'FAIL'}" for k, v in chk["criteria"].items()))
    P(f"==> H7 stage 1: {'PASS' if chk['passed'] else 'FAIL'}")
    result = dict(chk, platt=[a, b], n_fit=n_fit, calibration=calibration_table(usable))
    REG.log_run(root, "H7", "screen", ["train", "val", "test"],
                {"min_price": FAVORITE_MIN_PRICE, "edge_min": EDGE_MIN, "delay_s": DELAY_S, "window": WINDOW,
                 "entries_per_market": 1}, P3._json_safe(result), chk["passed"],
                {t: mm["fingerprint"] for t, mm in store.manifest().items()})
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--stage", choices=("screen",), required=True)
    ap.add_argument("--out")
    args = ap.parse_args()
    P, store, t0 = P3._Out(args.out), EventStore(args.root), time.time()
    grids, markets = R.load_all(store)
    screen(P, grids, markets, args.root, store)
    P(f"\nfinished in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
