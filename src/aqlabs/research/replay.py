"""Tick-level replay engine for Kalshi 15-minute crypto markets.

Reads only from the event store. No look-ahead: a decision at second s uses ticks with
ts_ms <= s * 1000 and quotes no older than MAX_AGE_MS. Costs come from `aqlabs.costs`.
"""
from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable

import numpy as np
from scipy.special import ndtr

from ..costs import taker_fee_array as fee
from ..store import EventStore

MAX_AGE_MS = 5000
WIN = 840  # seconds of per-market history kept (T-14:00 .. T-0:01)
COEFFICIENTS = (-0.0199, 0.9142, 0.0294, -0.0268, -0.2871, 0.5018)  # frozen kalshi_bot.prediction.fair_value

SPLITS = {"train": ("2026-09-14", "2026-09-25"), "val": ("2026-09-26", "2026-10-01"), "test": ("2026-10-02", "2026-10-05")}
HOLDOUT_START = "2026-10-06"  # reserved: evaluate once per frozen candidate and log every look

ProbFn = Callable[[dict, np.ndarray, np.ndarray], np.ndarray]


def day_of(close_s: int) -> str:
    return dt.datetime.fromtimestamp(close_s, dt.UTC).strftime("%Y-%m-%d")


def split_of(day: str) -> str:
    for k, (a, b) in SPLITS.items():
        if a <= day <= b:
            return k
    return "holdout" if day >= HOLDOUT_START else "x"


# ------------------------------------------------------------------ data loading
def _filled(a) -> np.ndarray:
    return np.ma.filled(a.astype(float), np.nan) if np.ma.isMaskedArray(a) else np.asarray(a, dtype=float)


