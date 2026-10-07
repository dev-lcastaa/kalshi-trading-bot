"""Phase 3, H5 and H6: the external spec's trading shell (see the addendum in docs/PHASE3_PREREGISTRATION.md).

    python -m aqlabs.research.phase3_shell --root <eventstore> --stage screen --hypotheses H5,H6

H5 runs the spec's validator, risk rules and exits around the frozen fair-value probability. H6 swaps in a model that
adds momentum, acceleration and book imbalance (the spec's model D). Every number below is fixed in the addendum.
Only the stage 1 screen is implemented: stages 2 and 3 are built only if a hypothesis passes the screen.
"""
from __future__ import annotations

import argparse
import collections
import time

import numpy as np

from ..costs import taker_fee_array as fee
from ..store import EventStore
from . import phase3 as P3
from . import registry as REG
from . import replay as R

EDGE = 0.08
PRICE_BAND = (0.20, 0.80)
MAX_SPREAD = 0.03
MAX_QUOTE_AGE_MS = 1000
CHOP_REJECT_AT = 3  # crossings of the strike in the last 60 s
COOLDOWN_S = 20
MAX_CONSECUTIVE_LOSSES = 3
MAX_DAILY_LOSS = 0.25
TAKE_PROFIT = 0.08
STOP = 0.08
DELAY_S = 2
WINDOW = (60, 840)  # seconds before close in which the fair-value model is defined
MODEL_UNDEFINED_BELOW_S = 60
L2 = 1.0
_EPS = 1e-9
REASONS = ("NO_MODEL", "STALE_MARKET_DATA", "SPREAD_TOO_LARGE", "CHOP_DETECTED", "INSUFFICIENT_LIQUIDITY",
           "EDGE_TOO_LOW", "PRICE_OUT_OF_BAND", "CANDIDATE")


# ------------------------------------------------------------------ indicators
def chop_counts(grid: dict, m: dict) -> np.ndarray:
    """Crossings of the strike by the index during the 60 seconds up to and including each second."""
    n = len(m["s"])
    j = m["s"] - grid["s0"]
    lo, hi = max(int(j[0]) - 61, 0), min(int(j[-1]) + 1, len(grid["V"]))
    V, ok = grid["V"][lo:hi], grid["valid"][lo:hi]
    above = V >= m["strike"]
    change = np.zeros(len(V), dtype=int)
    change[1:] = (above[1:] != above[:-1]) & ok[1:] & ok[:-1]
    c = np.cumsum(change)
    k = j - lo
    out = np.zeros(n, dtype=int)
    good = (k >= 60) & (k < len(V))
    out[good] = c[k[good]] - c[k[good] - 60]
    return out


def _side(m: dict, p: np.ndarray, side: str):
    """(model probability of the side, entry price, exit price, size to enter, size to exit, exit price valid)."""
    if side == "yes":
        return p, m["ask"], m["bid"], m["asz"], m["bsz"], m["bid"] > 0
    return 1 - p, 1 - m["bid"], 1 - m["ask"], m["bsz"], m["asz"], m["ask"] < 1


def candidate_masks(m: dict, p: np.ndarray, chop: np.ndarray) -> dict[str, np.ndarray]:
    """The spec's validator, vectorised over the market's seconds. Returns one candidate mask per side."""
    sl = m["close_s"] - m["s"]
    with np.errstate(invalid="ignore"):
        base = (m["qok"] & np.isfinite(p) & (sl >= WINDOW[0]) & (sl <= WINDOW[1]) & (m["qage"] <= MAX_QUOTE_AGE_MS)
                & (chop < CHOP_REJECT_AT) & ((m["ask"] - m["bid"]) <= MAX_SPREAD + _EPS))
        out = {}
        for side in ("yes", "no"):
            ps, price, _, size_in, _, _ = _side(m, p, side)
            out[side] = (base & (ps - price >= EDGE - _EPS) & (price >= PRICE_BAND[0] - _EPS)
                         & (price <= PRICE_BAND[1] + _EPS) & (size_in >= 1))
    return out


