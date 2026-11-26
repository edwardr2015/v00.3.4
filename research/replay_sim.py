"""Execution-aware replay of selective-entry strategies on recorded BTC Prophet quotes.

Every simulated entry uses a quote the app actually captured (fresh, not rejected), the app's
own fill model (prophet.execution.execute: 25% of displayed top-level depth, one adverse tick,
banker's-rounded fees, >=95% of the $5 budget must fill) and holds to the official outcome.
One entry per market per strategy: the first qualifying quote at or after the start second.

Experiments
  A  in-sample      choose the best strategy using all outcomes (the "we know the answers" run)
  B  walk-forward   choose on the earlier half of markets, freeze, test on the later half
  C  null           shuffle outcomes across markets and repeat A: what luck alone produces
  D  determinism    run the whole pipeline repeatedly and compare result hashes
  E  bootstrap      resample markets of the frozen strategy to show its uncertainty
  F  sample size    trades needed before a 90% win rate or a profit edge can be claimed

    python research/replay_sim.py PAPER_REPORT.xlsx --cache quotes.pkl --out results.json
"""

import argparse
import hashlib
import itertools
import json
import math
import random
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "btc-prophet"))
from prophet.execution import execute  # noqa: E402
from quotes import load  # noqa: E402

STAKE = 5.0
SIGNALS = ("gap", "market", "agree")
STARTS = (0, 30, 60, 90, 120, 150, 180, 210, 240)
CONFS = (.50, .55, .60, .65, .70, .75, .80, .85, .90, .95)
PMIN = (0, .30, .40, .50, .60, .70, .80, .85, .90)
PMAX = (.50, .60, .70, .80, .85, .90, .95, .99)
EDGES = (-1, 0, .03, .05, .10)


def wilson(k, n, z=1.96):
    if not n:
        return (None, None)
    p = k / n
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (c - h, c + h)


def fill(q, side):
    book = dict(bids=[dict(price=q["bid"], quantity=q["bid_q"])],
                asks=[dict(price=q["ask"], quantity=q["ask_q"])])
    f = execute(book, side, budget=STAKE, **q["terms"])
    ok = f["total"] >= .95 * STAKE and f["quantity"] > 0
    return (f["quantity"], f["total"]) if ok else (0.0, 0.0)


def arrays(quotes):
    slugs = sorted({q["slug"] for q in quotes}, key=lambda s: min(x["t"] for x in quotes if x["slug"] == s))
    idx = {s: i for i, s in enumerate(slugs)}
    n = len(quotes)
    A = dict(market=np.array([idx[q["slug"]] for q in quotes]),
             elapsed=np.array([q["elapsed"] for q in quotes]),
             gap=np.array([q["gap_p"] for q in quotes]), mid=np.array([q["mid"] for q in quotes]),
             up=np.array([q["up"] for q in quotes]))
    for side in ("UP", "DOWN"):
        qty, cost = np.zeros(n), np.zeros(n)
        for i, q in enumerate(quotes):
            qty[i], cost[i] = fill(q, side)
        A[side + "_qty"], A[side + "_cost"] = qty, cost
    market_up = np.zeros(len(slugs), bool)
    for q in quotes:
        market_up[idx[q["slug"]]] = q["up"]
    return A, slugs, market_up


def configs():
    for sig, st, c, lo, hi, e in itertools.product(SIGNALS, STARTS, CONFS, PMIN, PMAX, EDGES):
        if lo < hi:
            yield (sig, st, c, lo, hi, e)


def precompute(A):
    side_up = {"gap": A["gap"] >= .5, "market": A["mid"] >= .5}
    side_up["agree"] = side_up["gap"]
    conf = {"gap": np.maximum(A["gap"], 1 - A["gap"]), "market": np.maximum(A["mid"], 1 - A["mid"])}
    conf["agree"] = conf["gap"]
    agree = side_up["gap"] == side_up["market"]
    P = {}
    for sig in SIGNALS:
        up = side_up[sig]
        qty = np.where(up, A["UP_qty"], A["DOWN_qty"])
        cost = np.where(up, A["UP_cost"], A["DOWN_cost"])
        price = np.where(cost > 0, cost / np.maximum(qty, 1e-9), np.inf)   # all-in per contract
        ev = np.where(cost > 0, (conf[sig] * qty - cost) / np.maximum(cost, 1e-9), -np.inf)
        valid = cost > 0
        if sig == "agree":
            valid &= agree
        P[sig] = dict(up=up, qty=qty, cost=cost, price=price, ev=ev, conf=conf[sig], valid=valid)
    return P


