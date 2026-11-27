"""Bankroll simulation: start with $50, $3 per trade, for each candidate rule.

1. Replay each rule at a $3 stake on the recorded markets (stress_test.run: app fill model, hold to
   settlement), at standard costs and at realistic costs (fees +0.02, 2-tick slippage, 2 s delay).
2. Monte Carlo: a day is 288 markets drawn with replacement from the recorded ones; each market
   contributes that rule's P&L there (0 if it skipped). Trading stops when cash < $3.
Reports the distribution of the balance after 1 day and 7 days, chance of loss, of halving, of
going broke, and the typical worst drawdown. This resamples ONE recorded session; it cannot
represent market conditions that session did not contain.

    python research/bankroll_sim.py --quotes-cache quotes.pkl --out bankroll.json
"""
import argparse
import json
import random
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stress_test as st  # noqa: E402
from quotes import load  # noqa: E402

START, STAKE, MARKETS_PER_DAY = 50.0, 3.0, 288
RULES = {
    "Current primary rule (40-70c, EV>=10%)": st.RULES["Primary-like (40-70c, EV>=10%, hold)"],
    "Model + market agree (40-70c, EV>=10%)": dict(model="gap", start=0, edge=.10, kind="rel", band=(.40, .70), agree=True),
    "Minute 3-4 follow (any price)": st.RULES["Minute 3-4 follow (gap side, 180-240 s, any price)"],
    "Minute 3-4 follow (price <= 85c)": st.RULES["Minute 3-4 follow, price <= 85c"],
    "90% late favourite (240 s+, 85-99c)": st.RULES["90% target: late favourite (240 s, 85-99c)"],
}
COSTS = {"standard": {}, "realistic": dict(theta_add=.02, slip=2, latency=2)}


def per_market(by, order, outcome, rule, costs):
    p = dict(st.BASE, stake=STAKE, **costs)
    trades = st.run(by, order, outcome, rule, p, seed=3)
    pnl = np.zeros(len(order))
    for t in trades:
        pnl[t["k"]] = t["pnl"]
    return pnl, len(trades)


def simulate(pnl, days, runs, rng):
    n = len(pnl)
    finals, broke, halved, worst = [], 0, 0, []
    for _ in range(runs):
        cash, peak, dd = START, START, 0.0
        for _ in range(days * MARKETS_PER_DAY):
            if cash < STAKE:
                break
            cash += pnl[rng.randrange(n)]
            peak = max(peak, cash); dd = max(dd, peak - cash)
        finals.append(cash); worst.append(dd)
        broke += cash < STAKE
        halved += cash <= START / 2
    f = np.array(finals)
    return dict(median=round(float(np.median(f)), 2), p10=round(float(np.percentile(f, 10)), 2),
                p90=round(float(np.percentile(f, 90)), 2), chance_of_loss=round(float(np.mean(f < START)), 3),
                chance_halved=round(halved / runs, 3), chance_broke=round(broke / runs, 3),
                median_worst_drawdown=round(float(np.median(worst)), 2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quotes-cache", required=True)
    ap.add_argument("--runs", type=int, default=10000)
    ap.add_argument("--out")
    a = ap.parse_args()
    data = load(None, a.quotes_cache)
    by = {}
    for q in data["quotes"]:
        by.setdefault(q["slug"], []).append(q)
    order = sorted(by, key=lambda s: by[s][0]["t"])
    rng = random.Random(50)
    out = dict(start=START, stake=STAKE, markets_per_day=MARKETS_PER_DAY, recorded_markets=len(order), rules={})
    for name, rule in RULES.items():
        out["rules"][name] = {}
        for cname, costs in COSTS.items():
            pnl, trades = per_market(by, order, data["outcome"], rule, costs)
            wins = int((pnl > 0).sum())
            out["rules"][name][cname] = dict(
                replay=dict(trades=trades, wins=wins, win_rate=round(wins / trades, 3) if trades else None,
                            pnl_94_markets=round(float(pnl.sum()), 2),
                            per_trade=round(float(pnl.sum() / trades), 3) if trades else None,
                            trades_per_day=round(trades / len(order) * MARKETS_PER_DAY)),
                one_day=simulate(pnl, 1, a.runs, rng), one_week=simulate(pnl, 7, max(1000, a.runs // 5), rng))
    text = json.dumps(out, indent=2)
    print(text)
    if a.out:
        Path(a.out).write_text(text)


if __name__ == "__main__":
    main()