def rejection_counts(m: dict, p: np.ndarray, chop: np.ndarray) -> collections.Counter:
    """First failing rule per second inside the decision window, in the spec's order."""
    sl = m["close_s"] - m["s"]
    inwin = (sl >= WINDOW[0]) & (sl <= WINDOW[1])
    with np.errstate(invalid="ignore"):
        e_yes = p - m["ask"]
        e_no = m["bid"] - p
        yes_best = e_yes >= e_no
        edge = np.where(yes_best, e_yes, e_no)
        price = np.where(yes_best, m["ask"], 1 - m["bid"])
        size = np.where(yes_best, m["asz"], m["bsz"])
        code = np.full(len(sl), len(REASONS) - 1)
        fails = [~np.isfinite(p), ~m["qok"] | (m["qage"] > MAX_QUOTE_AGE_MS), (m["ask"] - m["bid"]) > MAX_SPREAD + _EPS,
                 chop >= CHOP_REJECT_AT, size < 1, edge < EDGE - _EPS,
                 (price < PRICE_BAND[0] - _EPS) | (price > PRICE_BAND[1] + _EPS)]
        for k in reversed(range(len(fails))):  # applied last-to-first so that the earliest failing rule wins
            code = np.where(fails[k], k, code)
    counts = np.bincount(code[inwin], minlength=len(REASONS))
    return collections.Counter({REASONS[k]: int(c) for k, c in enumerate(counts) if c})


# ------------------------------------------------------------------ the shell
def run_shell(markets, probs, chops, delay: int = DELAY_S):
    """Markets of ONE coin in time order. Returns (trades, rejection histogram)."""
    trades, rejections = [], collections.Counter()
    state = {"day": None, "consec": 0, "daily": 0.0, "cooldown_until": -10**12}
    for m, p, chop in zip(markets, probs, chops):
        rejections += rejection_counts(m, p, chop)
        if m["day"] != state["day"]:
            state.update(day=m["day"], consec=0, daily=0.0)
        masks = candidate_masks(m, p, chop)
        sl, s, n = m["close_s"] - m["s"], m["s"], len(m["s"])
        i_next = 0
        for i in np.flatnonzero(masks["yes"] | masks["no"]):
            if i < i_next or s[i] < state["cooldown_until"]:
                continue
            if state["consec"] >= MAX_CONSECUTIVE_LOSSES or state["daily"] <= -MAX_DAILY_LOSS:
                continue
            side = "yes" if masks["yes"][i] else "no"
            e = i + delay
            if e >= n - 1 or not masks[side][e]:  # re-validation with the data of the second the order arrives
                continue
            ps, price, exit_px, _, size_out, exit_valid = _side(m, p, side)
            entry = float(price[e])
            tradable = m["qok"] & (size_out >= 1) & exit_valid
            gross = exit_px - entry
            with np.errstate(invalid="ignore"):
                invalid = np.isfinite(ps) & (sl >= MODEL_UNDEFINED_BELOW_S) & (ps - exit_px < 0)
                trig = m["qok"] & tradable & ((gross >= TAKE_PROFIT - _EPS) | (gross <= -STOP + _EPS) | invalid)
            trig[: e + 1] = False
            exit_idx, reason = None, "settle"
            for j in np.flatnonzero(trig):
                later = np.flatnonzero(tradable[j + delay:]) if j + delay < n else []
                if len(later):
                    exit_idx = j + delay + int(later[0])
                    reason = "tp" if gross[j] >= TAKE_PROFIT - _EPS else ("stop" if gross[j] <= -STOP + _EPS else "inval")
                break
            f_in = float(fee(entry))
            if exit_idx is None:
                won = m["y"] if side == "yes" else 1 - m["y"]
                pnl, i_next, exit_s = won - entry - f_in, n, int(m["close_s"])
            else:
                px = float(exit_px[exit_idx])
                pnl = px - float(fee(px)) - entry - f_in
                i_next, exit_s = exit_idx + 1, int(s[exit_idx])
            state["cooldown_until"] = exit_s + COOLDOWN_S
            state["daily"] += pnl
            state["consec"] = state["consec"] + 1 if pnl < 0 else 0
            trades.append(dict(pnl=pnl, market=m["ticker"], day=m["day"], split=m["split"], coin=m["coin"], side=side,
                               price=entry, exit=reason, close_s=int(m["close_s"]), ml=float(sl[i]) / 60,
                               hold_s=(exit_s - int(s[e]))))
    return trades, rejections


