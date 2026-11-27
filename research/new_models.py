"""Seven new forecasting models tested walk-forward on recorded quotes.

Every model forecasts P(UP) at each recorded quote using only that quote and earlier quotes in the
same round; learned models (history table, blended logistic, boosted trees) are trained only on
EARLIER rounds (expanding window, retrained every 5 rounds, first used after 30 rounds).

Scoring on the same evaluation rounds (31..94) for every model:
  * Brier score per minute of the round (one quote per round per minute)
  * Trading at $3 with the app fill model, held to settlement:
      follow:  between 180 and 240 s, buy the model's side if it agrees with the market and the
               all-in price is <= 90c (the best rule from improve_search.py)
      value:   between 180 and 240 s, buy a side when model probability - all-in price >= 5c

    python research/new_models.py --quotes-cache quotes.pkl --out new_models.json
"""
import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "btc-prophet"))
from prophet.execution import execute  # noqa: E402
from quotes import load  # noqa: E402
from replay_sim import wilson  # noqa: E402

START_EVAL, RETRAIN, STAKE = 30, 5, 3.0


def phi(z):
    return .5 * (1 + math.erf(z / math.sqrt(2)))


def clip(p, lo=.01, hi=.99):
    return min(hi, max(lo, p))


def logit(p):
    p = clip(p)
    return math.log(p / (1 - p))


def features(rows):
    """Per-quote features from this quote and earlier quotes of the same round only."""
    out = []
    for i, q in enumerate(rows):
        past = [r for r in rows[:i] if q["t"] - r["t"] <= 30]
        old = past[0] if past else None
        span = (q["t"] - old["t"]) if old else 0
        vel = math.log(q["ref"] / old["ref"]) / span if old and span >= 5 else 0.0
        mid_change = (q["mid"] - old["mid"]) if old and span >= 5 else 0.0
        # realised volatility from this round's own reference moves (per second)
        moves = [(math.log(b["ref"] / a["ref"]), b["t"] - a["t"]) for a, b in zip(rows[:i], rows[1:i + 1])
                 if b["t"] > a["t"] and b["ref"] != a["ref"] or b["t"] - a["t"] > 0]
        moves = [(m, dt) for m, dt in moves if dt > 0]
        rv = math.sqrt(sum(m * m for m, _ in moves) / sum(dt for _, dt in moves)) if len(moves) >= 5 else None
        left = max(q["left"], 1.0)
        sigma_app = max(1e-5, q["vol"] * math.sqrt(max(1, left / 15)))
        gap = math.log(q["ref"] / q["opening"])
        tot = q["bid_q"] + q["ask_q"]
        out.append(dict(q, gap_z=gap / sigma_app, vel=vel, mid_change=mid_change, rv=rv, left_s=left,
                        imbalance=(q["bid_q"] - q["ask_q"]) / tot if tot else 0.0, sigma_app=sigma_app, gap=gap))
    return out


def fixed_models(r):
    p = {"Reference Gap (current)": r["gap_p"], "Market price": r["mid"]}
    # 1. gap + speed: project the recent drift (damped by half) over the time left
    drift = .5 * r["vel"] * min(r["left_s"], 60)
    p["1. Gap + speed"] = clip(phi((r["gap"] + drift) / r["sigma_app"]), .02, .98)
    # 2. real-volatility gap: sigma from this round's own reference moves
    if r["rv"]:
        p["2. Real-volatility gap"] = clip(phi(r["gap"] / max(1e-6, r["rv"] * math.sqrt(r["left_s"]))), .02, .98)
    else:
        p["2. Real-volatility gap"] = r["gap_p"]
    # 3. market momentum: market price pushed in the direction it has been moving
    p["3. Market momentum"] = clip(r["mid"] + 1.0 * r["mid_change"])
    # 7. order-book pressure on top of the market price
    p["7. Order-book pressure"] = clip(r["mid"] + .05 * r["imbalance"])
    return p


def vector(r):
    return [logit(r["gap_p"]), logit(r["mid"]), r["elapsed"] / 300, r["mid_change"] * 10, r["vel"] * 1e4,
            r["imbalance"], (r["ask"] - r["bid"]) * 10]


