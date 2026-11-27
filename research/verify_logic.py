"""Verify the decision path of the candidate rule: no look-ahead, correct sequencing and costs.

Candidate: between 180 and 240 s, Reference Gap side, market agrees, gap+speed agrees, all-in price
<= 90c; reversal exit; $3; entry and exit orders both arrive 2 s late with 2-tick slippage and fee +0.02.

Checks
  1. Outcome blindness: scrambling which rounds went UP must not change any decision
     (entry second, side, quantity, exit second). Only P&L may change.
  2. No future peeking: replacing every quote AFTER the entry decision with noise must not change
     the entry decision.
  3. Sequencing: decision < order arrival (>= +2 s) < exit signal < sale arrival (>= +2 s) < close,
     entry inside 180-240 s, price <= 90c, agreement conditions true at the decision quote.
  4. Accounting: P&L = settlement payout (or sale proceeds) - cost, payout = qty or 0.
  5. Results before/after charging the exit delay, plus traced example rounds.

    python research/verify_logic.py --quotes-cache quotes.pkl --out verify.json
"""
import argparse
import copy
import json
import random
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import option_batch as ob  # noqa: E402
from new_models import features  # noqa: E402
from quotes import load  # noqa: E402

GAP = lambda r: r["gap_p"]  # noqa: E731
AGREE3 = lambda r, s, c: (r["speed_p"] >= .5) == (s == "UP")  # noqa: E731
RULES = {
    "baseline (min 3-4, agree, <=90c)": dict(signal=GAP),
    "+ reversal exit": dict(signal=GAP, exit="reversal"),
    "+ all-3-agree + reversal exit": dict(signal=GAP, exit="reversal", entry=AGREE3),
}


def run(rounds, opt):
    trace, per = [], []
    for rows in rounds:
        pnl, _ = ob.play_round(rows, opt, opt["signal"], None, trace)
        per.append(pnl or 0.0)
    return trace, np.array(per)


def decisions(trace):
    return [(t["slug"], t["decision_s"], t["side"], t["qty"], (t["exit"] or {}).get("sold_t")) for t in trace]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quotes-cache", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    data = load(None, a.quotes_cache)
    by = {}
    for q in data["quotes"]:
        by.setdefault(q["slug"], []).append(q)
    order = sorted(by, key=lambda s: by[s][0]["t"])
    rounds = [ob.enrich(features(by[s])) for s in order]
    cand = RULES["+ all-3-agree + reversal exit"]
    trace, per = run(rounds, cand)
    out = dict(checks={}, results={}, examples=[])

    # 1. Outcome blindness
    rng = random.Random(9)
    ups = [rows[0]["up"] for rows in rounds]
    same = True
    for _ in range(20):
        shuffled = ups[:]; rng.shuffle(shuffled)
        fake = copy.deepcopy(rounds)
        for rows, u in zip(fake, shuffled):
            for r in rows:
                r["up"] = u
        same &= decisions(run(fake, cand)[0]) == decisions(trace)
    out["checks"]["1_outcome_blind_20_shuffles"] = same

    # 2. No future peeking: noise after each entry decision leaves the entry unchanged
    ok = True
    for t in trace:
        k = order.index(t["slug"])
        rows = copy.deepcopy(by[t["slug"]])
        for q in rows:
            if q["t"] > t["decision_t"] + 1e-9:          # strictly after the decision quote
                q["ref"] *= 1 + rng.uniform(-.002, .002)
                q["mid"] = min(.99, max(.01, q["mid"] + rng.uniform(-.3, .3)))
                q["gap_p"] = rng.random()
        tr2 = []
        ob.play_round(ob.enrich(features(rows)), dict(cand, exit=None), GAP, None, tr2)
        ok &= bool(tr2) and (tr2[0]["decision_s"], tr2[0]["side"]) == (t["decision_s"], t["side"])
    out["checks"]["2_entry_unchanged_by_future_noise"] = ok

    # 3. Sequencing and rule conditions; 4. accounting
    seq, acct = True, True
    for t in trace:
        rows = {round(r["elapsed"], 1): r for r in rounds[order.index(t["slug"])]}
        r = rows[t["decision_s"]]
        seq &= 180 <= t["decision_s"] < 240 and t["arrival_s"] >= t["decision_s"] + 2 - 1e-6
        seq &= t["all_in_price"] <= .90 + 1e-9 and (r["mid"] >= .5) == (t["side"] == "UP")
        seq &= (r["speed_p"] >= .5) == (t["side"] == "UP") and (r["gap_p"] >= .5) == (t["side"] == "UP")
        if t["exit"]:
            e = t["exit"]
            seq &= t["arrival_s"] < e["signal_t"] and e["sold_t"] >= e["signal_t"] + 2 - 1e-6 and e["sold_t"] < 300
            acct &= abs(t["pnl"] - (e["proceeds"] - t["cost"])) < 1e-3
        else:
            payout = t["qty"] if t["side"] == t["outcome"] else 0
            acct &= abs(t["pnl"] - (payout - t["cost"])) < 1e-3
    out["checks"]["3_sequencing_and_conditions"] = seq
    out["checks"]["4_accounting"] = acct

    # 5. Results with the exit delay now charged
    n = len(rounds)
    for name, opt in RULES.items():
        tr, p = run(rounds, opt)
        s = ob.stats(p, n)
        s["early_exits"] = sum(1 for t in tr if t["exit"])
        out["results"][name] = s
    # 6. Exit stress: faster bots take the stale bid, or the book reprices before the sale arrives
    stress = {}
    for label, knobs in (("exit bid taken 50% of the time", {"taken": .5}), ("exit bid taken 100%", {"taken": 1.0}),
                         ("book reprices to the model's value first", {"reprice": True})):
        ob.EXIT_STRESS.clear(); ob.EXIT_STRESS.update(knobs); ob.EXIT_RNG.seed(11)
        for name in ("+ reversal exit", "+ all-3-agree + reversal exit"):
            s = ob.stats(run(rounds, RULES[name])[1], n)
            stress[f"{name} | {label}"] = dict(trades=s["trades"], win_rate=s["win_rate"], pnl=s["pnl"],
                                               max_drawdown=s["max_drawdown"])
    ob.EXIT_STRESS.clear()
    out["exit_stress"] = stress
    exits = [t for t in trace if t["exit"]]
    held_win = [t for t in trace if not t["exit"] and t["pnl"] > 0]
    held_loss = [t for t in trace if not t["exit"] and t["pnl"] <= 0]
    out["examples"] = [x for x in (held_win[:1] + exits[:2] + held_loss[:1])]
    text = json.dumps(out, indent=2)
    print(text)
    if a.out:
        Path(a.out).write_text(text)


if __name__ == "__main__":
    main()
