"""Phase 3, H8: thinner markets are less efficient (see Addendum 2 in docs/PHASE3_PREREGISTRATION.md).

    python -m aqlabs.research.phase3_h8 --root <eventstore> --stage validate
    python -m aqlabs.research.phase3_h8 --root <eventstore> --stage confirm --confirm-holdout

Universe: the seven alt-coin series (ETH, XRP, DOGE, BNB, ZEC, NEAR, HYPE). There is no development data for them, so
the first data seen is the validation window. D0 is the first full UTC day after alt markets first appear in the store.
Stage 2 is D0 .. D0+13; stage 3 is D0+14 .. D0+27, evaluated once. The rule is exactly rule A with the frozen
fair-value coefficients, unchanged for every coin.
"""
from __future__ import annotations

import argparse
import datetime as dt
import time

import numpy as np

from ..store import EventStore
from . import phase3 as P3
from . import registry as REG
from . import replay as R

ALT_COINS = ("ETH", "XRP", "DOGE", "BNB", "ZEC", "NEAR", "HYPE")
ALT_INDEX = tuple(f"{c}USD_RTI" for c in ALT_COINS)
WINDOW_DAYS = 14
THR = 0.03
DELAY_S = 2


def _day(d: str) -> dt.date:
    return dt.date.fromisoformat(d)


def windows(markets) -> tuple[dt.date, tuple[dt.date, dt.date], tuple[dt.date, dt.date]]:
    """(D0, stage 2 window, stage 3 window) from the earliest alt market in the store."""
    alts = [m for m in markets if m["coin"] in ALT_INDEX]
    if not alts:
        raise REG.RegistryError("no alt-coin markets in the event store yet; the extended collector has not been "
                                "deployed or has not settled a market (see docs/RUNBOOK.md)")
    d0 = _day(min(m["day"] for m in alts)) + dt.timedelta(days=1)
    w2 = (d0, d0 + dt.timedelta(days=WINDOW_DAYS - 1))
    w3 = (w2[1] + dt.timedelta(days=1), w2[1] + dt.timedelta(days=WINDOW_DAYS))
    return d0, w2, w3


def _in(m, window) -> bool:
    return window[0] <= _day(m["day"]) <= window[1]


def evaluate_rule(grids, mk, delay=DELAY_S):
    frozen = R.frozen_prob()
    zs = [R.signal_arrays(grids[("idx", m["coin"])], m) for m in mk]
    z_shuf = P3.shuffle_within_coin(mk, zs)
    return {"trades": R.run_fair_value(mk, zs, frozen, thr=THR, delay=delay)[0],
            "placebo": R.run_fair_value(mk, z_shuf, frozen, thr=THR, delay=delay)[0],
            "delay5": R.run_fair_value(mk, zs, frozen, thr=THR, delay=5)[0]}


def traded_volume_per_market(markets) -> dict[str, float]:
    """Mean contracts traded per market, by coin (cumulative volume at the last tick minus the first)."""
    out: dict[str, list[float]] = {}
    for m in markets:
        vol = np.asarray(m["q"][:, 6], dtype=float)
        vol = vol[np.isfinite(vol)]
        if len(vol):
            out.setdefault(m["coin"], []).append(float(vol[-1] - vol[0]))
    return {c: float(np.mean(v)) for c, v in out.items()}