def run_by_coin(markets, probs, chops, delay: int = DELAY_S):
    trades, rejections = [], collections.Counter()
    for coin in sorted({m["coin"] for m in markets}):
        idx = [i for i, m in enumerate(markets) if m["coin"] == coin]
        t, r = run_shell([markets[i] for i in idx], [probs[i] for i in idx], [chops[i] for i in idx], delay)
        trades += t
        rejections += r
    return trades, rejections


def summary(trades) -> dict:
    pnl = np.array([t["pnl"] for t in trades])
    if not len(pnl):
        return {"n": 0}
    order = sorted(trades, key=lambda t: t["close_s"])
    curve = np.cumsum([t["pnl"] for t in order])
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    return {"n": int(len(pnl)), "win_rate": float(np.mean(pnl > 0)), "avg_win_c": float(wins.mean() * 100) if len(wins) else 0.0,
            "avg_loss_c": float(losses.mean() * 100) if len(losses) else 0.0,
            "profit_factor": float(wins.sum() / -losses.sum()) if len(losses) else float("inf"),
            "max_drawdown_c": float((np.maximum.accumulate(curve) - curve).max() * 100),
            "exits": dict(collections.Counter(t["exit"] for t in trades))}


# ------------------------------------------------------------------ H6: model D
def d_features(grid: dict, m: dict, p_a: np.ndarray):
    """(X, valid): X = [logit(p_A), momentum, acceleration, book imbalance] per second."""
    n = len(grid["V"])
    j = m["s"] - grid["s0"]
    j0, j30, j60 = np.clip(j, 0, n - 1), np.clip(j - 30, 0, n - 1), np.clip(j - 60, 0, n - 1)
    sig = grid["sigma"][j0]
    ok = ((j >= 60) & (j < n) & grid["valid"][j0] & grid["valid"][j30] & grid["valid"][j60]
          & np.isfinite(sig) & (sig > 0))
    with np.errstate(invalid="ignore", divide="ignore"):
        v0, v30, v60 = grid["V"][j0], grid["V"][j30], grid["V"][j60]
        x1 = np.log(v0 / v60) / sig
        x2 = (np.log(v0 / v30) - np.log(v30 / v60)) / sig
        tot = m["bsz"] + m["asz"]
        x3 = np.where(tot > 0, (m["bsz"] - m["asz"]) / np.maximum(tot, _EPS), np.nan)
    x0 = np.where(np.isfinite(p_a), R.logit(np.where(np.isfinite(p_a), p_a, 0.5)), np.nan)
    X = np.column_stack([x0, x1, x2, x3])
    return X, ok & m["qok"] & np.isfinite(X).all(axis=1)


def fit_d(markets, feats, columns=(0, 1, 2, 3)):
    """Ridge logistic fit on `train` at the minute marks inside the decision window."""
    rows, ys = [], []
    for m, (X, valid) in zip(markets, feats):
        if m["split"] != "train":
            continue
        sl = m["close_s"] - m["s"]
        pick = np.flatnonzero(valid & (sl % 60 == 0) & (sl >= WINDOW[0]) & (sl <= WINDOW[1]) & (m["ask"] < 1) & (m["bid"] > 0))
        if len(pick):
            rows.append(X[pick][:, list(columns)])
            ys.append(np.full(len(pick), m["y"]))
    X, y = np.vstack(rows), np.concatenate(ys)
    return R.fit_ridge_logit(np.column_stack([np.ones(len(y)), X]), y, l2=L2)


