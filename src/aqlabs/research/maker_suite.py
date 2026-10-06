"""Phase 2 maker-execution suite (development splits only; the forward split and the holdout are never touched).

    python -m aqlabs.research.maker_suite --root C:\\data\\eventstore --out maker_results.txt

Pre-registered headline spec (fixed before any result was seen): fair-value signal with frozen coefficients, 3c edge
over the limit price (no fee), limit order joins the touch and rests 60 s, 2 s order delay, up to 5 decisions per
market 60 s apart, held to settlement, maker fee 0. Everything else printed is a sensitivity, not a selection.
"""
from __future__ import annotations

import argparse
import time

import numpy as np

from ..costs import RULES
from ..store import EventStore
from . import maker as MK
from . import replay as R

HEADLINE = dict(thr=0.03, window_s=60, delay=2, maker_fee_scale=0.0)
SHORT = {"optimistic": "O", "queue": "Q", "pessimistic": "P"}


def _mean_c(trades) -> float:
    return float(np.mean([t["pnl"] for t in trades])) * 100 if trades else float("nan")


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
    P(f"markets analysed={len(usable)} (not used here: {sum(m['split'] == 'fwd' for m in markets)} forward Oct 6-19, "
      f"{sum(m['split'] == 'holdout' for m in markets)} holdout)")
    z_idx = [R.signal_arrays(grids[("idx", m["coin"])], m) for m in usable]
    z_stale = [R.signal_arrays(grids[("idx", m["coin"])], m, shift=60) for m in usable]
    rng = np.random.default_rng(11)
    z_shuf = list(z_idx)
    for coin in ("BRTI", "SOLUSD_RTI"):
        ids = [i for i, m in enumerate(usable) if m["coin"] == coin]
        for a, b in zip(ids, rng.permutation(ids)):
            z_shuf[a] = z_idx[b]
    frozen = R.frozen_prob()

    def run(zs, **kw):
        return MK.run_maker(usable, zs, frozen, **{**HEADLINE, **kw})

    def line(label, orders):
        fr = " ".join(f"{SHORT[r]}={MK.fill_rate(orders, r) * 100:4.1f}%" for r in RULES)
        ps = "/".join(f"{_mean_c(MK.as_trades(orders, r, True)):+5.2f}" for r in RULES)
        pf = "/".join(f"{_mean_c(MK.as_trades(orders, r, False)):+5.2f}" for r in RULES)
        P(f"{label:40} n={len(orders):5d} fill {fr} | per-decision c O/Q/P {ps} | per-fill c O/Q/P {pf}")

    P("\n== Fill mechanics (legacy quote data; queue figures are contracts displayed at the touch)")
    orders = run(z_idx)
    qa = np.array([o["queue_ahead"] for o in orders])
    P(f"headline decisions={len(orders)}  median queue ahead={np.median(qa):.0f}  "
      f"share with <50 ahead={np.mean(qa < 50) * 100:.0f}%  share with >1000 ahead={np.mean(qa > 1000) * 100:.0f}%")

    P("\n== HEADLINE: maker execution of the fair-value signal (thr 3c over limit, rest 60 s, delay 2 s, fee 0)")
    taker = MK.taker_trades(orders)
    P(R.stats(taker, "taker: cross the spread on these same decisions"))
    ref, st = R.run_fair_value(usable, z_idx, frozen, thr=0.03, delay=2)
    P(R.stats(ref, f"reference: best taker spec A (3c after fee), signals={st['signals']}"))
    for rule in RULES:
        P(f"-- rule: {rule}   fill rate {MK.fill_rate(orders, rule) * 100:.1f}%")
        per_signal = MK.as_trades(orders, rule, True)
        P(R.stats(per_signal, "   value per DECISION (unfilled = 0) ALL"))
        for k in ("train", "val", "test"):
            P(R.stats([t for t in per_signal if t["split"] == k], f"      {k}"))
        P(R.stats(MK.as_trades(orders, rule, False), "   value per FILL ALL"))
        a = MK.adverse_selection(orders, rule)
        P(f"   adverse selection (taker P/L had we crossed): filled n={a['filled_n']} mean={a['filled_taker_mean'] * 100:+.2f}c"
          f" | unfilled n={a['unfilled_n']} mean={a['unfilled_taker_mean'] * 100:+.2f}c")

    P("\n== Sensitivities (per-decision / per-fill means in cents; no selection)")
    for thr in (0.01, 0.02, 0.05):
        line(f"edge threshold {thr:.2f}", run(z_idx, thr=thr))
    for w in (30, 120):
        line(f"order rests {w}s", run(z_idx, window_s=w))
    line("delay 5s", run(z_idx, delay=5))
    line("maker fee = 25% of taker formula", run(z_idx, maker_fee_scale=0.25))
    line("maker fee = 100% of taker formula", run(z_idx, maker_fee_scale=1.0))

    P("\n== Placebos (no real signal; maker P/L must not look good)")
    line("signal 60s stale", run(z_stale))
    line("signal shuffled across markets", run(z_shuf))

    P("\n== SECOND pre-declared test: pull the order when the edge over the limit falls below 1c (2 s cancel latency)")
    pulled = run(z_idx, cancel_edge=0.01)
    P(f"orders cancelled before the window ended: {np.mean([o['cancelled'] for o in pulled]) * 100:.0f}%")
    for rule in RULES:
        P(f"-- rule: {rule}   fill rate {MK.fill_rate(pulled, rule) * 100:.1f}%")
        per_signal = MK.as_trades(pulled, rule, True)
        P(R.stats(per_signal, "   value per DECISION (unfilled = 0) ALL"))
        for k in ("train", "val", "test"):
            P(R.stats([t for t in per_signal if t["split"] == k], f"      {k}"))
        a = MK.adverse_selection(pulled, rule)
        P(f"   adverse selection (taker P/L had we crossed): filled n={a['filled_n']} mean={a['filled_taker_mean'] * 100:+.2f}c"
          f" | unfilled n={a['unfilled_n']} mean={a['unfilled_taker_mean'] * 100:+.2f}c")
    line("cancel variant, signal 60s stale (placebo)", run(z_stale, cancel_edge=0.01))
    line("cancel variant, signal shuffled (placebo)", run(z_shuf, cancel_edge=0.01))

    P("\n== Where it works (headline, per-decision means)")
    for coin in ("BRTI", "SOLUSD_RTI"):
        line(f"coin={coin}", [o for o in orders if o["coin"] == coin])
    for a, b in ((4, 7), (7, 10), (10, 14)):
        line(f"minutes left {a}-{b}", [o for o in orders if a <= o["ml"] < b])
    for k in ("train", "val", "test"):
        line(f"split={k}", [o for o in orders if o["split"] == k])

    P("\n== Pre-declared readout")
    dev = [o for o in orders if o["split"] in ("train", "val")]
    opt_dev = _mean_c(MK.as_trades(dev, "optimistic", True))
    opt_test = _mean_c(MK.as_trades([o for o in orders if o["split"] == "test"], "optimistic", True))
    pess = MK.as_trades(orders, "pessimistic", True)
    P(f"optimistic per-decision mean: dev(train+val)={opt_dev:+.2f}c  test={opt_test:+.2f}c")
    pulled_dev = [o for o in pulled if o["split"] in ("train", "val")]
    opt_dev_pulled = _mean_c(MK.as_trades(pulled_dev, "optimistic", True))
    P(f"optimistic per-decision mean with cancel-on-weak-signal: dev(train+val)={opt_dev_pulled:+.2f}c")
    if not opt_dev > 0 and not opt_dev_pulled > 0:
        P("KILL CHECK: even the optimistic fill bound is not profitable on development data, with or without "
          "cancelling -> maker execution of this signal is dead.")
    else:
        P("KILL CHECK passed: an optimistic bound is profitable on development data, so the idea is not dead yet.")
    P("The pessimistic and queue rows say how much of that survives realistic queue position; "
      "calibrate on the collector's trade tape and depth before deciding.")
    P(R.stats(pess, "pessimistic per-decision (for the CI)"))
    P(f"\nfinished in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
