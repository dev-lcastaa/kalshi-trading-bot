"""Stop-loss replay for the P1 paper setting (docs/PHASE3_PREREGISTRATION.md, addendum 3b). Development splits only.
Stops are net dollars per position of 10 contracts; the entry gate is the bot's locked call with >= 2 agreeing checks.

    python -m aqlabs.research.stop_grid --root C:\\data\\eventstore --decisions decisions.csv.gz
"""
import argparse
import csv
import gzip

import numpy as np

from ..store import EventStore
from . import replay as R

ap = argparse.ArgumentParser()
ap.add_argument("--root", required=True)
ap.add_argument("--decisions", required=True)
args = ap.parse_args()

N = 10
TP_TOTAL = 0.30
CONF = 0.85
STOPS = [("none ($9)", 9.00), ("$5", 5.00), ("$4", 4.00), ("$3", 3.00), ("$2.50", 2.50), ("$2", 2.00)]

grids, markets = R.load_all(EventStore(args.root))
usable = [m for m in markets if m["split"] in ("train", "val", "test")]
print("markets", len(usable), {k: sum(m["split"] == k for m in usable) for k in ("train", "val", "test")}, flush=True)

side, sec = {}, []
with gzip.open(args.decisions, "rt", newline="") as f:
    for row in csv.DictReader(f):
        if int(row["confirmation_agree"] or 0) >= 2:
            side[row["ticker"]] = "yes" if float(row["model_p_yes"]) >= 0.5 else "no"
            sec.append(float(row["seconds_to_expiry"]))
print("gate passes for", len(side), "markets; lock time seconds_to_expiry p5/p50/p95 =",
      np.percentile(sec, [5, 50, 95]).round(0), flush=True)
HI = float(np.percentile(sec, 95))


def summarize(trs):
    if not trs:
        return "n=0"
    p = np.array([t["pnl"] for t in trs])
    wins, losses = p[p > 0], p[p <= 0]
    days = sorted({t["day"] for t in trs})
    daily = np.array([sum(t["pnl"] for t in trs if t["day"] == d) * N for d in days])  # $ per day at 10 contracts
    kinds = {}
    for t in trs:
        kinds[t["kind"]] = kinds.get(t["kind"], 0) + 1
    return (f"n={len(p):4d} mean={p.mean()*100:+6.2f}c win={np.mean(p>0)*100:4.1f}% avgW={wins.mean()*100 if len(wins) else 0:+5.1f}c "
            f"avgL={losses.mean()*100 if len(losses) else 0:+6.1f}c worst={p.min()*100:+6.1f}c "
            f"day$ mean={daily.mean():+6.2f} worst={daily.min():+7.2f} | {kinds}")


def run(stop_total, delay=2, hi=HI):
    return R.run_scalp(usable, grids, conf=CONF, tp=TP_TOTAL / N, stop=stop_total / N, delay=delay, max_cycles=3,
                       lo=90, hi=hi, room=0.02 / N, allowed_side=side, contracts=N)


for hi in (HI,):
    print(f"\n=== locked-call gate, confidence 85%, 10 contracts, target $0.30, entries up to {hi:.0f}s left ===")
    for label, stop in STOPS:
        trs = run(stop, hi=hi)
        print(f"-- stop {label}", flush=True)
        for k in ("train", "val", "test"):
            print(f"   {k:5}", summarize([t for t in trs if t["split"] == k]), flush=True)
        print("   val+test", R.stats([t for t in trs if t["split"] in ("val", "test")], ""), flush=True)

print("\n=== sensitivity: exit delay (val+test) ===")
for label, stop in (STOPS[0], STOPS[3], STOPS[5]):
    for d in (1, 2, 5, 10):
        trs = [t for t in run(stop, delay=d) if t["split"] in ("val", "test")]
        print(f"stop {label:10} delay {d:2d}s", summarize(trs), flush=True)
print("DONE")
