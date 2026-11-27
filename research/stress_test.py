"""Stress-test candidate rules on recorded quotes: costs, latency, missing data, noise, size, luck.

Every fill goes through prophet.execution.execute on the captured top-of-book quote. Probabilities
are recomputed from (perturbed) inputs with the app's own formulas, so perturbations propagate.

    python research/stress_test.py --quotes-cache quotes.pkl --out stress.json
"""

import argparse
import json
import math
import random
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "btc-prophet"))
from prophet import settlement_model  # noqa: E402
from prophet.execution import execute  # noqa: E402
from quotes import load  # noqa: E402
from replay_sim import wilson  # noqa: E402

RULES = {
    "Minute 3-4 follow (gap side, 180-240 s, any price)": dict(model="gap", start=180, end=240, edge=-9, kind="follow"),
    "Minute 3-4 follow, price <= 85c": dict(model="gap", start=180, end=240, edge=-9, kind="follow", cap=.85),
    "Late Gap (180 s, +10c)": dict(model="gap", start=180, edge=.10, kind="abs"),
    "Late Settle (180 s, +10c)": dict(model="settle", start=180, edge=.10, kind="abs"),
    "Primary-like (40-70c, EV>=10%, hold)": dict(model="shrink", start=0, edge=.10, kind="rel", band=(.40, .70)),
    "90% target: late favourite (240 s, 85-99c)": dict(model="mid", start=240, edge=-9, kind="fav", band=(.85, .99)),
}
BASE = dict(theta_add=0.0, slip=1, participation=.25, stake=5.0, latency=0.0, drop=0.0,
            ref_noise_bps=0.0, vol_mult=1.0, start_shift=0, edge_shift=0.0)


def phi(z):
    return .5 * (1 + math.erf(z / math.sqrt(2)))


def probs(q, p, rng, window):
    ref = q["ref"] * math.exp(rng.gauss(0, p["ref_noise_bps"] / 1e4)) if p["ref_noise_bps"] else q["ref"]
    vol = q["vol"] * p["vol_mult"]
    sigma = max(1e-5, vol * math.sqrt(max(1, q["left"] / 15)))
    gap = min(.98, max(.02, phi(math.log(ref / q["opening"]) / sigma)))
    settle = settlement_model.probability(ref, q["opening"], vol, q["left"], window)
    return dict(gap=gap, settle=settle, shrink=.5 * gap + .5 * q["mid"], mid=q["mid"])


def fill(q, side, p):
    book = dict(bids=[dict(price=q["bid"], quantity=q["bid_q"])], asks=[dict(price=q["ask"], quantity=q["ask_q"])])
    terms = dict(q["terms"], theta=min(1, q["terms"]["theta"] + p["theta_add"]))
    f = execute(book, side, budget=p["stake"], participation=p["participation"], slippage_ticks=p["slip"], **terms)
    return (f["quantity"], f["total"], f["average_price"]) if f["total"] >= .95 * p["stake"] and f["quantity"] else None


def qualifies(rule, prob, f, p):
    qty, cost, avg = f
    edge = rule["edge"] + p["edge_shift"]
    if rule.get("cap") and cost / qty > rule["cap"]:
        return False
    if rule["kind"] in ("abs", "follow"):
        return prob - cost / qty >= edge
    if rule["kind"] == "rel":
        lo, hi = rule["band"]
        return lo <= avg <= hi and cost / qty <= .85 and prob * qty - cost >= edge * cost
    lo, hi = rule["band"]                       # favourite: buy the side the market favours
    return lo <= cost / qty <= hi and prob >= .5


def run(by, order, outcome, rule, p, seed=1, outcome_override=None):
    rng = random.Random(seed)
    trades = []
    for k, slug in enumerate(order):
        quotes = [q for q in by[slug] if not (p["drop"] and rng.random() < p["drop"])]
        up = (outcome_override or outcome)[slug] == "UP"
        done = False
        for i, q in enumerate(quotes):
            if done or q["elapsed"] < rule["start"] + p["start_shift"]:
                continue
            if q["elapsed"] >= rule.get("end", 300) + p["start_shift"]:
                break
            end = q["t"] + q["left"]
            window = [x["ref"] for x in quotes[: i + 1] if x["t"] >= end - 60]
            pr = probs(q, p, rng, window)[rule["model"]]
            if pr is None:
                continue
            # "fav"/"follow": only the side the signal favours; "abs"/"rel": either side that passes.
            sides = (("UP", pr), ("DOWN", 1 - pr)) if rule["kind"] not in ("fav", "follow") else ((("UP", pr) if pr >= .5 else ("DOWN", 1 - pr)),)
            for side, ps in sides:
                f = fill(q, side, p)
                if not f or not qualifies(rule, ps, f, p):
                    continue
                exec_q = q
                if p["latency"]:                 # the order reaches the book later: use that quote
                    later = [x for x in quotes[i + 1:] if x["t"] >= q["t"] + p["latency"]]
                    exec_q = later[0] if later else None
                    f = fill(exec_q, side, p) if exec_q else None
                if f:
                    qty, cost, _ = f
                    trades.append(dict(k=k, pnl=qty * ((side == "UP") == up) - cost, cost=cost))
                done = True                       # decided this market (filled or missed)
                break
    return trades


