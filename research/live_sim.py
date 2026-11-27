"""Simulate LIVE execution through an exchange connection, on recorded quotes.

Paper trading fills at the quote the bot saw. A live order is different:
  * it reaches the exchange LATENCY seconds later (the next recorded quote at/after that time);
  * it is an immediate-or-cancel LIMIT order at the decision price + SLIP ticks: if the ask has
    moved above the limit it is rejected, no fill;
  * faster bots may take the displayed size first (COMPETITION probability), and only the top
    level is recorded, so a taken level means no fill (conservative);
  * a filled order pays the taker fee on the arrival price and is held to official settlement.
Bankroll: start $50, $3 orders, one position at a time (markets do not overlap), stop for the day
after -$15, stop when cash < $3. Days are 288 markets resampled from the recorded session.

    python research/live_sim.py --quotes-cache quotes.pkl --out live.json
"""
import argparse
import json
import random
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "btc-prophet"))
from prophet.execution import execute  # noqa: E402
from quotes import load  # noqa: E402

START, STAKE, DAY_STOP, PER_DAY = 50.0, 3.0, 15.0, 288
RULES = {
    "Minute 3-4 follow": dict(start=180, end=240, cap=None),
    "Minute 3-4 follow (max 85c)": dict(start=180, end=240, cap=.85),
    "90% late favourite": dict(start=240, end=300, band=(.85, .99), signal="mid"),
}


def side_price(q, side):
    return q["ask"] if side == "UP" else 1 - q["bid"]


def side_size(q, side):
    return q["ask_q"] if side == "UP" else q["bid_q"]


def decide(quotes, rule):
    """First quote in the window where the rule wants to buy; returns (index, side, price)."""
    for i, q in enumerate(quotes):
        if not rule["start"] <= q["elapsed"] < rule["end"]:
            continue
        p = q["mid"] if rule.get("signal") == "mid" else q["gap_p"]
        side = "UP" if p >= .5 else "DOWN"
        price = side_price(q, side)
        if rule.get("cap") and price > rule["cap"]:
            continue
        if rule.get("band") and not rule["band"][0] <= price <= rule["band"][1]:
            continue
        return i, side, price
    return None


def execute_live(quotes, i, side, price, latency, slip, competition, rng, theta_add=0.0):
    t0 = quotes[i]["t"]
    arrival = next((q for q in quotes[i:] if q["t"] >= t0 + latency), None) if latency else quotes[i]
    if arrival is None:
        return None, "MISSED_ROUND_ENDED"
    limit = round(price + slip * .01, 2)
    ask = side_price(arrival, side)
    if ask > limit + 1e-9:
        return None, "REJECTED_PRICE_MOVED"
    if rng.random() < competition:
        return None, "TAKEN_BY_FASTER_BOT"
    book = dict(bids=[dict(price=arrival["bid"], quantity=arrival["bid_q"])],
                asks=[dict(price=arrival["ask"], quantity=arrival["ask_q"])])
    terms = dict(arrival["terms"], theta=min(1, arrival["terms"]["theta"] + theta_add))
    f = execute(book, side, budget=STAKE, participation=1.0, slippage_ticks=0, **terms)
    if f["total"] < .95 * STAKE or not f["quantity"]:
        return None, "NOT_ENOUGH_SIZE"
    return (f["quantity"], f["total"]), "FILLED"


def per_market(by, order, outcome, rule, latency, slip, competition, seed=7):
    rng = random.Random(seed)
    pnl = np.zeros(len(order))
    reasons = {}
    signals = fills = wins = 0
    for k, slug in enumerate(order):
        d = decide(by[slug], rule)
        if not d:
            continue
        signals += 1
        res, why = execute_live(by[slug], *d, latency, slip, competition, rng)
        reasons[why] = reasons.get(why, 0) + 1
        if res:
            qty, cost = res
            won = (d[1] == "UP") == (outcome[slug] == "UP")
            pnl[k] = qty * won - cost
            fills += 1
            wins += won
    return pnl, dict(signals=signals, fills=fills, wins=wins, reasons=reasons)


def bankroll(pnl, runs, rng, days=1):
    n = len(pnl)
    finals, stopped = [], 0
    for _ in range(runs):
        cash = START
        for _ in range(days):
            day_start = cash
            for _ in range(PER_DAY):
                if cash < STAKE or cash <= day_start - DAY_STOP:
                    break
                cash += pnl[rng.randrange(n)]
            stopped += cash <= day_start - DAY_STOP
        finals.append(cash)
    f = np.array(finals)
    return dict(median=round(float(np.median(f)), 2), p10=round(float(np.percentile(f, 10)), 2),
                p90=round(float(np.percentile(f, 90)), 2), chance_of_loss=round(float(np.mean(f < START)), 3),
                hit_daily_stop=round(stopped / (runs * days), 3))


SCENARIOS = {
    "paper (instant, no competition)": (0, 0, 0),
    "good connection: 0.5 s, +1 tick limit": (.5, 1, 0),
    "home internet: 1 s, +1 tick": (1, 1, 0),
    "home internet: 2 s, +1 tick": (2, 1, 0),
    "2 s, exact price (no extra tick)": (2, 0, 0),
    "2 s, +2 ticks": (2, 2, 0),
    "2 s, +1 tick, 25% taken by faster bots": (2, 1, .25),
    "2 s, +1 tick, 50% taken by faster bots": (2, 1, .50),
    "slow: 5 s, +1 tick, 25% taken": (5, 1, .25),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quotes-cache", required=True)
    ap.add_argument("--runs", type=int, default=5000)
    ap.add_argument("--out")
    a = ap.parse_args()
    data = load(None, a.quotes_cache)
    by = {}
    for q in data["quotes"]:
        by.setdefault(q["slug"], []).append(q)
    order = sorted(by, key=lambda s: by[s][0]["t"])
    rng = random.Random(1)
    out = dict(start=START, stake=STAKE, daily_stop=DAY_STOP, markets=len(order), rules={})
    for rname, rule in RULES.items():
        out["rules"][rname] = {}
        for sname, (lat, slip, comp) in SCENARIOS.items():
            pnl, info = per_market(by, order, data["outcome"], rule, lat, slip, comp)
            info.update(pnl_94=round(float(pnl.sum()), 2),
                        win_rate=round(info["wins"] / info["fills"], 3) if info["fills"] else None,
                        fill_rate=round(info["fills"] / info["signals"], 3) if info["signals"] else None,
                        one_day=bankroll(pnl, a.runs, rng), one_week=bankroll(pnl, max(500, a.runs // 10), rng, 7))
            out["rules"][rname][sname] = info
    text = json.dumps(out, indent=2)
    print(text)
    if a.out:
        Path(a.out).write_text(text)


if __name__ == "__main__":
    main()
