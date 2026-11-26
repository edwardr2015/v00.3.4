"""Chronological offline research only. Does not modify or promote a live model.

First run the baseline, then optionally --train-challenger after reviewing it.
All predictions here are retrospective, never pre-recorded forward predictions.
"""

import argparse
import hashlib
import json
import math
import statistics
import time
from pathlib import Path

from .history import History, day, iso


def features(bars, decision_time):
    """Only 21 complete 15-second bars that closed by the decision timestamp."""
    required = list(range(decision_time - 315, decision_time, 15))
    if any(t not in bars or bars[t]["ticks"] != 15 for t in required):
        return None
    closes = [bars[t]["close"] for t in required]
    returns = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    sigma = max(statistics.pstdev(returns), 1e-7)
    return [
        math.log(closes[-1] / closes[-5]) / (sigma * 2),
        math.log(closes[-1] / closes[0]) / (sigma * math.sqrt(20)),
        math.log(sigma),
    ]


def baseline_probability(x):
    # A deliberately simple, uncalibrated momentum heuristic. No fitted accuracy claim.
    return 0.5 + 0.2 * math.tanh(0.6 * x[0] + 0.4 * x[1])


def decision(p):
    return "UP" if p >= 0.55 else "DOWN" if p <= 0.45 else "SKIP"


def examples(history, start, end, entry_offset=90):
    bars = {
        r["time"]: dict(r)
        for r in history.db.execute(
            "SELECT * FROM reference15 WHERE time>=? AND time<?", (start - 315, end)
        )
    }
    rows, rejected = [], 0
    for m in history.db.execute(
        "SELECT * FROM markets WHERE start>=? AND end<=? ORDER BY start", (start, end)
    ):
        entry = m["start"] + entry_offset
        x = features(bars, entry)
        if x is None:
            rejected += 1
            continue
        quote = history.db.execute(
            "SELECT * FROM quotes WHERE slug=? AND time<=? ORDER BY time DESC LIMIT 1",
            (m["slug"], entry),
        ).fetchone()
        rows.append(
            dict(
                slug=m["slug"],
                decision_time=entry,
                feature_end=entry,
                x=x,
                y=m["outcome"],
                label_end=m["end"],
                quote_age=entry - quote["time"] if quote else None,
                up_display_ask=quote["up_ask"] if quote else None,
                down_display_ask=quote["down_ask"] if quote else None,
                market_terms={
                    key: json.loads(m["payload"]).get(key)
                    for key in ["orderPriceMinTickSize", "minimumTradeQty", "feeCoefficient"]
                },
            )
        )
    return rows, rejected


def chronological_split(rows, start, end):
    if end - start != 7 * 86400:
        raise ValueError("This research protocol requires exactly seven complete UTC days")
    partitions = {"train": [], "validation": [], "test": []}
    for r in rows:
        # Training labels must be settled by the next partition boundary.
        if r["label_end"] <= start + 4 * 86400:
            partitions["train"].append(r)
        elif r["decision_time"] >= start + 4 * 86400 and r["label_end"] <= start + 5 * 86400:
            partitions["validation"].append(r)
        elif r["decision_time"] >= start + 5 * 86400 and r["label_end"] <= end:
            partitions["test"].append(r)
    return partitions


def sigmoid(z):
    return 1 / (1 + math.exp(-max(-35, min(35, z))))


def fit_challenger(train):
    if len(train) < 100 or len({r["y"] for r in train}) < 2:
        raise ValueError("Need at least 100 training examples with both outcomes")
    # Preprocessing sees training data only. Fixed hyperparameters: no test-set search.
    means = [statistics.mean(r["x"][j] for r in train) for j in range(3)]
    scales = [max(statistics.pstdev(r["x"][j] for r in train), 1e-8) for j in range(3)]
    data = [
        ([1] + [(v - means[j]) / scales[j] for j, v in enumerate(r["x"])], r["y"]) for r in train
    ]
    weights = [0.0] * 4
    for _ in range(300):
        gradient = [0.0] * 4
        for x, y in data:
            error = sigmoid(sum(a * b for a, b in zip(weights, x))) - y
            for j in range(4):
                gradient[j] += error * x[j] / len(data)
        for j in range(4):
            weights[j] -= 0.05 * (gradient[j] + (0.01 * weights[j] if j else 0))
    return dict(
        type="logistic_challenger",
        version="offline-logistic-v0.1",
        means=means,
        scales=scales,
        weights=weights,
        iterations=300,
        step_size=0.05,
        l2=0.01,
        promoted=False,
    )


