"""Head-to-head candidate improvements at $3 stakes and realistic costs, with half-split checks.

Realistic = fees +0.02, 2-tick slippage, 2 s delay (stress_test.run). A composite rule tries its first
rule in each market and falls back to the second only if the first did not trade there.

    python research/improve_search.py --quotes-cache quotes.pkl --out improve.json
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stress_test as st  # noqa: E402
from quotes import load  # noqa: E402

FOLLOW = dict(model="gap", edge=-9, kind="follow")
PRIMARY = dict(model="shrink", edge=.10, kind="rel", band=(.40, .70))
CANDIDATES = {
    "A. Current primary rule": [dict(PRIMARY, start=0)],
    "B. Minute 3-4 follow": [dict(FOLLOW, start=180, end=240)],
    "C. Minute 3-4, max 85c": [dict(FOLLOW, start=180, end=240, cap=.85)],
    "D. Minute 3-4, max 90c": [dict(FOLLOW, start=180, end=240, cap=.90)],
    "E. Minute 3-4, model+market agree": [dict(FOLLOW, start=180, end=240, agree=True)],
    "E2. Minute 3-4, agree + max 90c": [dict(FOLLOW, start=180, end=240, agree=True, cap=.90)],
    "E3. Minute 3-4, agree + max 85c": [dict(FOLLOW, start=180, end=240, agree=True, cap=.85)],
    "F. 3:15-4:00 follow": [dict(FOLLOW, start=195, end=240)],
    "G. 3:00-4:30 follow": [dict(FOLLOW, start=180, end=270)],
    "H. Primary rule only at 30-60 s": [dict(PRIMARY, start=30, end=60)],
    "I. Primary at 30-60 s, else minute 3-4": [dict(PRIMARY, start=30, end=60), dict(FOLLOW, start=180, end=240)],
    "J. Primary at 30-60 s, else minute 3-4 max 85c": [dict(PRIMARY, start=30, end=60), dict(FOLLOW, start=180, end=240, cap=.85)],
    "K. 90% late favourite": [st.RULES["90% target: late favourite (240 s, 85-99c)"]],
    "L. Minute 3-4, else 90% favourite": [dict(FOLLOW, start=180, end=240), st.RULES["90% target: late favourite (240 s, 85-99c)"]],
}
COSTS = dict(st.BASE, stake=3.0, theta_add=.02, slip=2, latency=2)


def composite(by, order, outcome, rules, p):
    taken = {}
    for rule in rules:
        for t in st.run(by, order, outcome, rule, p, seed=3):
            taken.setdefault(t["k"], t)
    return [taken[k] for k in sorted(taken)]


def summary(trades, n):
    s = st.stats(trades, n)
    half = n // 2
    s["first_half_pnl"] = round(sum(t["pnl"] for t in trades if t["k"] < half), 2)
    s["second_half_pnl"] = round(sum(t["pnl"] for t in trades if t["k"] >= half), 2)
    return s


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
    out = {name: summary(composite(by, order, data["outcome"], rules, COSTS), len(order))
           for name, rules in CANDIDATES.items()}
    text = json.dumps(out, indent=2)
    print(text)
    if a.out:
        Path(a.out).write_text(text)


if __name__ == "__main__":
    main()