def stats(trades, n):
    if not trades:
        return dict(trades=0)
    pnl = np.array([t["pnl"] for t in trades])
    w = int((pnl > 0).sum())
    lo, hi = wilson(w, len(pnl))
    cum = np.cumsum(pnl)
    return dict(trades=len(pnl), coverage=round(len(pnl) / n, 3), wins=w, win_rate=round(w / len(pnl), 3),
                win_ci95=[round(lo, 3), round(hi, 3)], pnl=round(float(pnl.sum()), 2),
                per_trade=round(float(pnl.mean()), 3), per_dollar=round(float(pnl.sum() / sum(t["cost"] for t in trades)), 4),
                max_drawdown=round(float(np.max(np.maximum.accumulate(np.concatenate([[0], cum]))[1:] - cum)), 2),
                worst_loss_streak=max_streak(pnl))


def max_streak(pnl):
    best = cur = 0
    for v in pnl:
        cur = cur + 1 if v <= 0 else 0
        best = max(best, cur)
    return best


SCENARIOS = {
    "baseline": {},
    "fees +0.02 theta": dict(theta_add=.02),
    "fees +0.05 theta": dict(theta_add=.05),
    "slippage 2 ticks": dict(slip=2),
    "slippage 3 ticks": dict(slip=3),
    "latency 2 s": dict(latency=2),
    "latency 5 s": dict(latency=5),
    "latency 10 s": dict(latency=10),
    "drop 25% of quotes": dict(drop=.25),
    "drop 50% of quotes": dict(drop=.50),
    "reference noise 1 bp": dict(ref_noise_bps=1),
    "reference noise 3 bp": dict(ref_noise_bps=3),
    "volatility x0.5": dict(vol_mult=.5),
    "volatility x2": dict(vol_mult=2),
    "stake $20": dict(stake=20),
    "stake $100": dict(stake=100),
    "depth 10% participation": dict(participation=.10),
    "start 30 s earlier": dict(start_shift=-30),
    "start 30 s later": dict(start_shift=30),
    "edge -5c": dict(edge_shift=-.05),
    "edge +5c": dict(edge_shift=.05),
    "combined: fees+2, slip 2, latency 2 s": dict(theta_add=.02, slip=2, latency=2),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quotes-cache", required=True)
    ap.add_argument("--out")
    ap.add_argument("--null-runs", type=int, default=200)
    ap.add_argument("--boot", type=int, default=2000)
    a = ap.parse_args()
    data = load(None, a.quotes_cache)
    by = {}
    for q in data["quotes"]:
        by.setdefault(q["slug"], []).append(q)
    order = sorted(by, key=lambda s: by[s][0]["t"])
    n = len(order)
    out = dict(markets=n, scenarios={}, robustness={})
    for name, change in SCENARIOS.items():
        p = dict(BASE, **change)
        out["scenarios"][name] = {r: stats(run(by, order, data["outcome"], rule, p, seed=3), n) for r, rule in RULES.items()}
    rng = random.Random(11)
    for rname, rule in RULES.items():
        base = run(by, order, data["outcome"], rule, dict(BASE))
        pnl = [t["pnl"] for t in base]
        rob = {}
        # Market bootstrap: resample whole markets (with their trade or skip).
        per_market = {t["k"]: t["pnl"] for t in base}
        boots = []
        for _ in range(a.boot):
            ks = [rng.randrange(n) for _ in range(n)]
            boots.append(sum(per_market.get(k, 0.0) for k in ks))
        rob["bootstrap_pnl_90pct"] = [round(float(np.percentile(boots, 5)), 2), round(float(np.percentile(boots, 95)), 2)]
        rob["bootstrap_prob_loss"] = round(float(np.mean(np.array(boots) < 0)), 3)
        # Fragility: remove the best trades.
        s = sorted(pnl, reverse=True)
        rob["pnl_without_best_3"] = round(sum(s[3:]), 2) if len(s) > 3 else None
        rob["pnl_without_best_5"] = round(sum(s[5:]), 2) if len(s) > 5 else None
        # Time split: first vs second half, and four quarters.
        rob["quarters_pnl"] = [round(sum(t["pnl"] for t in base if q * n // 4 <= t["k"] < (q + 1) * n // 4), 2) for q in range(4)]
        # Null: shuffle which market won; a real edge should collapse to a loss.
        nulls = []
        slugs = list(order)
        for j in range(a.null_runs):
            vals = [data["outcome"][s] for s in slugs]
            rng.shuffle(vals)
            fake = dict(zip(slugs, vals))
            nulls.append(sum(t["pnl"] for t in run(by, order, data["outcome"], rule, dict(BASE), outcome_override=fake)))
        rob["null_pnl_median"] = round(float(np.median(nulls)), 2)
        rob["null_pnl_95pct"] = round(float(np.percentile(nulls, 95)), 2)
        rob["real_pnl"] = round(sum(pnl), 2)
        rob["share_null_runs_at_or_above_real"] = round(float(np.mean(np.array(nulls) >= sum(pnl))), 3)
        out["robustness"][rname] = rob
    text = json.dumps(out, indent=2)
    print(text)
    if a.out:
        Path(a.out).write_text(text)


if __name__ == "__main__":
    main()
