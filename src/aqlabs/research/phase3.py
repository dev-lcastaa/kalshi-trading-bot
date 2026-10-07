"""Phase 3, hypotheses H1 to H3 (see docs/PHASE3_PREREGISTRATION.md, which fixes every setting used here).

    python -m aqlabs.research.phase3 --root <eventstore> --stage screen --hypotheses H1,H2,H3
    python -m aqlabs.research.phase3 --root <eventstore> --stage validate --hypotheses H1,H2,H3,H1b
    python -m aqlabs.research.phase3 --root <eventstore> --stage confirm --hypotheses ... --confirm-holdout --holdout-end YYYY-MM-DD

Stages are enforced through the registry: validate needs a passed screen, confirm needs a passed validation and an
explicit flag. H4 lives in `phase3_h4`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import time

import numpy as np
from scipy.special import ndtr

from ..costs import taker_fee_array as fee
from ..store import EventStore
from . import registry as REG
from . import replay as R

EPOCH = dt.date(1970, 1, 1)
EXCHANGES = ("coinbase", "kraken", "bitstamp")
THR = 0.03  # fair-value rule, after fee
H3_THR = 0.02  # final-minute rule, after fee (pre-registered: the edge ceiling at extreme prices is small)
H3_CLIP = (0.005, 0.995)
PLACEBO_LAG_S = 3600
HORIZON_S, BACK_S = 10, 5


def day_number(day: str) -> int:
    return (dt.date.fromisoformat(day) - EPOCH).days


def split_days(split: str) -> tuple[int, int]:
    a, b = R.SPLITS[split]
    return day_number(a), day_number(b)


# ------------------------------------------------------------------ statistics shared by the stage checks
def day_ci(trades, lo_pct=5, hi_pct=95, boot=2000, seed=7) -> tuple[float, float, float]:
    """(mean, lower, upper) of net P/L per trade in cents, with a bootstrap over days (as in replay.stats)."""
    if not trades:
        return float("nan"), float("nan"), float("nan")
    pnl = np.array([t["pnl"] for t in trades])
    days = sorted({t["day"] for t in trades})
    S = np.array([sum(t["pnl"] for t in trades if t["day"] == d) for d in days])
    N = np.array([sum(1 for t in trades if t["day"] == d) for d in days], dtype=float)
    k = np.random.default_rng(seed).integers(0, len(days), size=(boot, len(days)))
    lo, hi = np.percentile(S[k].sum(1) / np.maximum(N[k].sum(1), 1), [lo_pct, hi_pct])
    return float(pnl.mean() * 100), float(lo * 100), float(hi * 100)


def _mean_c(trades) -> float:
    return float(np.mean([t["pnl"] for t in trades])) * 100 if trades else float("nan")


def shuffle_within_coin(markets, items, seed=11):
    out = list(items)
    rng = np.random.default_rng(seed)
    for coin in sorted({m["coin"] for m in markets}):  # "BRTI" sorts before "SOLUSD_RTI": same order as before
        ids = [i for i, m in enumerate(markets) if m["coin"] == coin]
        for a, b in zip(ids, rng.permutation(ids)):
            out[a] = items[b]
    return out


# ------------------------------------------------------------------ H1: cross-exchange lead-lag
def basis_series(idx_grid: dict, ext_grids: list[dict]) -> np.ndarray:
    """ln(exchange) - ln(index) on the index's 1 s grid; the mean over the exchanges that are fresh, NaN if none."""
    n = len(idx_grid["V"])
    acc, cnt = np.zeros(n), np.zeros(n)
    for g in ext_grids:
        j = np.arange(n) + idx_grid["s0"] - g["s0"]
        inb = (j >= 0) & (j < len(g["V"]))
        jc = np.clip(j, 0, len(g["V"]) - 1)
        ok = inb & g["valid"][jc] & idx_grid["valid"]
        b = np.log(np.where(ok, g["V"][jc], 1.0)) - np.log(np.where(ok, idx_grid["V"], 1.0))
        acc += np.where(ok, b, 0.0)
        cnt += ok
    return np.where(cnt > 0, acc / np.maximum(cnt, 1), np.nan)


