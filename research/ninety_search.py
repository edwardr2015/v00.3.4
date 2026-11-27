"""What does it take to win 85/90/95% of trades? Search entry rules on recorded quotes.

Same execution model as replay_sim.py ($5, app fill model, hold to settlement, one entry per market,
first qualifying quote). Adds an entry-window END so rules like "only 30-60 s" are possible.
For each win-rate target the best rule is chosen on the first half of markets (by profit, at least
10 trades) and then run unchanged on the second half.

    python research/ninety_search.py --cache quotes.pkl --out ninety.json
"""
import argparse
import itertools
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quotes import load  # noqa: E402
from replay_sim import arrays, precompute, wilson  # noqa: E402

SIGNALS = ("gap", "market", "agree")
WINDOWS = [(a, b) for a in (0, 30, 60, 90, 120, 150, 180, 210, 240, 270) for b in (60, 90, 120, 180, 240, 300) if b > a]
CONFS = (.5, .6, .7, .8, .9, .95)
BANDS = [(lo, hi) for lo in (0, .4, .5, .6, .7, .8, .85, .9) for hi in (.6, .7, .8, .9, .95, .99) if hi > lo]
EDGES = (-1, 0, .05)


def run(A, P, outcome_up, cfg, mask=None):
    sig, (a, b), c, (lo, hi), e = cfg
    p = P[sig]
    m = p["valid"] & (A["elapsed"] >= a) & (A["elapsed"] < b) & (p["conf"] >= c) & (p["price"] >= lo) & (p["price"] <= hi) & (p["ev"] >= e)
    if mask is not None:
        m &= mask[A["market"]]
    hits = np.flatnonzero(m)
    if not len(hits):
        return None
    _, first = np.unique(A["market"][hits], return_index=True)
    sel = hits[first]
    won = p["up"][sel] == outcome_up[A["market"][sel]]
    pnl = p["qty"][sel] * won - p["cost"][sel]
    return dict(trades=len(sel), wins=int(won.sum()), pnl=float(pnl.sum()), staked=float(p["cost"][sel].sum()),
                avg_price=float(np.mean(p["price"][sel])))


def describe(cfg, r, markets):
    if not r:
        return dict(rule=label(cfg), trades=0)
    lo, hi = wilson(r["wins"], r["trades"])
    return dict(rule=label(cfg), trades=r["trades"], share_of_rounds=round(r["trades"] / markets, 2),
                wins=r["wins"], win_rate=round(r["wins"] / r["trades"], 3), win_ci95=[round(lo, 2), round(hi, 2)],
                pnl=round(r["pnl"], 2), per_trade=round(r["pnl"] / r["trades"], 3), avg_price=round(r["avg_price"], 3))


def label(cfg):
    sig, (a, b), c, (lo, hi), e = cfg
    return dict(signal=sig, window_s=[a, b], min_conf=c, price=[lo, hi], min_edge=None if e < 0 else e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    A, slugs, outcome_up = arrays(load(None, a.cache)["quotes"])
    P = precompute(A)
    n = len(slugs)
    half = np.zeros(n, bool); half[: n // 2] = True
    cfgs = list(itertools.product(SIGNALS, WINDOWS, CONFS, BANDS, EDGES))
    first, second, full = {}, {}, {}
    for cfg in cfgs:
        first[cfg] = run(A, P, outcome_up, cfg, half)
    out = dict(markets=n, rules_tested=len(cfgs), targets={})
    for target in (.80, .85, .90, .95):
        ok = [(c, r) for c, r in first.items() if r and r["trades"] >= 10 and r["wins"] / r["trades"] >= target]
        best = max(ok, key=lambda cr: cr[1]["pnl"], default=None)
        entry = dict(rules_meeting_target_on_first_half=len(ok))
        if best:
            cfg = best[0]
            entry["chosen_on_first_half"] = describe(cfg, best[1], n // 2)
            entry["untouched_second_half"] = describe(cfg, run(A, P, outcome_up, cfg, ~half), n - n // 2)
            entry["all_94"] = describe(cfg, run(A, P, outcome_up, cfg), n)
        # How common is profit at this win rate (whole sample)?
        allr = [run(A, P, outcome_up, c) for c, _ in ok[:4000]]
        allr = [r for r in allr if r and r["trades"] >= 20 and r["wins"] / r["trades"] >= target]
        if allr:
            per = [r["pnl"] / r["trades"] for r in allr]
            entry["typical_rule_whole_sample"] = dict(rules=len(allr), median_per_trade=round(float(np.median(per)), 3),
                                                      median_avg_price=round(float(np.median([r["avg_price"] for r in allr])), 3),
                                                      median_share_of_rounds=round(float(np.median([r["trades"] / n for r in allr])), 2))
        out["targets"][f"{int(target*100)}%"] = entry
    text = json.dumps(out, indent=2)
    print(text)
    if a.out:
        Path(a.out).write_text(text)


if __name__ == "__main__":
    main()