def challenger_probability(model, x):
    values = [1] + [(v - model["means"][j]) / model["scales"][j] for j, v in enumerate(x)]
    return sigmoid(sum(a * b for a, b in zip(model["weights"], values)))


def metrics(rows, predict):
    if not rows:
        return dict(examples=0)
    probabilities = [predict(r["x"]) for r in rows]
    correct = sum(int(p >= 0.5) == r["y"] for p, r in zip(probabilities, rows))
    choices = [decision(p) for p in probabilities]
    chosen = [(p, r) for p, r, d in zip(probabilities, rows, choices) if d != "SKIP"]
    return dict(
        examples=len(rows),
        up_outcome_fraction=sum(r["y"] for r in rows) / len(rows),
        directional_accuracy=correct / len(rows),
        brier_score=sum((p - r["y"]) ** 2 for p, r in zip(probabilities, rows)) / len(rows),
        log_loss=-sum(
            r["y"] * math.log(max(p, 1e-12)) + (1 - r["y"]) * math.log(max(1 - p, 1e-12))
            for p, r in zip(probabilities, rows)
        )
        / len(rows),
        directional_choices=len(chosen),
        skipped_by_direction_threshold=choices.count("SKIP"),
        chosen_direction_accuracy=sum(int(p >= 0.5) == r["y"] for p, r in chosen) / len(chosen)
        if chosen
        else None,
        quotes_no_older_than_15_seconds=sum(
            r["quote_age"] is not None and r["quote_age"] <= 15 for r in rows
        ),
        hypothetical_trades=0,
        pnl=None,
    )


def run(args):
    start, end = day(args.start), day(args.end)
    history = History(args.db, args.cache)
    try:
        rows, rejected = examples(history, start, end)
        parts = chronological_split(rows, start, end)
        if any(len(part) < 100 for part in parts.values()):
            raise ValueError("Insufficient chronological coverage: need 100 examples per partition")
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
        majority = sum(r["y"] for r in parts["train"]) / len(parts["train"])
        predictors = {
            "momentum_baseline_v0.1": baseline_probability,
            "coin_flip_reference": lambda x: 0.5,
            "training_base_rate_reference": lambda x: majority,
        }
        model = None
        if args.train_challenger:
            baseline_path = Path(args.baseline_report)
            if (
                not baseline_path.exists()
                or json.loads(baseline_path.read_text()).get("dataset_sha256") != digest
            ):
                raise ValueError(
                    "Run and review the baseline report on this exact dataset before training a challenger"
                )
            model = fit_challenger(parts["train"])
            predictors[model["version"]] = lambda x: challenger_probability(model, x)
        results = {
            name: {part: metrics(samples, predict) for part, samples in parts.items()}
            for name, predict in predictors.items()
        }
        report = dict(
            generated_at=iso(time.time()),
            start=iso(start),
            end_exclusive=iso(end),
            dataset_sha256=digest,
            valid_examples=len(rows),
            excluded_for_missing_or_partial_features=rejected,
            entry_seconds_after_open=90,
            decision_thresholds={"UP": 0.55, "DOWN": 0.45},
            partition_boundaries={
                "train": [iso(start), iso(start + 4 * 86400)],
                "validation": [iso(start + 4 * 86400), iso(start + 5 * 86400)],
                "test": [iso(start + 5 * 86400), iso(end)],
            },
            predictions_are_retrospective=True,
            active_model_changed=False,
            executable_liquidity_known=False,
            limitations=[
                "No historical depth or fill simulation; no P&L claim",
                "Input publication latency is not reconstructed; features use candle close boundaries",
                "Seven days is a short sample; results do not establish future accuracy",
                "Direction thresholds do not substitute for contract expected-value and risk checks",
            ],
            models=results,
        )
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n")
        if model:
            model.update(
                dataset_sha256=digest,
                train_start=iso(start),
                train_end=iso(start + 4 * 86400),
                trained_at=report["generated_at"],
            )
            artifact = out.with_name("challenger-model.json")
            artifact.write_text(json.dumps(model, indent=2) + "\n")
            versions = out.parent / "models"
            versions.mkdir(exist_ok=True)
            (versions / (model["version"] + "-" + digest[:12] + ".json")).write_text(
                json.dumps(model, indent=2) + "\n"
            )
        print(json.dumps(report, indent=2))
    finally:
        history.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--db", default="data/history.sqlite3")
    p.add_argument("--cache", default="data/history-cache")
    p.add_argument("--output", default="data/baseline-report.json")
    p.add_argument("--baseline-report", default="data/baseline-report.json")
    p.add_argument("--train-challenger", action="store_true")
    run(p.parse_args())