def lead_lag_data(idx_grid: dict, basis: np.ndarray, h: int = HORIZON_S, back: int = BACK_S):
    """Rows (day, y, X): y = ln(BRTI(t+h)/BRTI(t)), X = [basis(t), ln(BRTI(t)/BRTI(t-back))]."""
    V, valid = idx_grid["V"], idx_grid["valid"]
    i = np.arange(back, len(V) - h)
    keep = valid[i] & valid[i + h] & valid[i - back] & np.isfinite(basis[i])
    i = i[keep]
    y = np.log(V[i + h] / V[i])
    X = np.column_stack([basis[i], np.log(V[i] / V[i - back])])
    return (idx_grid["s0"] + i) // 86400, y, X


def ols_clustered(X, y, day):
    """OLS without intercept, with day-clustered standard errors. `day` must be non-decreasing."""
    xtx = X.T @ X
    beta = np.linalg.solve(xtx, X.T @ y)
    e = y - X @ beta
    starts = np.r_[0, np.flatnonzero(np.diff(day)) + 1]
    scores = np.add.reduceat(X * e[:, None], starts, axis=0)
    xi = np.linalg.inv(xtx)
    return beta, np.sqrt(np.diag(xi @ (scores.T @ scores) @ xi))


def oos_r2(X, y, beta) -> float:
    """R^2 against a zero forecast (returns have no meaningful drift at this horizon)."""
    denom = float(np.sum(y * y))
    return float(1 - np.sum((y - X @ beta) ** 2) / denom) if denom > 0 else float("nan")


def in_days(day, lo: int, hi: int):
    return (day >= lo) & (day <= hi)


def h1_measurement(grids, bases: dict, fit_split: str, test_splits: tuple[str, ...]) -> dict:
    out = {}
    for coin, basis in bases.items():
        day, y, X = lead_lag_data(grids[("idx", coin)], basis)
        tr = in_days(day, *split_days(fit_split))
        beta, se = ols_clustered(X[tr], y[tr], day[tr])
        out[coin] = {"beta": beta.tolist(), "t": (beta / se).tolist(), "n_fit": int(tr.sum()),
                     "r2": {sp: oos_r2(X[m], y[m], beta) for sp in test_splits
                            for m in [in_days(day, *split_days(sp))]}}
    return out


def adjusted_grid(grid: dict, basis: np.ndarray, beta: float, lag_s: int = 0) -> dict:
    """The index level corrected by the exchange basis: BRTI * exp(beta * basis). `lag_s` > 0 is the stale placebo."""
    b = basis
    if lag_s:
        b = np.full(len(basis), np.nan)
        b[lag_s:] = basis[:-lag_s]
    return dict(grid, V=grid["V"] * np.exp(beta * np.where(np.isfinite(b), b, 0.0)))


def eval_h1(grids, mk, betas: dict, bases: dict, delay=2):
    frozen = R.frozen_prob()
    cache = {(c, lag): adjusted_grid(grids[("idx", c)], bases[c], betas[c], lag)
             for c in bases for lag in (0, PLACEBO_LAG_S)}

    def run(lag, d):
        zs = [R.signal_arrays(cache[(m["coin"], lag)], m) for m in mk]
        return R.run_fair_value(mk, zs, frozen, thr=THR, delay=d)[0]
    return {"trades": run(0, delay), "placebo": run(PLACEBO_LAG_S, delay), "delay5": run(0, 5)}


# ------------------------------------------------------------------ H2: faster volatility
def eval_h2(grids, mk, delay=2):
    frozen = R.frozen_prob()
    z_new = [R.signal_arrays(grids[("idx_ewma", m["coin"])], m) for m in mk]
    z_shuf = shuffle_within_coin(mk, z_new)
    return {"trades": R.run_fair_value(mk, z_new, frozen, thr=THR, delay=delay)[0],
            "placebo": R.run_fair_value(mk, z_shuf, frozen, thr=THR, delay=delay)[0],
            "delay5": R.run_fair_value(mk, z_new, frozen, thr=THR, delay=5)[0]}


