"""Test research-backed forecasting features on recorded quotes (walk-forward, execution-aware).

Candidates (all computable from timestamp-valid data the app already captures):
  gap        current Reference Gap formula: Phi(log(ref/open) / (vol * sqrt(seconds_left/15)))
  settle     settlement-average model. The close is the mean of 60 one-second BRTI values before
             the boundary (Polymarket US FAQ). Brownian approximation of an arithmetic average:
               s > 60 s left: var = s1^2 * ((s - 60) + 60/3)
               s <= 60 s left: the elapsed part of the window is fixed (estimated from captured
               reference prints inside the window); var = s1^2 * (s/60)^2 * s/3
             with s1^2 = vol^2 / 15 (vol is the app's 15-second volatility).
  micro      Stoikov-style microprice of the Polymarket YES book (size-weighted top of book)
  mid        book midpoint (the market's own probability)
  recal      walk-forward logistic recalibration of the midpoint (favourite-longshot bias):
             logit p' = a + b * logit(mid), fitted only on earlier markets
Trading test: buy the side whose probability exceeds its all-in executable price by >= D,
first qualifying quote per market, $5, app fill model, hold to settlement.

    python research/feature_research.py --quotes-cache quotes.pkl --out features.json
"""

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "btc-prophet"))
from quotes import load  # noqa: E402
from replay_sim import fill, wilson  # noqa: E402

WINDOW = 60
EDGES = (.03, .05, .08, .10, .15, .20)
STARTS = (0, 90, 180)
MODELS = ("gap", "settle", "micro", "mid", "recal", "settle_mid_blend")


def phi(z):
    return .5 * (1 + math.erf(z / math.sqrt(2)))


def clip(p):
    return min(.99, max(.01, p))


def settle_probability(q, window_refs):
    s = max(q["left"], 1e-3)
    s1 = max(q["vol"], 1e-6) / math.sqrt(15)
    if s > WINDOW:
        mean, var = q["ref"], s1 ** 2 * ((s - WINDOW) + WINDOW / 3)
    else:
        fixed = np.mean(window_refs) if window_refs else q["ref"]
        mean = ((WINDOW - s) * fixed + s * q["ref"]) / WINDOW
        var = s1 ** 2 * (s / WINDOW) ** 2 * s / 3
    sd = math.sqrt(max(var, 1e-14))
    return clip(phi(math.log(mean / q["opening"]) / sd))


def logit(p):
    p = clip(p)
    return math.log(p / (1 - p))


def fit_recal(rows):
    """Two-parameter logistic fit by Newton steps; one sample per market (latest before 150 s)."""
    if len(rows) < 10 or len({r[1] for r in rows}) < 2:
        return (0.0, 1.0)
    x = np.array([[1, logit(m)] for m, _ in rows]); y = np.array([u for _, u in rows], float)
    w = np.array([0.0, 1.0])
    for _ in range(25):
        p = 1 / (1 + np.exp(-x @ w))
        g = x.T @ (p - y) + .1 * (w - [0, 1])
        h = (x.T * (p * (1 - p))) @ x + .1 * np.eye(2)
        w = w - np.linalg.solve(h, g)
    return tuple(w)


def enrich(quotes):
    by = {}
    for q in quotes:
        by.setdefault(q["slug"], []).append(q)
    order = sorted(by, key=lambda s: by[s][0]["t"])
    history, out = [], []
    for k, slug in enumerate(order):
        a, b = fit_recal(history)                       # earlier markets only
        rows = by[slug]
        for i, q in enumerate(rows):
            end = q["t"] + q["left"]
            refs = [r["ref"] for r in rows[: i + 1] if r["t"] >= end - WINDOW]
            bq, aq = q["bid_q"], q["ask_q"]
            micro = (q["bid"] * aq + q["ask"] * bq) / (aq + bq) if aq + bq else q["mid"]
            settle = settle_probability(q, refs)
            out.append(dict(q, market_index=k, settle=settle, micro=clip(micro),
                            recal=clip(1 / (1 + math.exp(-(a + b * logit(q["mid"]))))),
                            settle_mid_blend=clip(.5 * settle + .5 * q["mid"]), gap=q["gap_p"]))
        sample = [q for q in rows if q["elapsed"] <= 150]
        if sample:
            history.append((sample[-1]["mid"], sample[-1]["up"]))
    return out, len(order)


def brier_table(rows):
    buckets = [(0, 60), (60, 120), (120, 180), (180, 240), (240, 300)]
    out = {}
    for lo, hi in buckets:
        sel = {}
        for r in rows:                                   # one quote per market per bucket
            if lo <= r["elapsed"] < hi and (r["slug"] not in sel):
                sel[r["slug"]] = r
        sel = list(sel.values())
        out[f"{lo}-{hi}s"] = dict(markets=len(sel), **{m: round(float(np.mean([(r[m] - r["up"]) ** 2 for r in sel])), 4)
                                                     for m in MODELS})
    return out


def trade(rows, model, edge, start, markets):
    chosen = {}
    for r in rows:
        if r["slug"] in chosen or r["elapsed"] < start:
            continue
        p = r[model]
        for side, ps in (("UP", p), ("DOWN", 1 - p)):
            qty, cost = fill(r, side)
            if cost and ps - cost / qty >= edge:
                won = (side == "UP") == r["up"]
                chosen[r["slug"]] = dict(pnl=qty * won - cost, won=won, cost=cost, k=r["market_index"])
                break
    t = list(chosen.values())
    if not t:
        return dict(trades=0)
    w = sum(x["won"] for x in t)
    lo, hi = wilson(w, len(t))
    return dict(trades=len(t), wins=w, win_rate=round(w / len(t), 3), win_ci95=[round(lo, 3), round(hi, 3)],
                pnl=round(sum(x["pnl"] for x in t), 2), per_dollar=round(sum(x["pnl"] for x in t) / sum(x["cost"] for x in t), 4),
                first_half_pnl=round(sum(x["pnl"] for x in t if x["k"] < markets // 2), 2),
                second_half_pnl=round(sum(x["pnl"] for x in t if x["k"] >= markets // 2), 2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quotes-cache", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    rows, n = enrich(load(None, a.quotes_cache)["quotes"])
    out = dict(markets=n, quotes=len(rows), brier=brier_table(rows), trading={})
    for m in MODELS:
        out["trading"][m] = {f"edge{e}_from{s}s": trade(rows, m, e, s, n) for e in EDGES for s in STARTS}
    # Walk-forward: choose edge/start on the first half (by P&L), report the frozen choice on the second.
    first = [r for r in rows if r["market_index"] < n // 2]
    second = [r for r in rows if r["market_index"] >= n // 2]
    wf = {}
    for m in MODELS:
        best = max(((e, s) for e in EDGES for s in STARTS),
                   key=lambda es: trade(first, m, es[0], es[1], n).get("pnl", -1e9))
        wf[m] = dict(chosen_edge=best[0], chosen_start=best[1], train=trade(first, m, *best, n),
                     test=trade(second, m, *best, n))
    out["walk_forward"] = wf
    text = json.dumps(out, indent=2)
    print(text)
    if a.out:
        Path(a.out).write_text(text)


if __name__ == "__main__":
    main()