def build_coin_grid(ts_ms: np.ndarray, val: np.ndarray) -> dict:
    """1-second grid of the latest tick, its validity and the 30-minute 1-minute-return volatility."""
    s0 = int(ts_ms[0] // 1000)
    s = np.arange(s0, int(ts_ms[-1] // 1000) + 1, dtype=np.int64)
    idx = np.searchsorted(ts_ms, s * 1000, side="right") - 1
    ic = np.clip(idx, 0, None)
    age = s * 1000 - ts_ms[ic]
    V = val[ic]
    valid = (idx >= 0) & (age <= MAX_AGE_MS) & (V > 0)
    n = len(s)
    lv = np.where(valid, np.log(np.where(valid, V, 1.0)), 0.0)
    R = np.full(n, np.nan)
    R[60:] = np.where(valid[60:] & valid[:-60], lv[60:] - lv[:-60], np.nan)
    fin = np.isfinite(R)
    r2, c = np.where(fin, R * R, 0.0), fin.astype(float)
    ss, cn = np.zeros(n), np.zeros(n)
    for k in range(30):
        ss[60 * k:] += r2[: n - 60 * k]
        cn[60 * k:] += c[: n - 60 * k]
    sigma = np.where(cn >= 10, np.sqrt(ss / np.maximum(cn, 1)), np.nan)
    return {"s0": s0, "V": V, "valid": valid, "sigma": sigma}


def load_all(store: EventStore) -> tuple[dict, list[dict]]:
    con = store.connect()
    grids = {}
    for iid in ("BRTI", "SOLUSD_RTI"):
        d = con.execute("select ts_ms, value from index_ticks where index_id = ? order by ts_ms", [iid]).fetchnumpy()
        grids[("idx", iid)] = build_coin_grid(d["ts_ms"].astype(np.int64), _filled(d["value"]))
        if store.has_data("external_ticks"):
            d = con.execute("select ts_ms, price from external_ticks where source = 'coinbase' and index_id = ? "
                            "order by ts_ms", [iid]).fetchnumpy()
            if len(d["ts_ms"]):
                grids[("cb", iid)] = build_coin_grid(d["ts_ms"].astype(np.int64), _filled(d["price"]))
    mk = con.execute("select ticker, index_id, strike, close_ts_ms, result from markets "
                     "where result in ('yes','no') and strike is not null").fetchall()
    mk = {r[0]: r for r in mk}
    q = con.execute("select market_ticker, ts_ms, yes_bid_dollars, yes_ask_dollars, yes_bid_size, yes_ask_size "
                    "from market_ticks order by market_ticker, ts_ms").fetchnumpy()
    tickers = q["market_ticker"]
    cols = np.column_stack([q["ts_ms"].astype(float)] + [_filled(q[c]) for c in
                            ("yes_bid_dollars", "yes_ask_dollars", "yes_bid_size", "yes_ask_size")])
    cut = np.flatnonzero(tickers[1:] != tickers[:-1]) + 1
    starts, ends = np.r_[0, cut], np.r_[cut, len(tickers)]
    markets = []
    for a, b in zip(starts, ends):
        t = tickers[a]
        if t not in mk:
            continue
        _, iid, strike, close_ms, res = mk[t]
        close_s = int(close_ms // 1000)
        day = day_of(close_s)
        markets.append(dict(ticker=t, coin=iid, strike=float(strike), close_s=close_s, y=1.0 if res == "yes" else 0.0,
                            day=day, split=split_of(day), q=cols[a:b]))
    markets.sort(key=lambda m: m["close_s"])
    for m in markets:
        quote_arrays(m)
    return grids, markets


# ------------------------------------------------------------------ per-market grids
def quote_arrays(m: dict) -> None:
    q = m["q"]
    ts = q[:, 0].astype(np.int64)
    s = np.arange(m["close_s"] - WIN, m["close_s"], dtype=np.int64)
    idx = np.searchsorted(ts, s * 1000, side="right") - 1
    ic = np.clip(idx, 0, None)
    ok = (idx >= 0) & (s * 1000 - ts[ic] <= MAX_AGE_MS)
    bid, ask, bsz, asz = q[ic, 1], q[ic, 2], np.nan_to_num(q[ic, 3]), np.nan_to_num(q[ic, 4])
    ok &= np.isfinite(bid) & np.isfinite(ask) & (bid <= ask)
    m.update(s=s, bid=bid, ask=ask, bsz=bsz, asz=asz, qok=ok, mid=(bid + ask) / 2)


def signal_arrays(grid: dict, m: dict, shift: int = 0):
    """(z, ok, minutes_left) aligned with the market's per-second grid; `shift` = seconds the coin price is stale."""
    s = m["s"]
    j = s - grid["s0"] - shift
    n = len(grid["V"])
    jc = np.clip(j, 0, n - 1)
    L, sig = grid["V"][jc], grid["sigma"][jc]
    ml = (m["close_s"] - s) / 60.0
    ok = (j >= 0) & (j < n) & grid["valid"][jc] & np.isfinite(sig) & (ml >= 1.0) & (ml <= 15.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        sd = L * sig * np.sqrt(np.maximum(ml - 1 + 1 / 3, 1e-9))
        z = (L - m["strike"]) / sd
    ok &= np.isfinite(z) & (sd > 0)
    return np.where(ok, z, np.nan), ok, ml


def logit(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def _is_btc(m: dict) -> float:
    return 1.0 if m["coin"] == "BRTI" else 0.0


def frozen_prob(coefs=COEFFICIENTS) -> ProbFn:
    """Fair-value probability with fixed coefficients (matches kalshi_bot.prediction.fair_value)."""
    c0, c1, c2, c3, c4, c5 = coefs

    def prob(m, z, ml):
        lm, lz = logit(m["mid"]), logit(ndtr(np.where(np.isfinite(z), z, 0.0)))
        mm = np.minimum(ml, 15.0) / 15.0
        x = c0 + c1 * lm + c2 * lz + c3 * lz * _is_btc(m) + c4 * lm * mm + c5 * lz * mm
        return 1 / (1 + np.exp(-x))
    return prob


# ------------------------------------------------------------------ fitting (refit variants)
def design(kind: str, mid, z, ml, is_btc: float):
    lm, lz = logit(mid), logit(ndtr(np.where(np.isfinite(z), z, 0.0)))
    mm = np.minimum(ml, 15.0) / 15.0
    one = np.ones_like(lm)
    if kind == "full":
        return np.c_[one, lm, lz, lz * is_btc, lm * mm, lz * mm]
    if kind == "market_only":
        return np.c_[one, lm, lm * mm]
    raise ValueError(kind)


def fit_ridge_logit(X, y, l2=1.0, iters=60):
    w = np.zeros(X.shape[1])
    pen = np.r_[0.0, np.ones(X.shape[1] - 1)] * l2
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w))
        step = np.linalg.solve((X * (p * (1 - p))[:, None]).T @ X + np.diag(pen + 1e-9), X.T @ (p - y) + pen * w)
        w -= step
        if np.max(np.abs(step)) < 1e-8:
            break
    return w


def fit_variant(markets, zs, kind: str, splits=("train",)):
    Xs, ys = [], []
    for m, (z, zok, ml) in zip(markets, zs):
        if m["split"] not in splits:
            continue
        sl = m["close_s"] - m["s"]
        pick = np.flatnonzero(m["qok"] & zok & (sl >= 240) & (sl <= 840) & (sl % 60 == 0) & (m["ask"] < 1) & (m["bid"] > 0))
        if len(pick):
            Xs.append(design(kind, m["mid"][pick], z[pick], ml[pick], _is_btc(m)))
            ys.append(np.full(len(pick), m["y"]))
    X, y = np.vstack(Xs), np.concatenate(ys)
    return fit_ridge_logit(X, y), len(y)


def fitted_prob(kind: str, w) -> ProbFn:
    return lambda m, z, ml: 1 / (1 + np.exp(-(design(kind, m["mid"], z, ml, _is_btc(m)) @ w)))


# ------------------------------------------------------------------ strategy: fair value, hold to settlement
def run_fair_value(markets, zs, prob_fn: ProbFn, thr=0.03, delay=2, slip=0.0, gap=60, max_entries=5, lo=240, hi=840):
    trades, signals, unfilled = [], 0, 0
    for m, (z, zok, ml) in zip(markets, zs):
        sl = m["close_s"] - m["s"]
        p = prob_fn(m, z, ml)
        bid, ask = m["bid"], m["ask"]
        okq = m["qok"] & zok & (sl >= lo) & (sl <= hi)
        with np.errstate(invalid="ignore"):
            ey = p - ask - fee(ask) - slip
            en = (1 - p) - (1 - bid) - fee(1 - bid) - slip
        cand_y = okq & (p > 0.5) & (ey >= thr) & (m["asz"] >= 1) & (ask < 1)
        cand_n = okq & (p < 0.5) & (en >= thr) & (m["bsz"] >= 1) & (bid > 0)
        last, side, buys = -10**9, None, 0
        for i in np.flatnonzero(cand_y | cand_n):
            if buys >= max_entries:
                break
            if i - last < gap:
                continue
            sd_ = "yes" if cand_y[i] else "no"
            if side is not None and sd_ != side:
                continue
            signals += 1
            e = i + delay
            if e >= len(ask) or not m["qok"][e]:
                unfilled += 1
                continue
            if sd_ == "yes":
                if not (m["asz"][e] >= 1 and m["ask"][e] < 1):
                    unfilled += 1
                    continue
                price, won = float(m["ask"][e]), m["y"]
            else:
                if not (m["bsz"][e] >= 1 and m["bid"][e] > 0):
                    unfilled += 1
                    continue
                price, won = float(1 - m["bid"][e]), 1 - m["y"]
            f = float(fee(price))
            trades.append(dict(pnl=won - price - f - slip, gross=won - price, fee=f, market=m["ticker"], day=m["day"],
                               split=m["split"], coin=m["coin"], ml=float(ml[i]), side=sd_, price=price))
            side, last, buys = sd_, i, buys + 1
    return trades, dict(signals=signals, unfilled=unfilled)


def run_blind(markets, side: str, delay=2, at_left=390):
    out = []
    for m in markets:
        e = WIN - at_left + delay
        if not (m["qok"][e] and m["asz"][e] >= 1 and m["bsz"][e] >= 1 and m["ask"][e] < 1 and m["bid"][e] > 0):
            continue
        price = float(m["ask"][e]) if side == "yes" else float(1 - m["bid"][e])
        won = m["y"] if side == "yes" else 1 - m["y"]
        f = float(fee(price))
        out.append(dict(pnl=won - price - f, gross=won - price, fee=f, market=m["ticker"], day=m["day"], split=m["split"],
                        coin=m["coin"], ml=at_left / 60, side=side, price=price))
    return out


# ------------------------------------------------------------------ strategy: momentum scalping (legacy, kept to prove it loses)
def run_scalp(markets, grids, conf=0.65, tp=0.03, stop=0.05, delay=2, ref=15, max_cycles=3, cooldown=30,
              lo=90, hi=810, max_spread=0.03, room=0.02, pmin=0.05, pmax=0.95):
    trades = []
    for m in markets:
        g = grids[("idx", m["coin"])]
        j = m["s"] - g["s0"]
        n = len(g["V"])
        jc, jc60 = np.clip(j, 0, n - 1), np.clip(j - 60, 0, n - 1)
        V, V60 = g["V"][jc], g["V"][jc60]
        vok = g["valid"][jc] & g["valid"][jc60] & (j >= 60) & (j < n)
        bid, ask, bsz, asz, qok = m["bid"], m["ask"], m["bsz"], m["asz"], m["qok"]
        L = len(bid)
        sl = m["close_s"] - m["s"]
        r = np.clip(np.arange(L) - ref, 0, None)
        dbid, dask = bid - bid[r], ask - ask[r]
        up = qok & qok[r] & (dbid >= 0.01 - 1e-9) & (dask >= 0.01 - 1e-9) & vok & (V > V60)
        dn = qok & qok[r] & (dbid <= -0.01 + 1e-9) & (dask <= -0.01 + 1e-9) & vok & (V < V60)
        mid = (bid + ask) / 2
        win = (sl >= lo) & (sl <= hi) & ((ask - bid) <= max_spread + 1e-9)
        cy = up & win & (mid >= conf) & (ask >= pmin) & (ask <= pmax) & (asz >= 1)
        cn_ = dn & win & ((1 - mid) >= conf) & ((1 - bid) >= pmin) & ((1 - bid) <= pmax) & (bsz >= 1)
        vy, vn = bid - fee(bid), (1 - ask) - fee(1 - ask)
        free_from, cycles = 0, 0
        for i in np.flatnonzero(cy | cn_):
            if cycles >= max_cycles:
                break
            if i < free_from:
                continue
            e = i + delay
            if e >= L - 1 or not qok[e]:
                continue
            is_yes = bool(cy[i])
            if is_yes:
                if not (asz[e] >= 1 and ask[e] < 1):
                    continue
                price, won, val_now = float(ask[e]), m["y"], (float(vy[e]) if bsz[e] >= 1 else None)
            else:
                if not (bsz[e] >= 1 and bid[e] > 0):
                    continue
                price, won, val_now = float(1 - bid[e]), 1 - m["y"], (float(vn[e]) if asz[e] >= 1 else None)
            cost = price + float(fee(price))
            if val_now is not None and (cost - val_now) > stop - room + 1e-9:
                continue
            cycles += 1
            val = vy if is_yes else vn
            exit_ok = (bsz >= 1) if is_yes else (asz >= 1)
            net = val - cost
            trig = np.flatnonzero(qok & exit_ok & ((net >= tp - 1e-9) | (net <= -stop + 1e-9)))
            done = None
            for t in trig[trig > e]:
                x = t + delay
                if x < L and qok[x] and exit_ok[x]:
                    done = (x, float(val[x]) - cost)
                    break
            if done is None:
                pnl, free_from, kind = won - cost, 10**9, "settle"
            else:
                pnl = done[1]
                free_from, kind = (done[0] + cooldown, "tp") if pnl > 0 else (10**9, "stop")
            trades.append(dict(pnl=pnl, market=m["ticker"], day=m["day"], split=m["split"], coin=m["coin"],
                               ml=float(sl[i]) / 60, side="yes" if is_yes else "no", price=price, kind=kind))
    return trades


# ------------------------------------------------------------------ statistics
def stats(trades, label="", boot=2000, seed=7) -> str:
    if not trades:
        return f"{label:44} n=0"
    pnl = np.array([t["pnl"] for t in trades])
    per_m: dict[str, float] = {}
    for t in trades:
        per_m[t["market"]] = per_m.get(t["market"], 0.0) + t["pnl"]
    v = np.array(list(per_m.values()))
    tstat = v.mean() / (v.std(ddof=1) / math.sqrt(len(v))) if len(v) > 2 and v.std() > 0 else float("nan")
    days = sorted({t["day"] for t in trades})
    S = np.array([sum(t["pnl"] for t in trades if t["day"] == d) for d in days])
    N = np.array([sum(1 for t in trades if t["day"] == d) for d in days], dtype=float)
    k = np.random.default_rng(seed).integers(0, len(days), size=(boot, len(days)))
    lo, hi = np.percentile(S[k].sum(1) / np.maximum(N[k].sum(1), 1), [5, 95])
    return (f"{label:44} n={len(pnl):5d} mkts={len(v):4d} mean={pnl.mean()*100:+6.2f}c total=${pnl.sum():+8.2f} "
            f"win={np.mean(pnl > 0)*100:4.0f}% t_mkt={tstat:+5.2f} CI90(day)=[{lo*100:+.2f},{hi*100:+.2f}]c")
