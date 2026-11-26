"""Original/new replay on a contiguous causal L2 cohort; never implies a full week."""

import csv
import json
import math
from pathlib import Path

from .backtest import replay
from .depth import at
from .history import History, day, iso
from .research import baseline_probability, examples, metrics


def run():
    start, end = day("2026-11-08"), day("2026-11-22")
    h = History()
    try:
        rows, _ = examples(h, start, end, entry_offset=60)
        test = [r for r in rows if r["decision_time"] >= start + 7 * 86400]
        anchors = {
            r["slug"]: r["opening"] for r in h.db.execute("SELECT slug,opening FROM markets")
        }
        prices = {r["time"]: r["close"] for r in h.db.execute("SELECT time,close FROM reference15")}
        coverage = 0
        prefix = []
        missing = False
        for r in test:
            book = at(h.db, r["slug"], int(r["decision_time"]) * 10**9)
            coverage += int(book is not None)
            if book is None:
                missing = True
            if missing:
                continue
            r["observed_book"] = book
            r["quote_age"] = (int(r["decision_time"]) * 10**9 - book["source_ns"]) / 1e9
            r["up_display_ask"] = book["asks"][0]["price"] if book["asks"] else None
            r["down_display_ask"] = 1 - book["bids"][0]["price"] if book["bids"] else None
            sigma = max(math.exp(r["x"][2]), 1e-7)
            z = math.log(prices[r["decision_time"] - 15] / anchors[r["slug"]]) / (
                sigma * math.sqrt(max(1, r["label_end"] - r["decision_time"] - 40) / 15)
            )
            r["x"].append(z)
            prefix.append(r)
        if not prefix:
            raise ValueError("No contiguous causal books at the beginning of the replay period")
        candidates = {
            "ORIGINAL_RESEARCH_MOMENTUM": lambda x: baseline_probability(x[:3]),
            "NEW_VOLATILITY_REFERENCE_GAP": lambda x: max(
                0.02, min(0.98, 0.5 * (1 + math.erf(x[3] / math.sqrt(2))))
            ),
        }
        report = dict(
            status="PARTIAL_CAUSAL_SAMPLE_NOT_WEEK_PROFIT"
            if len(prefix) < len(test)
            else "CAUSAL_DEPTH_SCENARIO",
            full_week_markets=len(test),
            causal_books_available=coverage,
            contiguous_replayed_markets=len(prefix),
            entry_start=iso(prefix[0]["decision_time"]),
            last_settlement=iso(prefix[-1]["label_end"]),
            price_source="PMData Polymarket US L2, source and receive timestamps <= entry",
            execution="25% displayed depth, one adverse tick, theta >=0.095, fixed total-cost budgets",
            take_profit_enabled=False,
            active_model_changed=False,
            usable_for_profitability_claims=False,
            limitations=[
                "Only the contiguous downloaded sample is replayed; no skipped archival gaps",
                "Recorded liquidity does not guarantee fills; latency and participation remain assumptions",
                "Both strategies hold until settlement; same hard risk gates, no manual restarts",
                "Research momentum is not an exact recreation of every forward-engine candle/book adjustment",
                "New candidate is exploratory; no pristine independent holdout or profitability conclusion",
            ],
            models={},
        )
        ledger = []
        for name, predict in candidates.items():
            cases = []
            for label, theta, tick in [("STANDARD", 0.095, 0.01), ("HIGHER_COSTS", 0.12, 0.02)]:
                result = replay(
                    prefix,
                    predict,
                    1,
                    theta=theta,
                    tick=tick,
                    label=name + ":" + label,
                    use_depth=True,
                )
                entries = result.pop("ledger")
                ledger.extend(entries)
                result["label"] = label
                result["combined_main_pnl"] = sum(
                    a["realized"] for a in result["accounts"] if a["id"].startswith("main")
                )
                result["combined_main_trades"] = sum(
                    a["trades"] for a in result["accounts"] if a["id"].startswith("main")
                )
                cases.append(result)
            report["models"][name] = dict(
                direction_metrics=metrics(prefix, predict), scenarios=cases
            )
        out = Path("data/depth-comparison")
        out.mkdir(exist_ok=True)
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        fields = sorted({k for r in ledger for k in r})
        with (out / "ledger.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(ledger)
        print(json.dumps({k: v for k, v in report.items() if k != "models"}, indent=2))
        for name, m in report["models"].items():
            print(
                name,
                [
                    (s["label"], round(s["combined_main_pnl"], 4), s["combined_main_trades"])
                    for s in m["scenarios"]
                ],
            )
    finally:
        h.close()


if __name__ == "__main__":
    run()
