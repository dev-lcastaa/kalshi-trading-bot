"""Maker (resting order) strategy replay: same signals as the fair-value taker rule, different execution.

The decision schedule is independent of execution, so taker and maker are compared on exactly the same signals:
a decision at second i joins the touch (YES: the bid; NO: the ask) with a 1-contract limit order that rests for
`window_s` seconds and is cancelled if unfilled. A filled order is held to settlement and pays no fee in the base
case (see `aqlabs.costs.fills.maker_fee`). Every decision also records what crossing the spread at the same
moment would have earned, which exposes adverse selection (the trades that never fill are the good ones).
"""
from __future__ import annotations

import numpy as np

from ..costs import RULES, first_fills, maker_fee, taker_fee_array
from .replay import ProbFn

# Columns of m["q"] (see replay.load_all): ts_ms, bid, ask, bid_size, ask_size, last trade price, cumulative volume.
_TS, _BID, _ASK, _BSZ, _ASZ, _PRICE, _VOL = range(7)


def run_maker(markets, zs, prob_fn: ProbFn, thr=0.03, window_s=60, delay=2, gap=60, max_attempts=5,
              lo=240, hi=840, maker_fee_scale=0.0, cancel_edge: float | None = None) -> list[dict]:
    """One dict per order attempt. `thr` is the edge over the limit price (no fee in the base case).

    `cancel_edge`: while the order rests, re-evaluate the signal every second and cancel (with `delay` seconds of
    cancel latency, during which the order can still fill) once the edge over the limit price drops below it.
    None leaves the order resting for the whole window."""
    orders = []
    for m, (z, zok, ml) in zip(markets, zs):
        p = prob_fn(m, z, ml)
        sl = m["close_s"] - m["s"]
        bid, ask = m["bid"], m["ask"]
        okq = m["qok"] & zok & (sl >= lo) & (sl <= hi)
        with np.errstate(invalid="ignore"):
            cand_y = okq & (p > 0.5) & (p - bid >= thr) & (m["bsz"] >= 1) & (bid > 0)
            cand_n = okq & (p < 0.5) & (ask - p >= thr) & (m["asz"] >= 1) & (ask < 1)
        raw = m["q"]
        side, last, attempts = None, -10**9, 0
        for i in np.flatnonzero(cand_y | cand_n):
            if attempts >= max_attempts:
                break
            if i - last < gap:
                continue
            sd = "yes" if cand_y[i] else "no"
            if side is not None and sd != side:
                continue
            e = i + delay
            if e >= len(bid) or not m["qok"][e]:
                continue
            side, last, attempts = sd, i, attempts + 1
            limit = float(bid[i]) if sd == "yes" else float(ask[i])
            start_ms = int(m["s"][i] + delay) * 1000
            end_ms = start_ms + window_s * 1000
            cancelled = False
            if cancel_edge is not None:
                edge = (p[e:e + window_s + 1] - limit) if sd == "yes" else (limit - p[e:e + window_s + 1])
                weak = np.flatnonzero(edge < cancel_edge)
                if len(weak):
                    cancelled = True
                    end_ms = min(end_ms, int(m["s"][e + weak[0]] + delay) * 1000)
            fills = first_fills(sd, limit, raw[:, _TS], raw[:, _PRICE], raw[:, _VOL], raw[:, _BID], raw[:, _ASK],
                                raw[:, _BSZ], raw[:, _ASZ], start_ms, end_ms)
            won = m["y"] if sd == "yes" else 1 - m["y"]
            cost = limit if sd == "yes" else 1 - limit
            maker_pnl = won - cost - float(maker_fee(cost, maker_fee_scale))
            taker_pnl = None  # crossing the spread at the same moment, same fee schedule as the taker replay
            if sd == "yes" and m["asz"][e] >= 1 and ask[e] < 1:
                px = float(ask[e])
                taker_pnl = won - px - float(taker_fee_array(px))
            elif sd == "no" and m["bsz"][e] >= 1 and bid[e] > 0:
                px = float(1 - bid[e])
                taker_pnl = won - px - float(taker_fee_array(px))
            orders.append(dict(market=m["ticker"], day=m["day"], split=m["split"], coin=m["coin"], side=sd,
                               ml=float(ml[i]), limit=limit, cost=cost, maker_pnl=maker_pnl, taker_pnl=taker_pnl,
                               cancelled=cancelled,
                               fill_ms={r: fills[r] for r in RULES}, start_ms=start_ms,
                               queue_ahead=float(raw[np.searchsorted(raw[:, _TS], start_ms, side="right") - 1,
                                                     _BSZ if sd == "yes" else _ASZ])))
    return orders


def as_trades(orders: list[dict], rule: str, per_signal: bool) -> list[dict]:
    """Trade dicts for `replay.stats`. per_signal=True counts unfilled orders as 0 (value per decision);
    False keeps only the filled orders (value per fill)."""
    out = []
    for o in orders:
        filled = o["fill_ms"][rule] is not None
        if filled or per_signal:
            out.append(dict(pnl=o["maker_pnl"] if filled else 0.0, market=o["market"], day=o["day"], split=o["split"],
                            coin=o["coin"], ml=o["ml"], side=o["side"], price=o["cost"]))
    return out


def taker_trades(orders: list[dict]) -> list[dict]:
    """The same decisions executed by crossing the spread (orders with no executable quote are dropped)."""
    return [dict(pnl=o["taker_pnl"], market=o["market"], day=o["day"], split=o["split"], coin=o["coin"], ml=o["ml"],
                 side=o["side"], price=o["cost"]) for o in orders if o["taker_pnl"] is not None]


def fill_rate(orders: list[dict], rule: str) -> float:
    return float(np.mean([o["fill_ms"][rule] is not None for o in orders])) if orders else float("nan")


def adverse_selection(orders: list[dict], rule: str) -> dict:
    """Mean taker P/L of the decisions that did and did not fill. If the unfilled ones were better, resting orders
    keep the losers and miss the winners."""
    both = [o for o in orders if o["taker_pnl"] is not None]
    filled = [o["taker_pnl"] for o in both if o["fill_ms"][rule] is not None]
    unfilled = [o["taker_pnl"] for o in both if o["fill_ms"][rule] is None]
    mean = lambda v: float(np.mean(v)) if v else float("nan")  # noqa: E731
    return {"filled_n": len(filled), "filled_taker_mean": mean(filled), "unfilled_n": len(unfilled),
            "unfilled_taker_mean": mean(unfilled)}
