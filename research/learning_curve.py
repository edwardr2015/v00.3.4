"""Does training on fewer markets help? Walk-forward learning curve with the app's own model code.

Inputs come from a Full (Paper) Report:
  * Challenger Outcomes: the exact 3 model features captured live (momentum, trend, volatility)
  * Forward Quote Audit: the executable quote nearest before each capture (market price, gap model)
  * UP DOWN History: official outcomes

For each minimum training size N0, every market from N0 onward is predicted by a model trained
ONLY on earlier markets (retrained every RETRAIN markets, label purged by construction because
markets are strictly sequential). Scores are reported on one common evaluation set so that
different N0 are comparable. A trade is simulated at the capture quote when the model's
probability exceeds the all-in price by EDGE, using the app's fill model, held to settlement.

    python research/learning_curve.py PAPER_REPORT.xlsx --quotes-cache quotes.pkl --out lc.json
"""

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "btc-prophet"))
from prophet.models import fit_artifact, predict_artifact  # noqa: E402
from prophet.research import baseline_probability  # noqa: E402
from quotes import load  # noqa: E402
from replay_sim import fill, wilson  # noqa: E402

KINDS = ("logistic", "random_forest", "gradient_boosting", "xgboost", "lightgbm", "ensemble")
RETRAIN = 5
EDGE = .05
MIN_SIZES = (10, 20, 30, 40, 60)


def ts(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def rows(workbook, quotes):
    wb = openpyxl.load_workbook(workbook, read_only=True, data_only=True)
    by_slug = {}
    for q in quotes:
        by_slug.setdefault(q["slug"], []).append(q)
    best = {}
    for r in list(wb["Challenger Outcomes"].iter_rows(values_only=True))[1:]:
        family, _, slug, second, when, _, _, outcome, _, _, feats = r
        if family != "momentum" or not 30 <= second <= 90 or outcome not in ("UP", "DOWN"):
            continue
        if slug not in best or second > best[slug]["second"]:      # latest capture <= 90 s
            best[slug] = dict(slug=slug, second=second, time=ts(when), x=json.loads(feats),
                              y=int(outcome == "UP"))
    out = []
    for slug, r in best.items():
        prior = [q for q in by_slug.get(slug, []) if q["t"] <= r["time"] + 1]
        if not prior or not all(map(math.isfinite, r["x"])):
            continue
        q = prior[-1]
        out.append(dict(r, quote=q, mid=q["mid"], gap=q["gap_p"], decision_time=r["time"],
                        label_end=q["t"] + q["left"]))
    out.sort(key=lambda r: r["time"])
    return out


def brier(ps, ys):
    return float(np.mean((np.array(ps) - np.array(ys)) ** 2))


def trade(r, p):
    """Buy the side whose model probability beats its all-in price by EDGE; else skip."""
    for side, prob in (("UP", p), ("DOWN", 1 - p)):
        qty, cost = fill(r["quote"], side)
        if cost and prob - cost / qty >= EDGE:
            won = (side == "UP") == bool(r["y"])
            return qty * won - cost, won
    return None


def walk(data, kind, n0):
    preds, model, trained_on = {}, None, 0
    for t in range(n0, len(data)):
        if model is None or t - trained_on >= RETRAIN:
            past = data[:t]
            cut = int(len(past) * .75)
            try:
                model = fit_artifact(kind, past[:cut] if kind == "ensemble" else past, past[cut:])
                trained_on = t
            except (ValueError, ImportError) as exc:   # e.g. only one outcome so far
                model = None
                preds[t] = None
                continue
        preds[t] = float(predict_artifact(model, data[t]["x"]))
    return preds


def evaluate(data, preds, idx):
    ok = [i for i in idx if preds.get(i) is not None]
    ys = [data[i]["y"] for i in ok]
    out = dict(markets=len(ok), brier=round(brier([preds[i] for i in ok], ys), 4) if ok else None,
               accuracy=round(float(np.mean([(preds[i] >= .5) == data[i]["y"] for i in ok])), 3) if ok else None)
    t = [trade(data[i], preds[i]) for i in ok]
    t = [x for x in t if x]
    wins = sum(w for _, w in t)
    out.update(trades=len(t), wins=wins, win_rate=round(wins / len(t), 3) if t else None,
               win_ci95=[round(v, 3) for v in wilson(wins, len(t))] if t else None,
               pnl=round(sum(p for p, _ in t), 2))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workbook")
    ap.add_argument("--quotes-cache")
    ap.add_argument("--out")
    a = ap.parse_args()
    data = rows(a.workbook, load(a.workbook, a.quotes_cache)["quotes"])
    n = len(data)
    common = list(range(max(MIN_SIZES), n))           # same evaluation markets for every N0
    res = dict(markets=n, evaluation_markets=len(common), retrain_every=RETRAIN, edge=EDGE,
               first=data[0]["slug"], last=data[-1]["slug"], up_rate=round(np.mean([r["y"] for r in data]), 3))
    fixed = {"market_price": {i: data[i]["mid"] for i in range(n)},
             "reference_gap": {i: data[i]["gap"] for i in range(n)},
             "momentum_fixed": {i: baseline_probability(data[i]["x"]) for i in range(n)},
             "coin_flip": {i: .5 for i in range(n)}}
    res["benchmarks"] = {k: evaluate(data, v, common) for k, v in fixed.items()}
    res["models"] = {}
    for kind in KINDS:
        res["models"][kind] = {}
        for n0 in MIN_SIZES:
            preds = walk(data, kind, n0)
            res["models"][kind][f"min_{n0}"] = dict(common=evaluate(data, preds, common),
                                                    all_from_n0=evaluate(data, preds, range(n0, n)))
    text = json.dumps(res, indent=2)
    print(text)
    if a.out:
        Path(a.out).write_text(text)


if __name__ == "__main__":
    main()