def descriptive(grids, mk_alt, mk_core, P) -> dict:
    """Reported, not gated: per-coin results, the same rule on BTC and SOL, and the liquidity ranking."""
    frozen = R.frozen_prob()
    out = {"per_coin": {}}
    all_mk = mk_alt + mk_core
    for coin in sorted({m["coin"] for m in all_mk}):
        part = [m for m in all_mk if m["coin"] == coin]
        zs = [R.signal_arrays(grids[("idx", coin)], m) for m in part]
        trades = R.run_fair_value(part, zs, frozen, thr=THR, delay=DELAY_S)[0]
        out["per_coin"][coin] = {"n": len(trades), "mean_c": P3._mean_c(trades), "markets": len(part)}
        P(R.stats(trades, f"   {coin}"))
    vol = traded_volume_per_market(all_mk)
    coins = [c for c in out["per_coin"] if c in vol and out["per_coin"][c]["n"] >= 20]
    if len(coins) >= 4:
        from scipy.stats import spearmanr
        rho = spearmanr([vol[c] for c in coins], [out["per_coin"][c]["mean_c"] for c in coins]).statistic
        out["spearman_volume_vs_net_mean"] = float(rho)
        P(f"   rank correlation across {len(coins)} coins of traded volume per market with net mean: {rho:+.2f} "
          f"(negative = thinner markets earn more)")
    out["volume_per_market"] = vol
    return out


def evaluate(markets, grids, stage: str, today: dt.date | None, allow_partial: bool, P):
    d0, w2, w3 = windows(markets)
    window = w2 if stage == "validate" else w3
    today = today or dt.datetime.now(dt.UTC).date()
    mk_alt = [m for m in markets if m["coin"] in ALT_INDEX and _in(m, window)]
    mk_core = [m for m in markets if m["coin"] in R.CORE_COINS and _in(m, window)]
    last = max((_day(m["day"]) for m in mk_alt), default=None)
    complete = last is not None and last >= window[1] and today > window[1]
    P(f"H8 {stage}: D0 = {d0}; window {window[0]}..{window[1]}; {len(mk_alt)} alt markets, last market day {last}; today {today}")
    if not complete and not allow_partial:
        raise REG.RegistryError(f"the window {window[0]}..{window[1]} is not complete (last market day {last}); "
                                f"use --allow-partial for an unlogged dry run")
    if not mk_alt:
        raise REG.RegistryError("no alt markets inside the window")
    ev = evaluate_rule(grids, mk_alt)
    P(R.stats(ev["trades"], "H8 alts pooled, rule A"))
    P(R.stats(ev["placebo"], "H8 placebo (z shuffled within coin)"))
    P(R.stats(ev["delay5"], "H8 delay 5 s"))
    chk = P3.validation_check(ev["trades"], ev["placebo"], ev["delay5"])
    P("criteria: " + ", ".join(f"{k}={'ok' if v else 'FAIL'}" for k, v in chk["criteria"].items()))
    P("\ndescriptive (not gated), same rule per coin over the same days:")
    chk["descriptive"] = descriptive(grids, mk_alt, mk_core, P)
    chk.update(d0=str(d0), window=[str(window[0]), str(window[1])])
    P(f"==> H8 {stage}: {'PASS' if chk['passed'] else 'FAIL'}" + ("" if complete else "   [DRY RUN, not logged]"))
    return chk, complete


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--stage", choices=("validate", "confirm"), required=True)
    ap.add_argument("--out")
    ap.add_argument("--allow-partial", action="store_true")
    ap.add_argument("--confirm-holdout", action="store_true", help="required for stage 3 (the second, fresh window)")
    args = ap.parse_args()
    if args.stage == "confirm":
        if not args.confirm_holdout:
            raise SystemExit("stage 3 is evaluated once: pass --confirm-holdout")
        REG.require_prerequisite(args.root, "H8", "confirm")
    P, store, t0 = P3._Out(args.out), EventStore(args.root), time.time()
    grids, markets = R.load_all(store)
    chk, complete = evaluate(markets, grids, args.stage, None, args.allow_partial, P)
    if complete:
        REG.log_run(args.root, "H8", args.stage, ["alts"], {"thr": THR, "delay_s": DELAY_S, "coefs": R.COEFFICIENTS,
                                                              "window_days": WINDOW_DAYS, "alts": ALT_COINS},
                    P3._json_safe(chk), chk["passed"], {t: mm["fingerprint"] for t, mm in store.manifest().items()})
    P(f"\nfinished in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