def run(A, P, n_markets, outcome_up, cfg, market_mask=None):
    sig, st, c, lo, hi, e = cfg
    p = P[sig]
    m = p["valid"] & (A["elapsed"] >= st) & (p["conf"] >= c) & (p["price"] >= lo) & (p["price"] <= hi) & (p["ev"] >= e)
    if market_mask is not None:
        m &= market_mask[A["market"]]
    hits = np.flatnonzero(m)
    if not len(hits):
        return None
    mk = A["market"][hits]
    _, first = np.unique(mk, return_index=True)       # quotes are time-ordered: first per market
    sel = hits[first]
    won = p["up"][sel] == outcome_up[A["market"][sel]]
    pnl = p["qty"][sel] * won - p["cost"][sel]
    return dict(trades=len(sel), wins=int(won.sum()), pnl=float(pnl.sum()),
                staked=float(p["cost"][sel].sum()), pnl_list=pnl, markets=A["market"][sel])


def summary(cfg, r, n_markets):
    if not r:
        return dict(config=label(cfg), trades=0)
    lo, hi = wilson(r["wins"], r["trades"])
    cum = np.cumsum(r["pnl_list"])
    dd = float(np.max(np.maximum.accumulate(np.concatenate([[0], cum]))[1:] - cum)) if len(cum) else 0
    return dict(config=label(cfg), trades=r["trades"], skips=n_markets - r["trades"], wins=r["wins"],
                win_rate=round(r["wins"] / r["trades"], 4), win_ci95=[round(lo, 3), round(hi, 3)],
                net_pnl=round(r["pnl"], 2), per_dollar=round(r["pnl"] / r["staked"], 4),
                avg_pnl=round(r["pnl"] / r["trades"], 3), max_drawdown=round(dd, 2))


def label(cfg):
    sig, st, c, lo, hi, e = cfg
    return dict(signal=sig, start_s=st, min_conf=c, price=[lo, hi], min_edge=None if e < 0 else e)


def best(A, P, n, outcome_up, cfgs, *, key, min_trades, market_mask=None):
    top, top_r = None, None
    for cfg in cfgs:
        r = run(A, P, n, outcome_up, cfg, market_mask)
        if not r or r["trades"] < min_trades:
            continue
        k = key(r)
        if top is None or k > top:
            top, top_r, top_cfg = k, r, cfg
    return (top_cfg, top_r) if top_r else (None, None)


BY_PNL = lambda r: (r["pnl"], r["wins"] / r["trades"])                          # noqa: E731
BY_WIN = lambda r: (r["wins"] / r["trades"], r["pnl"])                          # noqa: E731


