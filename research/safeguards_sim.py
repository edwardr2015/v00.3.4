"""Remove primary-account entry/exit safeguards one at a time and replay on recorded quotes.

Primary rule as coded in prophet/unified.py (0.3.23):
  forecast  Reference Gap probability, shrunk toward the book midpoint (calibrate(): with few
            settled labels the estimate is ~0.5*raw + 0.5*midpoint)
  entry     first quote in the round where the better-EV side fills a $5 budget with average
            price 40-70c, all-in <= 85c per contract, and EV >= 10% of cost
  exit      probability exit: sell on a later fresh quote when sale proceeds >= held probability
            x quantity + 3% of cost; otherwise hold to the official settlement
Never relaxed here: quote freshness, executable depth (app fill model), one position at a time,
official settlement. Quotes without a usable book are excluded, never filled.

    python research/safeguards_sim.py --quotes-cache quotes.pkl --out safeguards.json
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "btc-prophet"))
from prophet.execution import execute  # noqa: E402
from quotes import load  # noqa: E402
from replay_sim import wilson  # noqa: E402

STAKE = 5.0
BASE = dict(band=(.40, .70), allin_cap=.85, edge=.10, shrink=True, exit="probability", start=0,
            end=300, agree=False, flip=False)
VARIANTS = {
    "A baseline (current primary rule)": {},
    "B no price band": dict(band=(.01, .99), allin_cap=1.0),
    "C no minimum edge (EV >= 0)": dict(edge=0.0),
    "D no edge at all (best side always)": dict(edge=-9.0),
    "E raw model, no market shrink": dict(shrink=False),
    "F hold to settlement (no early exit)": dict(exit="hold"),
    "G flips allowed": dict(flip=True),
    "H entries only before 90 s": dict(end=90),
    "I entries only at/after 90 s": dict(start=90),
    "J add model+market agreement": dict(agree=True),
    "K band + edge removed": dict(band=(.01, .99), allin_cap=1.0, edge=-9.0),
    "L all removed (band, edge, shrink, timing) + hold": dict(band=(.01, .99), allin_cap=1.0,
                                                              edge=-9.0, shrink=False, exit="hold"),
}


def book(q):
    return dict(bids=[dict(price=q["bid"], quantity=q["bid_q"])],
                asks=[dict(price=q["ask"], quantity=q["ask_q"])])


def prob(q, cfg):
    return .5 * q["gap_p"] + .5 * q["mid"] if cfg["shrink"] else q["gap_p"]


def entry(q, cfg):
    p = prob(q, cfg)
    if cfg["agree"] and (q["gap_p"] >= .5) != (q["mid"] >= .5):
        return None
    best = None
    for side, ps in (("UP", p), ("DOWN", 1 - p)):
        f = execute(book(q), side, budget=STAKE, **q["terms"])
        if f["total"] < .95 * STAKE or not f["quantity"]:
            continue
        lo, hi = cfg["band"]
        if not lo - 1e-10 <= f["average_price"] <= hi + 1e-10 or f["total"] / f["quantity"] > cfg["allin_cap"]:
            continue
        ev = ps * f["quantity"] - f["total"]
        if ev < cfg["edge"] * f["total"]:
            continue
        if not best or ev > best["ev"]:
            best = dict(side=side, qty=f["quantity"], cost=f["total"], ev=ev, p=ps, t=q["t"])
    return best


def play(market_quotes, up, cfg):
    pos = None
    flipped = False
    for q in market_quotes:
        if not cfg["start"] <= q["elapsed"] < 300:
            continue
        if pos is None:
            if q["elapsed"] >= cfg["end"]:
                continue
            pos = entry(q, cfg)
            if pos:
                pos.update(proceeds=None, entry_elapsed=q["elapsed"])
            continue
        if q["t"] <= pos["t"]:
            continue
        p = prob(q, cfg)
        held = p if pos["side"] == "UP" else 1 - p
        if cfg["exit"] == "probability":
            sale = execute(book(q), pos["side"], quantity=pos["qty"], buying=False, **q["terms"])
            if sale["quantity"] + 1e-9 >= pos["qty"] and sale["total"] >= held * pos["qty"] + .03 * pos["cost"]:
                return dict(pos, proceeds=sale["total"], exited=True, flipped=flipped)
        if cfg["flip"] and not flipped:
            other = "DOWN" if pos["side"] == "UP" else "UP"
            sale = execute(book(q), pos["side"], quantity=pos["qty"], buying=False, **q["terms"])
            buy = execute(book(q), other, budget=STAKE, **q["terms"])
            if sale["quantity"] + 1e-9 >= pos["qty"] and buy["total"] >= .95 * STAKE:
                op = 1 - held
                gain = sale["total"] - buy["total"] + op * buy["quantity"] - held * pos["qty"]
                if op * buy["quantity"] - buy["total"] >= .10 * buy["total"] and gain >= .10 * pos["cost"]:
                    realised = sale["total"] - pos["cost"]
                    pos = dict(side=other, qty=buy["quantity"], cost=buy["total"], t=q["t"],
                               carried=realised, entry_elapsed=pos["entry_elapsed"])
                    flipped = True
    if pos:
        return dict(pos, proceeds=None, exited=False, flipped=flipped)
    return None


def simulate(quotes, outcome, cfg):
    by = {}
    for q in quotes:
        by.setdefault(q["slug"], []).append(q)
    order = sorted(by, key=lambda s: by[s][0]["t"])
    trades = []
    for slug in order:
        up = outcome[slug] == "UP"
        r = play(by[slug], up, cfg)
        if not r:
            continue
        settle = r["qty"] * ((r["side"] == "UP") == up)
        value = r["proceeds"] if r["exited"] else settle
        pnl = value - r["cost"] + r.get("carried", 0)
        trades.append(dict(slug=slug, pnl=pnl, win=pnl > 0, exited=r["exited"], flipped=r["flipped"],
                           held_would=settle - r["cost"] + r.get("carried", 0), cost=r["cost"],
                           order=order.index(slug)))
    return trades, len(order)


def stats(trades, markets):
    n = len(trades)
    if not n:
        return dict(trades=0, skips=markets)
    w = sum(t["win"] for t in trades)
    pnl = sum(t["pnl"] for t in trades)
    run = peak = dd = 0.0
    for t in trades:
        run += t["pnl"]; peak = max(peak, run); dd = max(dd, peak - run)
    lo, hi = wilson(w, n)
    return dict(trades=n, skips=markets - n, wins=w, win_rate=round(w / n, 3), win_ci95=[round(lo, 3), round(hi, 3)],
                net_pnl=round(pnl, 2), per_dollar=round(pnl / sum(t["cost"] for t in trades), 4),
                max_drawdown=round(dd, 2), early_exits=sum(t["exited"] for t in trades),
                flips=sum(t["flipped"] for t in trades),
                hold_instead_pnl=round(sum(t["held_would"] for t in trades), 2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quotes-cache", required=True)
    ap.add_argument("--workbook")
    ap.add_argument("--out")
    a = ap.parse_args()
    data = load(a.workbook, a.quotes_cache)
    out = {}
    for name, change in VARIANTS.items():
        cfg = dict(BASE, **change)
        trades, n = simulate(data["quotes"], data["outcome"], cfg)
        half = n // 2
        out[name] = dict(all=stats(trades, n),
                         first_half=stats([t for t in trades if t["order"] < half], half),
                         second_half=stats([t for t in trades if t["order"] >= half], n - half))
    text = json.dumps(out, indent=2)
    print(text)
    if a.out:
        Path(a.out).write_text(text)


if __name__ == "__main__":
    main()
