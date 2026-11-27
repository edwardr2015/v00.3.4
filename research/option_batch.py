"""Batch-test catalogue options (research/OPTIONS-200.md) against the current best rule.

Baseline (#147): between 180 and 240 s, buy the Reference Gap model's side if the market agrees and
the all-in price is <= 90c; hold to settlement. Every option changes one thing (signal, entry,
filter, exit or risk rule). Realistic execution for everything: $3 budget, the order reaches the
book 2 s later (next recorded quote), 2-tick slippage, fee coefficient +0.02.

For each option: trades, wins, P&L, drawdown, half-split P&L, and the paired per-market difference
versus the baseline with a one-sided bootstrap p-value. Benjamini-Hochberg q-values correct for the
number of options tested. Learned signals (online learner, calibrated market, best-of weighting)
only use rounds that settled before the round being traded.

    python research/option_batch.py --quotes-cache quotes.pkl --out option_batch.json
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
from prophet.execution import execute  # noqa: E402
from new_models import features, logit, clip, phi  # noqa: E402
from quotes import load  # noqa: E402
from replay_sim import wilson  # noqa: E402

STAKE, LATENCY, SLIP, THETA_ADD = 3.0, 2.0, 2, .02
EXIT_STRESS = {}            # research knobs: {"taken": 0.5} or {"reprice": True}
EXIT_RNG = random.Random(11)


# ---------------------------------------------------------------- per-quote extra features
def enrich(rows):
    hi = lo = rows[0]["ref"] if rows else 0
    above = crossings = 0
    last_sign = None
    sign_since = rows[0]["t"] if rows else 0
    for i, r in enumerate(rows):
        hi, lo = max(hi, r["ref"]), min(lo, r["ref"])
        sign = r["ref"] >= r["opening"]
        if last_sign is not None and sign != last_sign:
            crossings += 1; sign_since = r["t"]
        last_sign = sign
        above += sign
        prev15 = next((p for p in reversed(rows[:i]) if r["t"] - p["t"] >= 15), None)
        r["accel"] = (r["vel"] - prev15["vel"]) if prev15 else 0.0
        r["range_pos"] = (r["ref"] - lo) / (hi - lo) if hi > lo else .5
        r["time_above"] = above / (i + 1)
        r["crossings"] = crossings
        r["persist"] = r["t"] - sign_since
        w30 = [p for p in rows[: i + 1] if r["t"] - p["t"] <= 30]
        if len(w30) >= 4:
            ts = np.array([p["t"] for p in w30]); ys = np.log([p["ref"] for p in w30])
            r["slope30"] = float(np.polyfit(ts - ts[0], ys, 1)[0])
        else:
            r["slope30"] = 0.0
        prev = rows[i - 1] if i else None
        r["book_lag"] = bool(prev and prev["bid"] == r["bid"] and prev["ask"] == r["ask"]
                             and abs(math.log(r["ref"] / prev["ref"])) * 1e4 >= 1.0)
        r["speed_p"] = clip(phi((r["gap"] + .5 * r["vel"] * min(r["left_s"], 60)) / r["sigma_app"]), .02, .98)
        r["accel_p"] = clip(phi((r["gap"] + .5 * r["vel"] * min(r["left_s"], 60)
                                 + .25 * r["accel"] * min(r["left_s"], 60)) / r["sigma_app"]), .02, .98)
        r["vol15_p"] = clip(phi(r["gap"] / (1.5 * r["sigma_app"])), .02, .98)
        r["slope_p"] = clip(phi((r["gap"] + r["slope30"] * min(r["left_s"], 60)) / r["sigma_app"]), .02, .98)
        r["meanrev_p"] = clip(phi(.7 * r["gap"] / r["sigma_app"]), .02, .98)
    return rows


# ---------------------------------------------------------------- learned signals (past rounds only)
class Learners:
    def __init__(self):
        self.rows = []; self.logit = self.calib = None; self.brier = {}

    def update(self, settled_rows):
        sample = {}
        for r in settled_rows:
            sample.setdefault(int(r["elapsed"] // 30), r)
        self.rows.extend(sample.values())
        if len(self.rows) < 60 or len({r["up"] for r in self.rows}) < 2:
            return
        from sklearn.linear_model import LogisticRegression
        X = np.array([[logit(r["gap_p"]), logit(r["mid"]), r["elapsed"] / 300, r["vel"] * 1e4,
                       r["mid_change"] * 10, r["imbalance"]] for r in self.rows])
        y = np.array([r["up"] for r in self.rows], float)
        self.logit = LogisticRegression(max_iter=500).fit(X, y)
        Xc = np.array([[logit(r["mid"]), logit(r["mid"]) * r["elapsed"] / 300] for r in self.rows])
        self.calib = LogisticRegression(max_iter=500).fit(Xc, y)
        late = [r for r in self.rows if r["elapsed"] >= 120][-300:]
        for k in ("gap_p", "mid", "speed_p"):
            self.brier[k] = float(np.mean([(r[k] - r["up"]) ** 2 for r in late])) if late else .25

    def online(self, r):
        if self.logit is None:
            return None
        x = [[logit(r["gap_p"]), logit(r["mid"]), r["elapsed"] / 300, r["vel"] * 1e4, r["mid_change"] * 10, r["imbalance"]]]
        return clip(float(self.logit.predict_proba(x)[0, 1]))

    def calibrated(self, r):
        if self.calib is None:
            return None
        return clip(float(self.calib.predict_proba([[logit(r["mid"]), logit(r["mid"]) * r["elapsed"] / 300]])[0, 1]))

    def best_of(self, r):
        if not self.brier:
            return None
        w = {k: math.exp(-40 * b) for k, b in self.brier.items()}
        return clip(sum(w[k] * r[k] for k in w) / sum(w.values()))


# ---------------------------------------------------------------- execution
def fill(q, side, buying=True, quantity=None):
    book = dict(bids=[dict(price=q["bid"], quantity=q["bid_q"])], asks=[dict(price=q["ask"], quantity=q["ask_q"])])
    terms = dict(q["terms"], theta=min(1, q["terms"]["theta"] + THETA_ADD))
    if buying:
        f = execute(book, side, budget=STAKE, slippage_ticks=SLIP, **terms)
        return (f["quantity"], f["total"]) if f["total"] >= .95 * STAKE and f["quantity"] else None
    f = execute(book, side, quantity=quantity, buying=False, slippage_ticks=SLIP, **terms)
    return f["total"] if f["quantity"] + 1e-9 >= quantity else None


def play_round(rows, opt, signal, state, trace=None):
    window = opt.get("window", (180, 240))
    for i, r in enumerate(rows):
        if not window[0] <= r["elapsed"] < window[1]:
            continue
        p = signal(r)
        if p is None:
            continue
        side = "UP" if p >= .5 else "DOWN"
        if opt.get("agree", True) and (r["mid"] >= .5) != (side == "UP"):
            continue
        conf = p if side == "UP" else 1 - p
        if conf < opt.get("min_conf", 0) or not opt.get("entry", lambda r, side, conf: True)(r, side, conf):
            continue
        if not opt.get("filter", lambda r: True)(r):
            return None, "FILTERED"
        arrival = next((x for x in rows[i:] if x["t"] >= r["t"] + LATENCY), None)
        if arrival is None:
            return None, "MISSED"
        f = fill(arrival, side)
        if not f:
            continue
        qty, cost = f
        price = cost / qty
        if price > opt.get("cap", .90) or price < opt.get("floor", 0):
            continue
        settle = qty * ((side == "UP") == r["up"])
        exit_rule = opt.get("exit")
        proceeds, peak, exit_info = None, 0.0, None
        if exit_rule:
            for j, x in enumerate(rows):
                if x["t"] <= arrival["t"]:
                    continue
                seen = fill(x, side, buying=False, quantity=qty)      # what the bot sees at decision time
                if seen is None:
                    continue
                px = signal(x) or p
                held = px if side == "UP" else 1 - px
                peak = max(peak, seen)
                fire = ((exit_rule == "probability" and seen >= held * qty + .03 * cost)
                        or (exit_rule == "trailing" and peak >= 1.3 * cost and seen <= peak - .15 * qty)
                        or (exit_rule == "time270" and x["elapsed"] >= 270)
                        or (exit_rule == "reversal" and ((px >= .5) != (side == "UP"))))
                if not fire:
                    continue
                # The sell order also arrives LATENCY seconds later; if the round ends first, hold.
                sell_at = next((y for y in rows[j:] if y["t"] >= x["t"] + LATENCY), None)
                sale = fill(sell_at, side, buying=False, quantity=qty) if sell_at else None
                if sale is not None and EXIT_STRESS.get("reprice"):
                    sale = min(sale, held * qty)            # book catches up to the model before we sell
                if sale is not None and EXIT_RNG.random() < EXIT_STRESS.get("taken", 0):
                    sale = None                             # a faster bot hit the stale bid first: hold
                if sale is not None:
                    proceeds = sale
                    exit_info = dict(signal_t=x["elapsed"], sold_t=sell_at["elapsed"], proceeds=round(sale, 4),
                                     model_p=round(px, 3))
                    break
        value = settle if proceeds is None else proceeds
        if trace is not None:
            trace.append(dict(slug=r["slug"], decision_s=round(r["elapsed"], 1), decision_t=r["t"], side=side, model_p=round(p, 3),
                              market_mid=r["mid"], arrival_s=round(arrival["elapsed"], 1), qty=qty, cost=round(cost, 4),
                              all_in_price=round(price, 3), exit=exit_info, outcome="UP" if r["up"] else "DOWN",
                              pnl=round(value - cost, 4)))
        return value - cost, "TRADED"
    return None, "NO_SIGNAL"


# ---------------------------------------------------------------- options
def options(L):
    g = lambda r: r["gap_p"]  # noqa: E731
    O = {}
    O["#147 BASELINE: minute 3-4, agree, <=90c"] = dict(signal=g)
    # signals
    O["#1 gap + speed signal"] = dict(signal=lambda r: r["speed_p"])
    O["#2 gap + acceleration signal"] = dict(signal=lambda r: r["accel_p"])
    O["#14 last-30s slope signal"] = dict(signal=lambda r: r["slope_p"])
    O["#5 mean-reversion signal"] = dict(signal=lambda r: r["meanrev_p"])
    O["#22 volatility x1.5 signal"] = dict(signal=lambda r: r["vol15_p"])
    O["#68 market price as signal"] = dict(signal=lambda r: r["mid"])
    O["#69 calibrated market signal"] = dict(signal=L.calibrated)
    O["#113 online learner signal"] = dict(signal=L.online)
    O["#131 best-of weighting signal"] = dict(signal=L.best_of)
    O["#133 median of gap/market/speed"] = dict(signal=lambda r: float(np.median([r["gap_p"], r["mid"], r["speed_p"]])))
    O["#134 all three agree (gap, market, speed)"] = dict(signal=g, entry=lambda r, s, c: (r["speed_p"] >= .5) == (s == "UP"))
    # entry timing / thresholds
    O["#149 gap >= 1 sd"] = dict(signal=g, entry=lambda r, s, c: abs(r["gap_z"]) >= 1)
    O["#149b gap >= 1.5 sd"] = dict(signal=g, entry=lambda r, s, c: abs(r["gap_z"]) >= 1.5)
    O["#150 model >= 80%"] = dict(signal=g, min_conf=.80)
    O["#150b model >= 90%"] = dict(signal=g, min_conf=.90)
    O["#151 price 70-85c only"] = dict(signal=g, floor=.70, cap=.85)
    O["#151b price 60-90c only"] = dict(signal=g, floor=.60)
    O["#153 breakout: at round high/low"] = dict(signal=g, entry=lambda r, s, c: (r["range_pos"] >= .9) if s == "UP" else (r["range_pos"] <= .1))
    O["#152 pullback: not at round extreme"] = dict(signal=g, entry=lambda r, s, c: (r["range_pos"] < .9) if s == "UP" else (r["range_pos"] > .1))
    O["#157 book-lag entry"] = dict(signal=g, entry=lambda r, s, c: r["book_lag"])
    O["#159 market moving our way"] = dict(signal=g, entry=lambda r, s, c: (r["mid_change"] > 0) == (s == "UP"))
    O["#156 window 150-240 s"] = dict(signal=g, window=(150, 240))
    O["#156b window 180-210 s"] = dict(signal=g, window=(180, 210))
    O["#156c window 210-240 s"] = dict(signal=g, window=(210, 240))
    O["#17 gap held same side >= 60 s"] = dict(signal=g, entry=lambda r, s, c: r["persist"] >= 60)
    O["#13 crossed the open <= 2 times"] = dict(signal=g, entry=lambda r, s, c: r["crossings"] <= 2)
    O["#11 >= 70% of round on our side"] = dict(signal=g, entry=lambda r, s, c: (r["time_above"] >= .7) if s == "UP" else (r["time_above"] <= .3))
    O["#16 skip momentum exhaustion"] = dict(signal=g, entry=lambda r, s, c: not ((r["vel"] > 0) == (s == "UP") and (r["accel"] < 0) and abs(r["vel"]) > 2e-6))
    # filters
    O["#171 skip spread > 2c"] = dict(signal=g, filter=lambda r: r["ask"] - r["bid"] <= .02)
    O["#172 skip thin top size < 50"] = dict(signal=g, filter=lambda r: min(r["bid_q"], r["ask_q"]) >= 50)
    O["#173 skip high volatility"] = dict(signal=g, filter=lambda r: r["vol"] < 2.5e-4)
    O["#174 skip low volatility"] = dict(signal=g, filter=lambda r: r["vol"] > 6e-5)
    O["#175 skip near-zero gap (< 0.5 sd)"] = dict(signal=g, filter=lambda r: abs(r["gap_z"]) >= .5)
    O["#176 skip if gap and speed models disagree"] = dict(signal=g, filter=lambda r: (r["gap_p"] >= .5) == (r["speed_p"] >= .5))
    O["#47 order-flow agrees (imbalance)"] = dict(signal=g, entry=lambda r, s, c: (r["imbalance"] >= 0) == (s == "UP"))
    # exits
    O["#162 probability exit"] = dict(signal=g, exit="probability")
    O["#166 trailing exit"] = dict(signal=g, exit="trailing")
    O["#167 time exit at 4:30"] = dict(signal=g, exit="time270")
    O["#168 reversal exit"] = dict(signal=g, exit="reversal")
    # combinations of the strongest single ideas are added after the first pass (see main)
    return O


def evaluate(rounds, opt, learners_factory, risk=None):
    L = learners_factory()
    per = []
    streak = 0
    day_pnl = 0.0
    for k, rows in enumerate(rounds):
        sig = opt["signal"] if not callable(opt.get("signal_factory")) else opt["signal_factory"](L)
        pnl, why = (None, "PAUSED")
        if not (risk and risk(streak, day_pnl)):
            pnl, why = play_round(rows, opt, opt["signal"], None)
        per.append(pnl if pnl is not None else 0.0)
        if pnl is not None:
            streak = streak + 1 if pnl <= 0 else 0
            day_pnl += pnl
        L.update(rows)
    return np.array(per)


def stats(per, n):
    traded = per != 0
    t = per[traded]
    if not len(t):
        return dict(trades=0)
    w = int((t > 0).sum())
    cum = np.cumsum(per)
    dd = float(np.max(np.maximum.accumulate(np.concatenate([[0], cum]))[1:] - cum))
    return dict(trades=int(traded.sum()), wins=w, win_rate=round(w / len(t), 3), pnl=round(float(per.sum()), 2),
                per_trade=round(float(t.mean()), 3), max_drawdown=round(dd, 2),
                first_half=round(float(per[: n // 2].sum()), 2), second_half=round(float(per[n // 2:].sum()), 2))


def paired_p(diff, rng, runs=4000):
    """One-sided bootstrap p-value that the mean per-market improvement is <= 0."""
    if not np.any(diff):
        return 1.0
    n = len(diff)
    means = np.array([diff[rng.integers(0, n, n)].mean() for _ in range(runs)])
    return float(np.mean(means <= 0))


def bh(pvals):
    order = np.argsort(pvals); m = len(pvals); q = np.empty(m); prev = 1.0
    for rank, idx in reversed(list(enumerate(order, 1))):
        prev = min(prev, pvals[idx] * m / rank); q[idx] = prev
    return q


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
    rounds = [enrich(features(by[s])) for s in order]
    n = len(rounds)
    # Learned signals must only see settled earlier rounds: give each option its own learner instance.
    results, per_by = {}, {}
    holder = {}
    def factory():
        holder["L"] = Learners(); return holder["L"]
    base_L = Learners()
    names = list(options(base_L).keys())
    for name in names:
        L = Learners()
        opt = options(L)[name]
        per = []
        for rows in rounds:
            pnl, _ = play_round(rows, opt, opt["signal"], None)
            per.append(pnl if pnl is not None else 0.0)
            L.update(rows)
        per_by[name] = np.array(per)
    # Risk rules applied to the baseline trade sequence (daily stop, pause after losses, cooldown).
    base = per_by[names[0]]
    def with_risk(rule):
        out, streak, day, cool = [], 0, 0.0, 0
        for v in base:
            if rule(streak, day) or cool:
                out.append(0.0); cool = max(0, cool - 1); continue
            out.append(v)
            if v != 0:
                streak = streak + 1 if v < 0 else 0; day += v
                if v < 0 and rule is cooldown:
                    cool = 1
        return np.array(out)
    def cooldown(s, d): return False
    risk = {
        "#181 daily stop -$15": lambda s, d: d <= -15,
        "#182 daily stop -$10": lambda s, d: d <= -10,
        "#183 profit lock at +$15": lambda s, d: d >= 15,
        "#178 pause after 3 losses in a row": lambda s, d: s >= 3,
        "#187 skip next round after a loss": cooldown,
    }
    for name, rule in risk.items():
        per_by[name] = with_risk(rule)
    rng = np.random.default_rng(42)
    names_all = list(per_by.keys())
    pvals = np.array([paired_p(per_by[k] - base, rng) if k != names[0] else 1.0 for k in names_all])
    qvals = bh(pvals[1:]) if len(pvals) > 1 else []
    for i, k in enumerate(names_all):
        s = stats(per_by[k], n)
        s["vs_baseline"] = round(float((per_by[k] - base).sum()), 2)
        s["p_improves"] = round(float(pvals[i]), 3) if i else None
        s["q_after_correction"] = round(float(qvals[i - 1]), 3) if i else None
        results[k] = s
    out = dict(rounds=n, options_tested=len(names_all) - 1, results=results)
    text = json.dumps(out, indent=2)
    print(text)
    if a.out:
        Path(a.out).write_text(text)


if __name__ == "__main__":
    main()
