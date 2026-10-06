"""Standard experiment suite. Uses the train/val/test development splits only; the forward split and the
reserved holdout (days >= HOLDOUT_START) are never touched here.

    python -m aqlabs.research.suite --root C:\\data\\eventstore --out results.txt
"""
from __future__ import annotations

import argparse
import time

import numpy as np

from ..store import EventStore
from . import replay as R


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out")
    args = ap.parse_args()
    out = open(args.out, "w", encoding="utf-8") if args.out else None

    def P(s=""):
        print(s, flush=True)
        if out:
            out.write(s + "\n")

    t0 = time.time()
    grids, markets = R.load_all(EventStore(args.root))
    usable = [m for m in markets if m["split"] in ("train", "val", "test")]
    P(f"markets total={len(markets)} analysed={len(usable)} (not used here: "
      f"{sum(m['split'] == 'fwd' for m in markets)} forward Oct 6-19, "
      f"{sum(m['split'] == 'holdout' for m in markets)} holdout)  train {sum(m['split'] == 'train' for m in usable)}, "
      f"val {sum(m['split'] == 'val' for m in usable)}, test {sum(m['split'] == 'test' for m in usable)}")

    z_idx = [R.signal_arrays(grids[("idx", m["coin"])], m) for m in usable]
    z_cb = [R.signal_arrays(grids[("cb", m["coin"])], m) for m in usable]
    z_stale = [R.signal_arrays(grids[("idx", m["coin"])], m, shift=60) for m in usable]
    rng = np.random.default_rng(11)
    z_shuf = list(z_idx)
    for coin in ("BRTI", "SOLUSD_RTI"):
        ids = [i for i, m in enumerate(usable) if m["coin"] == coin]
        for a, b in zip(ids, rng.permutation(ids)):
            z_shuf[a] = z_idx[b]

    def block(trs, label):
        P(R.stats(trs, label + " | ALL"))
        for k in ("train", "val", "test"):
            P(R.stats([t for t in trs if t["split"] == k], f"   {k}"))

    frozen = R.frozen_prob()
    P("\n== A. frozen repo fair-value coefficients (thr 3c, 4-14 min left, up to 5 buys 60s apart, hold to settlement)")
    for delay in (2, 5, 10):
        trs, st = R.run_fair_value(usable, z_idx, frozen, thr=0.03, delay=delay)
        block(trs, f"A delay={delay}s (signals={st['signals']} unfilled={st['unfilled']})")
    trs, _ = R.run_fair_value(usable, z_idx, frozen, thr=0.03, delay=10, slip=0.01)
    P(R.stats(trs, "A delay=10s + 1c slippage | ALL"))
    trsA, _ = R.run_fair_value(usable, z_idx, frozen, thr=0.03, delay=2)
    gross = np.mean([t["gross"] for t in trsA])
    fees = np.mean([t["fee"] for t in trsA])
    P(f"cost split (A, delay 2s): gross={gross*100:+.2f}c  fees={fees*100:.2f}c  net={(gross-fees)*100:+.2f}c per contract")
    P("-- threshold sweep")
    for thr in (0.0, 0.02, 0.03, 0.05, 0.08):
        trs, st = R.run_fair_value(usable, z_idx, frozen, thr=thr, delay=2)
        P(R.stats(trs, f"A thr={thr:.2f} (signals {st['signals']})"))
    P("-- by coin / minutes left")
    for coin in ("BRTI", "SOLUSD_RTI"):
        P(R.stats([t for t in trsA if t["coin"] == coin], f"   coin={coin}"))
    for a, b in ((4, 7), (7, 10), (10, 14)):
        P(R.stats([t for t in trsA if a <= t["ml"] < b], f"   minutes_left {a}-{b}"))
    P("-- placebos (must lose)")
    trs, st = R.run_fair_value(usable, z_stale, frozen, thr=0.03, delay=2)
    P(R.stats(trs, f"PLACEBO coin price 60s stale (signals {st['signals']})"))
    trs, st = R.run_fair_value(usable, z_shuf, frozen, thr=0.03, delay=2)
    P(R.stats(trs, f"PLACEBO z shuffled across markets (signals {st['signals']})"))
    trs, st = R.run_fair_value(usable, z_cb, frozen, thr=0.03, delay=2)
    block(trs, f"C. Coinbase price in z (signals={st['signals']})")

    P("\n== B. refit on train only, threshold tuned on val, test evaluated once")
    for kind in ("full", "market_only"):
        w, n = R.fit_variant(usable, z_idx, kind)
        P(f"fit {kind}: n_samples={n} weights={np.round(w, 3).tolist()}")
        prob = R.fitted_prob(kind, w)
        best = None
        for thr in (0.02, 0.03, 0.05):
            trs, _ = R.run_fair_value(usable, z_idx, prob, thr=thr, delay=2)
            v = [t for t in trs if t["split"] == "val"]
            P(R.stats(v, f"  {kind} thr={thr:.2f} val"))
            mean = np.mean([t["pnl"] for t in v]) if v else -9
            if best is None or mean > best[0]:
                best = (mean, thr)
        thr = best[1]
        trs, _ = R.run_fair_value(usable, z_idx, prob, thr=thr, delay=2)
        P(R.stats([t for t in trs if t["split"] == "test"], f"  >>> {kind} chosen thr={thr:.2f} TEST (once)"))
        trs, _ = R.run_fair_value(usable, z_idx, prob, thr=thr, delay=10, slip=0.01)
        P(R.stats([t for t in trs if t["split"] == "test"], f"  >>> {kind} TEST delay 10s + 1c slip"))

    P("\n== D. momentum scalping (legacy; the locked-call/LLM gate is not replicated)")
    for conf, tp, stop, label in ((0.65, 0.03, 0.05, "README default"), (0.80, 0.02, 0.95, "paper settings"),
                                  (0.80, 0.03, 0.05, "conf80 tp3c stop5c")):
        trs = R.run_scalp(usable, grids, conf=conf, tp=tp, stop=stop, delay=2)
        block(trs, f"D {label}")
        kinds: dict[str, int] = {}
        for t in trs:
            kinds[t["kind"]] = kinds.get(t["kind"], 0) + 1
        P(f"   exits: {kinds}")

    P("\n== E. blind baselines at T-6:30")
    P(R.stats(R.run_blind(usable, "yes"), "E blind YES @ask"))
    P(R.stats(R.run_blind(usable, "no"), "E blind NO @bid"))
    P(f"\nfinished in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