def predict_d(feats, w, columns=(0, 1, 2, 3)):
    out = []
    for X, valid in feats:
        p = np.full(len(valid), np.nan)
        z = w[0] + X[valid][:, list(columns)] @ w[1:]
        p[valid] = 1 / (1 + np.exp(-z))
        out.append(p)
    return out


def logloss_common(markets, probs_list) -> list[float]:
    """Mean held-out (val + test) log loss of each probability set, on the seconds where ALL of them are defined."""
    losses = [[] for _ in probs_list]
    for k, m in enumerate(markets):
        if m["split"] not in ("val", "test"):
            continue
        sl = m["close_s"] - m["s"]
        pick = (sl % 60 == 0) & (sl >= WINDOW[0]) & (sl <= WINDOW[1])
        for probs in probs_list:
            pick = pick & np.isfinite(probs[k])
        idx = np.flatnonzero(pick)
        for out, probs in zip(losses, probs_list):
            q = np.clip(probs[k][idx], 1e-4, 1 - 1e-4)
            out.append(-(m["y"] * np.log(q) + (1 - m["y"]) * np.log(1 - q)))
    return [float(np.mean(np.concatenate(v))) for v in losses]


# ------------------------------------------------------------------ the pre-registered stage 1 screen
def shell_screen_check(trades, placebo) -> dict:
    val = [t for t in trades if t["split"] == "val"]
    test = [t for t in trades if t["split"] == "test"]
    pooled = val + test
    crit = {
        "n_dev_ge_100": len(trades) >= 100,
        "n_valtest_ge_40": len(pooled) >= 40,
        "pooled_mean_ge_0.5c": P3._mean_c(pooled) >= 0.5,
        "val_gt_0_with_15": len(val) >= 15 and P3._mean_c(val) > 0,
        "test_gt_0_with_15": len(test) >= 15 and P3._mean_c(test) > 0,
        "placebo_pooled_lt_0": P3._mean_c([t for t in placebo if t["split"] in ("val", "test")]) < 0,
    }
    return {"criteria": crit, "passed": all(bool(v) for v in crit.values()), "pooled_mean_c": P3._mean_c(pooled),
            "val_mean_c": P3._mean_c(val), "test_mean_c": P3._mean_c(test), "n_dev": len(trades), "n_valtest": len(pooled)}


def _lines(P, trades, label):
    P(R.stats(trades, f"{label} | ALL"))
    for k in ("train", "val", "test"):
        part = [t for t in trades if t["split"] == k]
        if part:
            P(R.stats(part, f"   {k}"))
    btc = [t for t in trades if t["coin"] == "BRTI"]
    P(R.stats(btc, f"   BTC only (the spec's stated scope)"))
    s = summary(trades)
    if s["n"]:
        P(f"   win rate {s['win_rate'] * 100:.0f}% | avg win {s['avg_win_c']:+.2f}c | avg loss {s['avg_loss_c']:+.2f}c | "
          f"profit factor {s['profit_factor']:.2f} | max drawdown {s['max_drawdown_c']:.1f}c | exits {s['exits']}")