def h2_information(grids, mk) -> dict:
    """Paired Brier / log-loss difference of Phi(z): positive = the faster volatility estimate is better."""
    rows = {"brier": [], "logloss": []}
    for m in mk:
        z0, ok0, ml = R.signal_arrays(grids[("idx", m["coin"])], m)
        z1, ok1, _ = R.signal_arrays(grids[("idx_ewma", m["coin"])], m)
        sl = m["close_s"] - m["s"]
        pick = np.flatnonzero(ok0 & ok1 & (sl >= 240) & (sl <= 840) & (sl % 60 == 0))
        if not len(pick):
            continue
        p0, p1 = np.clip(ndtr(z0[pick]), 1e-4, 1 - 1e-4), np.clip(ndtr(z1[pick]), 1e-4, 1 - 1e-4)
        y = m["y"]
        for name, f in (("brier", lambda p: (p - y) ** 2),
                        ("logloss", lambda p: -(y * np.log(p) + (1 - y) * np.log(1 - p)))):
            for d in f(p0) - f(p1):
                rows[name].append({"pnl": float(d), "market": m["ticker"], "day": m["day"], "split": m["split"]})
    return rows


# ------------------------------------------------------------------ H3: the final-minute settlement average
def final_minute_p(grid: dict, m: dict, clip=H3_CLIP):
    """(p, ok) aligned with the market's per-second grid. For the last 60 seconds the settlement average S is
    Normal(mean = (observed sum + r * X) / 60, var = sigma_p^2 * r(r+1)(2r+1)/6 / 3600), r = seconds still to come."""
    p, ok = np.full(len(m["s"]), np.nan), np.zeros(len(m["s"]), dtype=bool)
    a = m["close_s"] - 60 - grid["s0"]
    if a < 0 or a + 60 > len(grid["V"]):
        return p, ok
    v, vok, sig = grid["V"][a:a + 60], grid["valid"][a:a + 60], grid["sigma"][a:a + 60]
    q = np.arange(60)
    r = 59 - q
    mean = (np.cumsum(np.where(vok, v, 0.0)) + r * v) / 60.0
    sd = np.sqrt((v * sig / np.sqrt(60.0)) ** 2 * r * (r + 1) * (2 * r + 1) / 6.0 / 3600.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        prob = np.where(sd > 0, ndtr((mean - m["strike"]) / sd), (mean >= m["strike"]).astype(float))
    good = vok & (np.cumsum(vok) == q + 1) & np.isfinite(sig) & np.isfinite(prob)
    p[-60:], ok[-60:] = np.clip(prob, *clip), good
    return p, ok


def run_final_minute(markets, ps, thr=H3_THR, delay=2, s_lo=10, s_hi=55):
    trades = []
    for m, (p, ok) in zip(markets, ps):
        sl = m["close_s"] - m["s"]
        bid, ask = m["bid"], m["ask"]
        win = ok & (sl >= s_lo) & (sl <= s_hi) & m["qok"]
        with np.errstate(invalid="ignore"):
            cy = win & (p > 0.5) & (p - ask - fee(ask) >= thr) & (m["asz"] >= 1) & (ask < 1)
            cn = win & (p < 0.5) & ((1 - p) - (1 - bid) - fee(1 - bid) >= thr) & (m["bsz"] >= 1) & (bid > 0)
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
            break  # one entry per market: the first fillable qualifying second
    return trades


def eval_h3(grids, mk, delay=2):
    ps = [final_minute_p(grids[("idx", m["coin"])], m) for m in mk]
    return {"trades": run_final_minute(mk, ps, delay=delay),
            "placebo": run_final_minute(mk, shuffle_within_coin(mk, ps), delay=delay),
            "delay5": run_final_minute(mk, ps, delay=5)}


# ------------------------------------------------------------------ stage rules (from the pre-registration)
def screen_check(trades, placebo) -> dict:
    val = [t for t in trades if t["split"] == "val"]
    test = [t for t in trades if t["split"] == "test"]
    pooled = val + test
    crit = {
        "pooled_mean_ge_0.5c": _mean_c(pooled) >= 0.5,
        "n_ge_300": len(pooled) >= 300,
        "val_mean_gt_0": _mean_c(val) > 0,
        "test_mean_gt_0": _mean_c(test) > 0,
        "placebo_pooled_lt_0": _mean_c([t for t in placebo if t["split"] in ("val", "test")]) < 0,
    }
    return {"criteria": crit, "passed": all(bool(v) for v in crit.values()), "pooled_mean_c": _mean_c(pooled),
            "val_mean_c": _mean_c(val), "test_mean_c": _mean_c(test), "n_pooled": len(pooled)}


def validation_check(trades, placebo, delay5) -> dict:
    mean, lo, hi = day_ci(trades)
    days = sorted({t["day"] for t in trades})
    half = len(days) // 2
    first = [t for t in trades if t["day"] in days[:half]]
    second = [t for t in trades if t["day"] in days[half:]]
    crit = {
        "mean_gt_0": mean > 0,
        "ci90_lower_gt_0": lo > 0,
        "n_ge_300": len(trades) >= 300,
        "both_halves_positive": bool(first and second and _mean_c(first) > 0 and _mean_c(second) > 0),
        "placebo_lt_0": _mean_c(placebo) < 0,
        "delay5_gt_0": _mean_c(delay5) > 0,
    }
    return {"criteria": crit, "passed": all(bool(v) for v in crit.values()), "mean_c": mean, "ci90_c": [lo, hi],
            "n": len(trades), "halves_c": [_mean_c(first), _mean_c(second)], "placebo_c": _mean_c(placebo),
            "delay5_c": _mean_c(delay5)}


# ------------------------------------------------------------------ orchestration
def _spec(h: str) -> dict:
    base = {"thr": THR, "delay_s": 2, "max_entries": 5, "gap_s": 60, "window_min": [4, 14], "coefs": R.COEFFICIENTS}
    return {
        "H1": {**base, "exchange": "coinbase", "horizon_s": HORIZON_S, "back_s": BACK_S, "placebo_lag_s": PLACEBO_LAG_S},
        "H1b": {**base, "exchanges": list(EXCHANGES), "horizon_s": HORIZON_S, "back_s": BACK_S},
        "H2": {**base, "half_life_min": 8.0},
        "H3": {"thr": H3_THR, "delay_s": 2, "window_s": [10, 55], "clip": H3_CLIP, "entries": 1},
    }[h]


def _fingerprints(store: EventStore) -> dict:
    return {t: m["fingerprint"] for t, m in store.manifest().items()}


class _Out:
    def __init__(self, path):
        self.fh = open(path, "w", encoding="utf-8") if path else None

    def __call__(self, s=""):
        print(s, flush=True)
        if self.fh:
            self.fh.write(s + "\n")


def _split_lines(P, trades, label):
    P(R.stats(trades, f"{label} | ALL"))
    for k in ("train", "val", "test"):
        part = [t for t in trades if t["split"] == k]
        if part:
            P(R.stats(part, f"   {k}"))


def screen_h1(P, grids, markets, usable) -> dict:
    P("\n== H1 lead-lag (Coinbase, stage 1)")
    bases = {c: basis_series(grids[("idx", c)], [grids[("cb", c)]]) for c in ("BRTI", "SOLUSD_RTI")}
    meas = h1_measurement(grids, bases, "train", ("val", "test"))
    for c, r in meas.items():
        P(f"{c}: fit on train n={r['n_fit']:,}  beta(basis)={r['beta'][0]:+.4f} (t={r['t'][0]:+.1f})  "
          f"beta(past 5s)={r['beta'][1]:+.4f} (t={r['t'][1]:+.1f})  OOS R^2 val={r['r2']['val']:+.5f} test={r['r2']['test']:+.5f}")
    gate = all(r["r2"][s] > 0 for r in meas.values() for s in ("val", "test"))
    P(f"measurement gate (OOS R^2 > 0 in val and test, both coins): {'PASS' if gate else 'FAIL'}")
    results = {"measurement": meas, "measurement_gate": gate}
    if not gate:
        results.update(passed=False, reason="measurement gate failed; trading test not run")
        return results
    ev = eval_h1(grids, usable, {c: r["beta"][0] for c, r in meas.items()}, bases)
    _split_lines(P, ev["trades"], "H1 trades")
    P(R.stats(ev["placebo"], "H1 placebo (basis 3600 s stale) | ALL"))
    chk = screen_check(ev["trades"], ev["placebo"])
    results.update(chk)
    return results


def screen_h2(P, grids, markets, usable) -> dict:
    P("\n== H2 faster volatility (stage 1)")
    info = h2_information(grids, usable)
    gate = True
    results = {}
    for name in ("brier", "logloss"):
        for sp in ("val", "test"):
            part = [t for t in info[name] if t["split"] == sp]
            mean, lo, hi = day_ci(part)
            P(f"{name:8} {sp:5} mean improvement (old - new) = {mean:+.4f}e-2  90% CI [{lo:+.4f}, {hi:+.4f}]e-2  n={len(part)}")
            results[f"{name}_{sp}_improvement_x100"] = mean
            gate &= mean > 0
    P(f"information gate (better on val and test, both metrics): {'PASS' if gate else 'FAIL'}")
    results["information_gate"] = gate
    ev = eval_h2(grids, usable)
    _split_lines(P, ev["trades"], "H2 trades")
    P(R.stats(ev["placebo"], "H2 placebo (z shuffled) | ALL"))
    results.update(screen_check(ev["trades"], ev["placebo"]))
    # the trading screen is the stage rule; the information gate is reported and also required
    results["passed"] = bool(results["passed"] and gate)
    return results


def screen_h3(P, grids, markets, usable) -> dict:
    P("\n== H3 final-minute settlement average (stage 1)")
    ev = eval_h3(grids, usable)
    _split_lines(P, ev["trades"], "H3 trades")
    P(R.stats(ev["placebo"], "H3 placebo (probabilities shuffled) | ALL"))
    if ev["trades"]:
        prices = np.array([t["price"] for t in ev["trades"]])
        P(f"   entry price: median {np.median(prices):.2f}, share above 0.90 = {np.mean(prices > 0.9) * 100:.0f}%")
    return screen_check(ev["trades"], ev["placebo"])


SCREENS = {"H1": screen_h1, "H2": screen_h2, "H3": screen_h3}


def _json_safe(x):
    if isinstance(x, dict):
        return {str(k): _json_safe(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_json_safe(v) for v in x]
    if isinstance(x, (np.floating, np.integer, np.bool_)):
        return x.item()
    return x


def run_screen(P, store, hyps, root):
    grids, markets = R.load_all(store, with_ewma="H2" in hyps)
    usable = [m for m in markets if m["split"] in ("train", "val", "test")]
    P(f"stage 1 screen on development data: {len(usable)} markets (train/val/test); fwd and holdout untouched")
    for h in hyps:
        results = SCREENS[h](P, grids, markets, usable)
        passed = bool(results.get("passed", False))
        crit = results.get("criteria")
        if crit:
            P("criteria: " + ", ".join(f"{k}={'ok' if v else 'FAIL'}" for k, v in crit.items()))
        P(f"==> {h} stage 1: {'PASS' if passed else 'FAIL'}")
        REG.log_run(root, h, "screen", ["train", "val", "test"], _spec(h), _json_safe(results), passed,
                    _fingerprints(store))


def _validation_markets(markets, split, last_n_days=None):
    mk = [m for m in markets if m["split"] == split]
    if last_n_days:
        days = sorted({m["day"] for m in mk})[-last_n_days:]
        mk = [m for m in mk if m["day"] in days]
    return mk


def run_validation(P, store, hyps, root, stage, allow_partial, holdout_end):
    """Stage 2 (fwd) or stage 3 (holdout). Frozen settings only: nothing is tuned here."""
    split = "fwd" if stage == "validate" else "holdout"
    for h in hyps:
        REG.require_prerequisite(root, h, stage)
    grids, markets = R.load_all(store, sources=EXCHANGES if "H1b" in hyps else (), with_ewma="H2" in hyps)
    if stage == "confirm":
        for m in markets:
            if m["split"] == "holdout" and m["day"] > holdout_end:
                m["split"] = "after"
    mk_all = [m for m in _validation_markets(markets, split) if m["coin"] in R.CORE_COINS]
    last_day = max((m["day"] for m in mk_all), default=None)
    want_last = R.SPLITS["fwd"][1] if stage == "validate" else holdout_end
    complete = last_day is not None and last_day >= want_last
    P(f"{stage} on '{split}': {len(mk_all)} markets, last market day {last_day}, period ends {want_last}")
    if not complete and not allow_partial:
        raise REG.RegistryError(f"{split} period is incomplete (last market day {last_day}, needs {want_last}); "
                                f"use --allow-partial for an unlogged dry run")
    for h in hyps:
        P(f"\n== {h} {stage}")
        mk = mk_all  # per hypothesis: H1b trims the fit days and must not shrink the sample for later hypotheses
        if h == "H1":
            train_bases = {c: basis_series(grids[("idx", c)], [grids[("cb", c)]]) for c in ("BRTI", "SOLUSD_RTI")}
            meas = h1_measurement(grids, train_bases, "train", ())
            ev = eval_h1(grids, mk, {c: r["beta"][0] for c, r in meas.items()}, train_bases)
        elif h == "H1b":
            bases = {c: basis_series(grids[("idx", c)], [grids[(f"ext:{s}", c)] for s in EXCHANGES
                                                          if (f"ext:{s}", c) in grids]) for c in ("BRTI", "SOLUSD_RTI")}
            days = sorted({m["day"] for m in mk})
            fit_days = days[:7]
            lo_d, hi_d = day_number(fit_days[0]), day_number(fit_days[-1])
            betas = {}
            for c, basis in bases.items():
                day, y, X = lead_lag_data(grids[("idx", c)], basis)
                tr = in_days(day, lo_d, hi_d)
                betas[c] = float(ols_clustered(X[tr], y[tr], day[tr])[0][0])
                P(f"{c}: beta(basis) fit on {fit_days[0]}..{fit_days[-1]} = {betas[c]:+.4f}")
            mk = [m for m in mk if m["day"] not in fit_days]
            ev = eval_h1(grids, mk, betas, bases)
        elif h == "H2":
            ev = eval_h2(grids, mk)
        elif h == "H3":
            ev = eval_h3(grids, mk)
        else:
            raise ValueError(f"unknown hypothesis {h}")
        chk = validation_check(ev["trades"], ev["placebo"], ev["delay5"])
        P(R.stats(ev["trades"], f"{h} trades"))
        P(R.stats(ev["placebo"], f"{h} placebo"))
        P(R.stats(ev["delay5"], f"{h} delay 5 s"))
        P("criteria: " + ", ".join(f"{k}={'ok' if v else 'FAIL'}" for k, v in chk["criteria"].items()))
        P(f"==> {h} {stage}: {'PASS' if chk['passed'] else 'FAIL'}" + ("   [DRY RUN, not logged]" if not complete else ""))
        if complete:
            REG.log_run(root, h, stage, [split], _spec(h), _json_safe(chk), chk["passed"], _fingerprints(store))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--stage", choices=("screen", "validate", "confirm"), required=True)
    ap.add_argument("--hypotheses", default="H1,H2,H3")
    ap.add_argument("--out")
    ap.add_argument("--allow-partial", action="store_true", help="dry run on an incomplete period (never logged)")
    ap.add_argument("--confirm-holdout", action="store_true", help="required for stage 3; every look is logged")
    ap.add_argument("--holdout-end", help="last day of the holdout window to evaluate (stage 3)")
    args = ap.parse_args()
    hyps = [h.strip() for h in args.hypotheses.split(",") if h.strip()]
    P, store, t0 = _Out(args.out), EventStore(args.root), time.time()
    if args.stage == "screen":
        run_screen(P, store, [h for h in hyps if h in SCREENS], args.root)
    elif args.stage == "validate":
        run_validation(P, store, hyps, args.root, "validate", args.allow_partial, None)
    else:
        if not args.confirm_holdout or not args.holdout_end:
            raise SystemExit("stage 3 looks at the holdout: pass --confirm-holdout and --holdout-end YYYY-MM-DD")
        run_validation(P, store, hyps, args.root, "confirm", args.allow_partial, args.holdout_end)
    P(f"\nfinished in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