class Learned:
    """History table, blended logistic and boosted trees, trained on earlier rounds only."""

    def __init__(self):
        self.table = None; self.logit = None; self.gbm = None

    def fit(self, train_rows):
        sample = {}
        for r in train_rows:                               # one quote per round per 30 s
            sample.setdefault((r["slug"], int(r["elapsed"] // 30)), r)
        rows = list(sample.values())
        y = np.array([r["up"] for r in rows], float)
        # 4. history table: P(UP | gap bucket, minute), shrunk toward the gap model
        tab = {}
        for r in rows:
            k = (int(np.clip(np.floor(r["gap_z"]), -3, 2)), int(r["elapsed"] // 60))
            n, s, prior = tab.get(k, (0, 0.0, 0.0))
            tab[k] = (n + 1, s + r["up"], prior + r["gap_p"])
        self.table = tab
        X = np.array([vector(r) for r in rows])
        if len(set(y)) < 2:
            return
        from sklearn.linear_model import LogisticRegression
        from sklearn.ensemble import HistGradientBoostingClassifier
        self.logit = LogisticRegression(C=1.0, max_iter=500).fit(X, y)
        self.gbm = HistGradientBoostingClassifier(max_depth=3, max_iter=120, learning_rate=.05,
                                                  min_samples_leaf=20, random_state=0).fit(X, y)

    def predict(self, r):
        out = {}
        k = (int(np.clip(np.floor(r["gap_z"]), -3, 2)), int(r["elapsed"] // 60))
        n, s, prior = self.table.get(k, (0, 0.0, 0.0)) if self.table else (0, 0, 0)
        out["4. History table"] = clip((s + 5 * r["gap_p"]) / (n + 5))
        if self.logit is not None:
            x = np.array([vector(r)])
            out["5. Blended learner"] = clip(float(self.logit.predict_proba(x)[0, 1]))
            out["6. Boosted learner (ML)"] = clip(float(self.gbm.predict_proba(x)[0, 1]))
        return out


def fill(q, side):
    book = dict(bids=[dict(price=q["bid"], quantity=q["bid_q"])], asks=[dict(price=q["ask"], quantity=q["ask_q"])])
    f = execute(book, side, budget=STAKE, **q["terms"])
    return (f["quantity"], f["total"]) if f["total"] >= .95 * STAKE and f["quantity"] else None


def trade(rows_by_market, model, rule):
    pnl, wins, n = 0.0, 0, 0
    for slug, rows in rows_by_market.items():
        for r in rows:
            if not 180 <= r["elapsed"] < 240 or model not in r["p"]:
                continue
            p = r["p"][model]
            pick = None
            if rule == "follow":
                side = "UP" if p >= .5 else "DOWN"
                if (r["mid"] >= .5) == (side == "UP"):
                    f = fill(r, side)
                    if f and f[1] / f[0] <= .90:
                        pick = (side, f)
            else:
                for side, ps in (("UP", p), ("DOWN", 1 - p)):
                    f = fill(r, side)
                    if f and ps - f[1] / f[0] >= .05:
                        pick = (side, f); break
            if pick:
                side, (qty, cost) = pick
                won = (side == "UP") == r["up"]
                pnl += qty * won - cost; wins += won; n += 1
                break
    lo, hi = wilson(wins, n) if n else (None, None)
    return dict(trades=n, wins=wins, win_rate=round(wins / n, 3) if n else None, pnl=round(pnl, 2),
                per_trade=round(pnl / n, 3) if n else None)


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
    rounds = [features(by[s]) for s in order]
    learner, trained_at = Learned(), None
    evaluation = {}
    for k, rows in enumerate(rounds):
        if k >= START_EVAL and (trained_at is None or k - trained_at >= RETRAIN):
            learner.fit([r for past in rounds[:k] for r in past]); trained_at = k
        for r in rows:
            r["p"] = fixed_models(r)
            if k >= START_EVAL:
                r["p"].update(learner.predict(r))
        if k >= START_EVAL:
            evaluation[order[k]] = rows
    models = sorted({m for rows in evaluation.values() for r in rows for m in r["p"]})
    out = dict(rounds=len(order), evaluation_rounds=len(evaluation), models={})
    for m in models:
        brier = {}
        for lo in range(0, 300, 60):
            pts = []
            for rows in evaluation.values():
                r = next((r for r in rows if lo <= r["elapsed"] < lo + 60 and m in r["p"]), None)
                if r:
                    pts.append((r["p"][m] - r["up"]) ** 2)
            brier[f"{lo}-{lo+60}s"] = round(float(np.mean(pts)), 4) if pts else None
        out["models"][m] = dict(brier=brier, follow=trade(evaluation, m, "follow"), value=trade(evaluation, m, "value"))
    text = json.dumps(out, indent=2)
    print(text)
    if a.out:
        Path(a.out).write_text(text)


if __name__ == "__main__":
    main()