def screen(P, grids, markets, hyps, root, store):
    usable = [m for m in markets if m["split"] in ("train", "val", "test")]
    usable.sort(key=lambda m: m["close_s"])
    P(f"stage 1 screen on development data: {len(usable)} markets; fwd and holdout untouched")
    zs = [R.signal_arrays(grids[("idx", m["coin"])], m) for m in usable]
    frozen = R.frozen_prob()
    p_a = [np.where(zok, frozen(m, z, ml), np.nan) for m, (z, zok, ml) in zip(usable, zs)]
    chops = [chop_counts(grids[("idx", m["coin"])], m) for m in usable]
    results = {}
    for h in hyps:
        results[h] = {}
        if h == "H5":
            probs = p_a
        else:
            feats = [d_features(grids[("idx", m["coin"])], m, p) for m, p in zip(usable, p_a)]
            w = fit_d(usable, feats)
            P(f"\nH6 model D fitted on train: intercept {w[0]:+.3f}, logit(p_A) {w[1]:+.3f}, momentum {w[2]:+.3f}, "
              f"acceleration {w[3]:+.3f}, book imbalance {w[4]:+.3f}")
            probs = predict_d(feats, w)
            base_ll, d_ll = logloss_common(usable, [p_a, probs])
            P(f"held-out log loss on val+test (same seconds): p_A {base_ll:.5f}  vs  p_D {d_ll:.5f}  (difference {base_ll - d_ll:+.5f}, positive = D better)")
            ablate = {}
            for name, col in (("momentum", 1), ("acceleration", 2), ("book imbalance", 3)):
                cols = tuple(c for c in (0, 1, 2, 3) if c != col)
                ablated = predict_d(feats, fit_d(usable, feats, cols), cols)
                full_ll, cut_ll = logloss_common(usable, [probs, ablated])
                ablate[name] = cut_ll - full_ll
                P(f"   removing {name}: held-out log loss changes by {ablate[name]:+.5f} (positive = it was helping)")
            results[h].update(w=w.tolist(), logloss_A=base_ll, logloss_D=d_ll, ablation=ablate)
        P(f"\n== {h} stage 1 (the spec's shell, {'model A' if h == 'H5' else 'model D'})")
        trades, rej = run_by_coin(usable, probs, chops)
        _lines(P, trades, f"{h} trades")
        top = ", ".join(f"{k}={v:,}" for k, v in rej.most_common())
        P(f"rejection reasons (seconds in the decision window): {top}")
        placebo, _ = run_by_coin(usable, P3.shuffle_within_coin(usable, probs), chops)
        P(R.stats([t for t in placebo if t["split"] in ("val", "test")], "placebo (probabilities shuffled) val+test"))
        chk = shell_screen_check(trades, placebo)
        P("criteria: " + ", ".join(f"{k}={'ok' if v else 'FAIL'}" for k, v in chk["criteria"].items()))
        P(f"==> {h} stage 1: {'PASS' if chk['passed'] else 'FAIL'}")
        results[h].update(chk, summary=summary(trades), rejections=dict(rej))
        REG.log_run(root, h, "screen", ["train", "val", "test"], _spec(h), P3._json_safe(results[h]), chk["passed"],
                    {t: mm["fingerprint"] for t, mm in store.manifest().items()})
    return results


def _spec(h: str) -> dict:
    return {"edge": EDGE, "band": PRICE_BAND, "max_spread": MAX_SPREAD, "quote_age_ms": MAX_QUOTE_AGE_MS,
            "chop_reject_at": CHOP_REJECT_AT, "cooldown_s": COOLDOWN_S, "max_consecutive_losses": MAX_CONSECUTIVE_LOSSES,
            "daily_loss": MAX_DAILY_LOSS, "take_profit": TAKE_PROFIT, "stop": STOP, "delay_s": DELAY_S, "window": WINDOW,
            "model": "fair-value-v1 frozen" if h == "H5" else "model D (ridge logistic, l2=1)"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--stage", choices=("screen",), required=True)
    ap.add_argument("--hypotheses", default="H5,H6")
    ap.add_argument("--out")
    args = ap.parse_args()
    P, store, t0 = P3._Out(args.out), EventStore(args.root), time.time()
    grids, markets = R.load_all(store)
    screen(P, grids, markets, [h.strip() for h in args.hypotheses.split(",") if h.strip()], args.root, store)
    P(f"\nfinished in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