def trades_needed(p, target, z=1.96):
    """Smallest n with Wilson lower bound > target when the observed win rate is p."""
    for n in range(10, 20001):
        if wilson(round(p * n), n, z)[0] > target:
            return n
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workbook")
    ap.add_argument("--cache")
    ap.add_argument("--out")
    ap.add_argument("--null-runs", type=int, default=100)
    ap.add_argument("--repeats", type=int, default=3)
    a = ap.parse_args()

    data = load(a.workbook, a.cache)
    A, slugs, outcome_up = arrays(data["quotes"])
    P = precompute(A)
    n = len(slugs)
    cfgs = list(configs())
    half = np.zeros(n, bool); half[: n // 2] = True
    out = dict(markets=n, first_market=slugs[0], last_market=slugs[-1], up_rate=float(outcome_up.mean()),
               strategies_tested=len(cfgs), stake=STAKE)

    # A. In-sample: tuned with knowledge of every outcome.
    cfg, r = best(A, P, n, outcome_up, cfgs, key=BY_PNL, min_trades=20)
    out["A_in_sample_best_pnl"] = summary(cfg, r, n)
    cfg, r = best(A, P, n, outcome_up, cfgs, key=BY_WIN, min_trades=20)
    out["A_in_sample_best_win_rate"] = summary(cfg, r, n)
    ninety = []
    for c in cfgs:
        r = run(A, P, n, outcome_up, c)
        if r and r["trades"] >= 20 and r["wins"] / r["trades"] >= .9:
            ninety.append(summary(c, r, n))
    ninety.sort(key=lambda s: -s["net_pnl"])
    out["A_90pct_strategies"] = dict(count=len(ninety), best_by_pnl=ninety[:5],
                                     median_per_dollar=float(np.median([s["per_dollar"] for s in ninety])) if ninety else None)

    # B. Walk-forward: tune on the earlier half, freeze, evaluate on the untouched later half.
    for name, key in (("pnl", BY_PNL), ("win_rate", BY_WIN)):
        cfg, r = best(A, P, n, outcome_up, cfgs, key=key, min_trades=10, market_mask=half)
        test = run(A, P, n, outcome_up, cfg, ~half) if cfg else None
        out[f"B_walk_forward_by_{name}"] = dict(train=summary(cfg, r, int(half.sum())),
                                               test=summary(cfg, test, int((~half).sum())))
    frozen = dict(baseline_gap_40_70_edge10=("gap", 0, .5, .40, .70, .10),
                  market_favourite_late_90=("market", 240, .90, .85, .99, -1),
                  gap_conf75_40_70=("gap", 0, .75, .40, .70, -1))
    out["B_predeclared"] = {k: dict(first_half=summary(c, run(A, P, n, outcome_up, c, half), int(half.sum())),
                                    second_half=summary(c, run(A, P, n, outcome_up, c, ~half), int((~half).sum())))
                            for k, c in frozen.items()}

    # C. Null: outcomes shuffled across markets; what does optimisation find in pure noise?
    rng = random.Random(20261126)
    sub = cfgs[:: max(1, len(cfgs) // 3000)]
    null_pnl, null_win = [], []
    for _ in range(a.null_runs):
        fake = np.array(outcome_up); rng.shuffle(fake)
        _, r = best(A, P, n, fake, sub, key=BY_PNL, min_trades=20)
        null_pnl.append(r["pnl"] if r else 0)
        _, r = best(A, P, n, fake, sub, key=BY_WIN, min_trades=20)
        null_win.append(r["wins"] / r["trades"] if r else 0)
    _, real = best(A, P, n, outcome_up, sub, key=BY_PNL, min_trades=20)
    out["C_null"] = dict(runs=a.null_runs, strategies_per_run=len(sub),
                         real_best_pnl=round(real["pnl"], 2),
                         null_best_pnl_median=round(float(np.median(null_pnl)), 2),
                         null_best_pnl_95pct=round(float(np.percentile(null_pnl, 95)), 2),
                         share_of_null_runs_beating_real=round(float(np.mean(np.array(null_pnl) >= real["pnl"])), 3),
                         null_best_win_rate_median=round(float(np.median(null_win)), 3),
                         null_best_win_rate_95pct=round(float(np.percentile(null_win, 95)), 3))

    # D. Determinism: identical inputs must give identical decisions every time.
    hashes = []
    for _ in range(a.repeats):
        A2, _, o2 = arrays(load(a.workbook, a.cache)["quotes"])
        P2 = precompute(A2)
        h = hashlib.sha256()
        for c in cfgs[::50]:
            r = run(A2, P2, n, o2, c)
            h.update(json.dumps(None if not r else [r["trades"], r["wins"], round(r["pnl"], 6),
                                                    r["markets"].tolist()]).encode())
        hashes.append(h.hexdigest())
    out["D_determinism"] = dict(repeats=a.repeats, identical=len(set(hashes)) == 1, sha256=hashes[0])

    # E. Bootstrap the walk-forward frozen strategy on its test half.
    cfg, _ = best(A, P, n, outcome_up, cfgs, key=BY_PNL, min_trades=10, market_mask=half)
    test = run(A, P, n, outcome_up, cfg, ~half)
    if test:
        pl = test["pnl_list"]
        boots = [float(np.sum(rng.choices(list(pl), k=len(pl)))) for _ in range(2000)]
        out["E_bootstrap_test_pnl"] = dict(trades=len(pl), mean=round(float(np.mean(boots)), 2),
                                           p05=round(float(np.percentile(boots, 5)), 2),
                                           p95=round(float(np.percentile(boots, 95)), 2),
                                           prob_loss=round(float(np.mean(np.array(boots) < 0)), 3))

    # F. Sample sizes.
    out["F_trades_needed"] = {
        "observed_92pct_prove_above_90pct": trades_needed(.92, .90),
        "observed_95pct_prove_above_90pct": trades_needed(.95, .90),
        "observed_93pct_prove_above_breakeven_88pct": trades_needed(.93, .88),
        "observed_70pct_prove_above_breakeven_60pct": trades_needed(.70, .60),
        "observed_75pct_prove_above_breakeven_65pct": trades_needed(.75, .65),
    }
    text = json.dumps(out, indent=2, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o))
    print(text)
    if a.out:
        Path(a.out).write_text(text)


if __name__ == "__main__":
    main()
