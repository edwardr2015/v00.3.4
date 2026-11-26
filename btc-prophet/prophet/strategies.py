"""Research-inspired strategy comparison; exploratory, conditional fills, no promotion."""

import argparse
import json
import math
from pathlib import Path

from .backtest import replay
from .history import History, day, iso
from .research import (
    baseline_probability,
    challenger_probability,
    examples,
    fit_challenger,
    metrics,
)

SOURCES = [
    dict(
        idea="Intraday momentum",
        url="https://doi.org/10.1111/fire.12290",
        limitation="Paper studies half-hour effects; our five-minute adaptation is not a replication",
    ),
    dict(
        idea="Momentum and reversal",
        url="https://doi.org/10.1016/j.najef.2022.101733",
        limitation="Evidence depends on regime, horizon, liquidity and costs",
    ),
    dict(
        idea="Order flow imbalance",
        url="https://arxiv.org/abs/1011.6402",
        limitation="Studied stocks; excluded from historical replay because US depth history is missing",
    ),
    dict(
        idea="Settlement reference rules",
        url="https://docs.polymarket.us/faqs/crypto-faqs",
        limitation="Use typed US BRTI terms; never substitute international Chainlink contracts",
    ),
]


def clamp(p):
    return max(0.02, min(0.98, p))


def fit_calibration(rows, predict):
    # Two-parameter logistic calibration fits validation labels only, not test labels.
    a, b = 1.0, 0.0
    for _ in range(200):
        ga = gb = 0.0
        for r in rows:
            p = clamp(predict(r["x"]))
            z = math.log(p / (1 - p))
            estimate = 1 / (1 + math.exp(-max(-35, min(35, a * z + b))))
            error = estimate - r["y"]
            ga += error * z / len(rows)
            gb += error / len(rows)
        a -= 0.05 * (ga + 0.01 * (a - 1))
        b -= 0.05 * gb
    return a, b


def calibrated(p, a, b):
    p = clamp(p)
    return clamp(1 / (1 + math.exp(-max(-35, min(35, a * math.log(p / (1 - p)) + b)))))


def run(start, end, db_path="data/history.sqlite3", output="data/strategy-comparison.json"):
    if end - start != 14 * 86400:
        raise ValueError("Use fourteen complete UTC days")
    h = History(db_path, Path(db_path).parent / "history-cache")
    try:
        rows, missing = examples(h, start, end, entry_offset=60)
        anchors = {
            r["slug"]: r["opening"] for r in h.db.execute("SELECT slug,opening FROM markets")
        }
        prices = {r["time"]: r["close"] for r in h.db.execute("SELECT time,close FROM reference15")}
        for r in rows:
            last = prices.get(r["decision_time"] - 15)
            if last is None:
                r["x"].append(0.0)
                continue
            # Zero-drift Brownian approximation to a final 60s average. Not an exchange model.
            seconds = max(1, r["label_end"] - r["decision_time"] - 40)
            z = math.log(last / anchors[r["slug"]]) / (
                max(math.exp(r["x"][2]), 1e-7) * math.sqrt(seconds / 15)
            )
            r["x"].append(z)
        training = [r for r in rows if r["label_end"] <= start + 5 * 86400]
        validation = [
            r
            for r in rows
            if r["decision_time"] >= start + 5 * 86400 and r["label_end"] <= start + 7 * 86400
        ]
        test = [r for r in rows if r["decision_time"] >= start + 7 * 86400]
        from .depth import at

        for r in test:
            r["observed_book"] = at(h.db, r["slug"], int(r["decision_time"]) * 10**9)
        full_depth = all(r["observed_book"] is not None for r in test)
        model = fit_challenger([dict(r, x=r["x"][:3]) for r in training])

        def logistic(x):
            return challenger_probability(model, x[:3])

        a, b = fit_calibration(validation, logistic)
        candidates = {
            "MOMENTUM": lambda x: baseline_probability(x[:3]),
            "MEAN_REVERSION": lambda x: 1 - baseline_probability(x[:3]),
            "LOGISTIC_TRAIN_5_DAYS": logistic,
            "CALIBRATED_LOGISTIC_5_PLUS_2": lambda x: calibrated(logistic(x), a, b),
            "VOLATILITY_REFERENCE_GAP": lambda x: clamp(0.5 * (1 + math.erf(x[3] / math.sqrt(2)))),
            "GAP_LOGISTIC_ENSEMBLE": lambda x: (
                clamp(0.5 * (1 + math.erf(x[3] / math.sqrt(2)))) + logistic(x)
            )
            / 2,
        }
        result = dict(
            start=iso(start),
            training_end=iso(start + 5 * 86400),
            validation_end=iso(start + 7 * 86400),
            test_end_exclusive=iso(end),
            test_examples=len(test),
            excluded=missing,
            sources=SOURCES,
            exploratory_after_test_inspection=True,
            active_model_changed=False,
            limitations=[
                "Not proof of executable profit; all fill quantities are hypothetical",
                "No exact replication of published papers at this horizon",
                "Many comparisons on an inspected test week create selection bias; new data required",
                "Gap model assumes zero drift, fixed volatility and known published opening reference",
                "Same hard risk stops, no automatic restart or stake escalation",
                "No take-profit rules are enabled",
            ],
            models={},
            status="CAUSAL_DEPTH_SCENARIOS"
            if full_depth
            else "EXECUTION_DISABLED_INCOMPLETE_DEPTH",
        )
        for name, predict in candidates.items():
            scenarios = []
            for label, theta, tick in [
                ("STANDARD_ASSUMED_FULL_FILL", 0.095, 0.01),
                ("HIGHER_COST_ASSUMED_FULL_FILL", 0.12, 0.02),
                ("ASSUMED_HALF_FILL", 0.095, 0.01),
            ]:
                if not full_depth:
                    continue
                r = replay(
                    test,
                    predict,
                    0.5 if label == "ASSUMED_HALF_FILL" else 1.0,
                    theta=theta,
                    tick=tick,
                    label=name + ":" + label,
                    use_depth=True,
                )
                r.pop("ledger")
                r["label"] = label
                r["combined_main_pnl"] = sum(
                    a["realized"] for a in r["accounts"] if a["id"].startswith("main")
                )
                scenarios.append(r)
            result["models"][name] = dict(
                validation=metrics(validation, predict),
                test=metrics(test, predict),
                scenarios=scenarios,
            )
        chosen = min(
            result["models"], key=lambda n: result["models"][n]["validation"]["brier_score"]
        )
        result["validation_selected_candidate"] = chosen
        Path(output).write_text(json.dumps(result, indent=2) + "\n")
        print(
            json.dumps(
                {
                    n: dict(
                        test_brier=m["test"]["brier_score"],
                        pnl=[round(s["combined_main_pnl"], 2) for s in m["scenarios"]],
                    )
                    for n, m in result["models"].items()
                },
                indent=2,
            )
        )
    finally:
        h.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--db", default="data/history.sqlite3")
    p.add_argument("--output", default="data/strategy-comparison.json")
    args = p.parse_args()
    run(day(args.start), day(args.end), args.db, args.output)
