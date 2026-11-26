"""Fourteen-day development and seven-day conditional P&L replay.

Historical quotes lack quantities. Capacity scenarios are declared assumptions,
not reconstructed order books. No output is a real or forward trading result.
"""

import argparse
import csv
import hashlib
import json
from pathlib import Path

from .execution import fee, execute
from .history import History, day, iso
from .research import (
    baseline_probability,
    challenger_probability,
    examples,
    fit_challenger,
    metrics,
)
from .risk import entry_block, settlement_block, utc_day


def assumed_fill(ask, budget, fraction, theta=0.095, tick=0.01, quantum=0.01):
    price = round(ask + tick, 10)
    if not 0 < price < 1 or fraction <= 0:
        return dict(quantity=0, cost=0, fee=0, price=price)
    cap = budget * fraction
    q = int((cap / (price * (1 + theta * (1 - price)))) / quantum) * quantum
    while q > 0:
        f = fee(theta * q * price * (1 - price))
        cost = q * price + f
        if cost <= cap + 1e-9:
            return dict(quantity=q, cost=cost, fee=f, price=price)
        q = round(q - quantum, 10)
    return dict(quantity=0, cost=0, fee=0, price=price)


def replay(
    rows,
    predict,
    capacity=1.0,
    theta=0.095,
    tick=0.01,
    checkpoint=True,
    label=None,
    exit_factor=None,
    stop_factor=None,
    min_edge=0.03,
    min_confidence=0.0,
    max_spread=1.0,
    min_price=0.0,
    max_price=1.0,
    use_depth=False,
):
    accounts = [
        dict(
            id=f"{group}-{b}",
            budget=b,
            balance=100.0,
            realized=0.0,
            streak=0,
            halted=None,
            day="",
            day_pnl=0.0,
            trades=0,
            wins=0,
            peak=100.0,
            drawdown=0.0,
            stopped_at=None,
            last_entry=None,
        )
        for group in ["main", "control"]
        for b in (1, 2, 3)
    ]
    ledger = []
    for row in sorted(rows, key=lambda r: r["decision_time"]):
        probability = predict(row["x"])
        asks = {"UP": row["up_display_ask"], "DOWN": row["down_display_ask"]}
        quote_ok = row["quote_age"] is not None and 0 <= row["quote_age"] <= 15
        observed_book = row.get("observed_book")
        if use_depth:
            entry_ns = int(row["decision_time"]) * 10**9
            quote_ok = bool(
                observed_book
                and observed_book.get("state") == "MARKET_STATE_OPEN"
                and observed_book.get("source_ns", entry_ns + 1) <= entry_ns
                and observed_book.get("received_ns", entry_ns + 1) <= entry_ns
                and 0 <= entry_ns - observed_book.get("source_ns", 0) <= 15 * 10**9
            )
            asks = dict(
                UP=observed_book["asks"][0]["price"]
                if observed_book and observed_book["asks"]
                else None,
                DOWN=1 - observed_book["bids"][0]["price"]
                if observed_book and observed_book["bids"]
                else None,
            )
        terms = row.get("market_terms") or {}
        try:
            market_tick = float(terms["orderPriceMinTickSize"])
            quantum = float(terms["minimumTradeQty"])
            fee_theta = max(theta, float(terms["feeCoefficient"]))
            terms_ok = 0 < market_tick < 1 and 0 < quantum <= 100 and 0.0695 <= fee_theta <= 1
        except (KeyError, ValueError, TypeError):
            terms_ok = False
        adverse_tick = max(tick, market_tick) if terms_ok else tick

        def fill_order(side, budget):
            if not use_depth:
                return assumed_fill(asks[side], budget, capacity, fee_theta, adverse_tick, quantum)
            fill = execute(
                observed_book,
                side,
                budget=budget,
                theta=fee_theta,
                tick=market_tick,
                quantum=quantum,
                participation=0.25 * capacity,
                slippage_ticks=max(1, round(adverse_tick / market_tick)),
            )
            return dict(fill, cost=fill["total"])

        alternatives = []
        if quote_ok and terms_ok:
            for side, p in [("UP", probability), ("DOWN", 1 - probability)]:
                if asks[side] is None:
                    continue
                if not min_price <= asks[side] <= max_price or p < min_confidence:
                    continue
                if (
                    any(v is None for v in asks.values())
                    or sum(asks.values()) - 1 > max_spread + 1e-9
                ):
                    continue
                if capacity <= 0:
                    continue
                f = fill_order(side, 1)
                edge = f["quantity"] * p - f["cost"]
                alternatives.append((edge, side))
        best = max(alternatives, default=(-1, "SKIP"))
        for a in accounts:
            reason = None
            if a["halted"]:
                reason = "MANUAL_REVIEW_REQUIRED"
            elif not terms_ok:
                reason = "MISSING_OR_INVALID_MARKET_TERMS"
            elif capacity == 0:
                reason = "ZERO_CAPACITY_SCENARIO"
            elif not quote_ok:
                reason = "MISSING_OR_STALE_HISTORICAL_QUOTE"
            elif checkpoint and a["id"].startswith("main") and (row["label_end"] - 300) % 3600 == 0:
                reason = "HOURLY_OBSERVATION_CHECKPOINT"
            elif best[0] < min_edge:
                reason = "NO_POSITIVE_EXPECTED_VALUE"
            side = best[1]
            if not reason:
                reason = entry_block(a, 0, a["budget"], row["decision_time"])
                if reason and reason not in ("ONE_POSITION_LIMIT", "INSUFFICIENT_BALANCE"):
                    a["halted"] = reason
                    a["stopped_at"] = iso(row["decision_time"])
            fill = fill_order(side, a["budget"]) if not reason else None
            if fill and not fill["quantity"]:
                reason = "UNFILLED_ASSUMED_CAPACITY"
            if (
                fill
                and fill["quantity"] * (probability if side == "UP" else 1 - probability)
                - fill["cost"]
                < min_edge * fill["cost"]
            ):
                reason = "INSUFFICIENT_EDGE_AFTER_COSTS"
            record = dict(
                account=a["id"],
                slug=row["slug"],
                entry_utc=iso(row["decision_time"]),
                settlement_utc=iso(row["label_end"]),
                estimated_up=probability,
                side=side,
                quote_age=row["quote_age"],
                quote_source="CAUSAL_L2" if use_depth else "UNVERIFIED_DISPLAY_HISTORY",
                l2_source_ns=observed_book["source_ns"] if use_depth and observed_book else None,
                l2_received_ns=observed_book["received_ns"]
                if use_depth and observed_book
                else None,
                l2_file_sha256=observed_book.get("file_sha256")
                if use_depth and observed_book
                else None,
                capacity_assumption=capacity,
                reason=reason,
                scenario=label or f"CAPACITY_{capacity}",
                quantity=0,
                cost=0,
                fee=0,
                pnl=None,
                balance=a["balance"],
                outcome="UP" if row["y"] else "DOWN",
            )
            if not reason:
                # Five-minute windows do not overlap. This settlement precedes the next entry.
                pnl = (fill["quantity"] if (side == "UP") == bool(row["y"]) else 0) - fill["cost"]
                exit_time = row["label_end"]
                exit_reason = "SETTLEMENT"
                impossible = bool(exit_factor and fill["quantity"] < fill["cost"] * exit_factor)
                for q in row.get("quote_trace", []):
                    if not row["decision_time"] < q["time"] < row["label_end"]:
                        continue
                    bid = 1 - q["down_ask"] if side == "UP" else 1 - q["up_ask"]
                    price = max(0, bid - adverse_tick)
                    net = fill["quantity"] * price - fee(
                        fee_theta * fill["quantity"] * price * (1 - price)
                    )
                    profit_hit = (
                        exit_factor and not impossible and net >= fill["cost"] * exit_factor
                    )
                    stop_hit = stop_factor and net <= fill["cost"] * stop_factor
                    if profit_hit or stop_hit:
                        pnl = net - fill["cost"]
                        exit_time = q["time"]
                        exit_reason = "SAMPLED_PROFIT_TARGET" if profit_hit else "SAMPLED_STOP_LOSS"
                        break
                a["balance"] += pnl
                a["realized"] += pnl
                a["streak"] = a["streak"] + 1 if pnl < 0 else 0
                settlement_day = utc_day(exit_time)
                a["day_pnl"] = (a["day_pnl"] if a["day"] == settlement_day else 0) + pnl
                a["day"] = settlement_day
                a["trades"] += 1
                a["wins"] += int(pnl > 0)
                a["peak"] = max(a["peak"], a["balance"])
                a["drawdown"] = max(a["drawdown"], (a["peak"] - a["balance"]) / a["peak"])
                a["halted"] = settlement_block(a)
                a["last_entry"] = iso(row["decision_time"])
                if a["halted"]:
                    a["stopped_at"] = iso(exit_time)
                record.update(
                    quantity=fill["quantity"],
                    cost=fill["cost"],
                    fee=fill["fee"],
                    pnl=pnl,
                    balance=a["balance"],
                    reason="ASSUMED_FILL",
                    halted_after=a["halted"],
                    exit_reason=exit_reason,
                    exit_utc=iso(exit_time),
                    target_impossible=impossible,
                )
            ledger.append(record)
    return dict(
        capacity_fraction=capacity,
        accounts=accounts,
        ledger=ledger,
        entry_rules=dict(
            min_edge=min_edge,
            min_confidence=min_confidence,
            max_spread=max_spread,
            min_price=min_price,
            max_price=max_price,
        ),
    )


def run(args):
    start, end = day(args.start), day(args.end)
    if end - start != 14 * 86400:
        raise ValueError("Select exactly fourteen complete UTC days")
    history = History(args.db, args.cache)
    try:
        rows, excluded = examples(history, start, end, entry_offset=60)
        train = [r for r in rows if r["label_end"] <= start + 5 * 86400]
        validation = [
            r
            for r in rows
            if r["decision_time"] >= start + 5 * 86400 and r["label_end"] <= start + 7 * 86400
        ]
        test = [
            r for r in rows if r["decision_time"] >= start + 7 * 86400 and r["label_end"] <= end
        ]
        for r in test:
            from .depth import at

            r["observed_book"] = at(history.db, r["slug"], int(r["decision_time"]) * 10**9)
            r["quote_trace"] = [
                dict(q)
                for q in history.db.execute(
                    "SELECT time,up_ask,down_ask FROM quotes WHERE slug=? ORDER BY time",
                    (r["slug"],),
                )
            ]
        depth_coverage = sum(r["observed_book"] is not None for r in test)
        if args.include_exit_research and not args.allow_unverified_display_history:
            raise ValueError(
                "Take-profit research is disabled; legacy replay requires the explicit unverified audit flag"
            )
        if not args.allow_unverified_display_history and depth_coverage < len(test):
            raise ValueError(
                f"Historical P&L blocked: only {depth_coverage}/{len(test)} causal entry books are available. Minute display-price timestamps conflict with L2. Use a complete depth cohort; --allow-unverified-display-history is audit-only."
            )
        if args.allow_unverified_display_history:
            print(
                "WARNING: WITHDRAWN unverified display-price replay; no profitability claims are valid.",
                flush=True,
            )
        if min(len(train), len(validation), len(test)) < 100:
            raise ValueError("Insufficient chronological coverage")
        candidate = fit_challenger(train)
        validation_baseline = metrics(validation, baseline_probability)
        validation_candidate = metrics(validation, lambda x: challenger_probability(candidate, x))
        selected = (
            "logistic"
            if validation_candidate["brier_score"] < validation_baseline["brier_score"]
            else "momentum"
        )
        # Refit selected fixed logistic specification using the first seven days only.
        model = fit_challenger(train + validation) if selected == "logistic" else None
        predict = (lambda x: challenger_probability(model, x)) if model else baseline_probability
        fingerprint = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
        report = dict(
            status="WITHDRAWN_PRICE_TIMESTAMP_CONFLICT"
            if args.allow_unverified_display_history
            else "CAUSAL_DEPTH_SCENARIO",
            usable_for_profitability_claims=False,
            causal_entry_books=depth_coverage,
            start=iso(start),
            end_exclusive=iso(end),
            training_end=iso(start + 7 * 86400),
            test_start=iso(start + 7 * 86400),
            dataset_sha256=fingerprint,
            selected_model=selected,
            training_examples=len(train),
            validation_examples=len(validation),
            test_examples=len(test),
            excluded_feature_windows=excluded,
            entry_seconds_after_open=60,
            validation={"baseline": validation_baseline, "challenger": validation_candidate},
            test_direction_metrics=metrics(test, predict),
            model_selection="Lower validation Brier score, fixed hyperparameters; no test-based selection",
            limitations=[
                "Conditional scenario P&L, not verified executable returns or proof of profitability",
                "Historical display asks have no quantities; fill capacities are explicit assumptions",
                "One adverse tick and conservative 0.095 fee coefficient assumed throughout; actual historical fee tiers may differ",
                "Candle and quote publication latency is not reconstructed",
                "Some dates in the latest week were inspected in earlier research; this is not a pristine blind holdout",
                "Risk shutdowns persist for the rest of replay; no invented manual restart is applied",
            ],
            active_model_changed=False,
            live_trades=False,
            scenarios=[],
        )
        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=True)
        all_ledger = []
        for capacity in (0.0, 0.5, 1.0):
            result = replay(
                test, predict, capacity, use_depth=not args.allow_unverified_display_history
            )
            all_ledger.extend(result.pop("ledger"))
            report["scenarios"].append(result)
        # Cost sensitivity is scenario analysis, not another model-selection pass.
        result = replay(
            test,
            predict,
            1.0,
            theta=0.12,
            tick=0.02,
            label="HIGHER_COSTS_0.12_FEE_2_TICKS",
            use_depth=not args.allow_unverified_display_history,
        )
        all_ledger.extend(result.pop("ledger"))
        result["label"] = "HIGHER_COSTS_0.12_FEE_2_TICKS"
        report["scenarios"].append(result)
        for target in (1.2, 1.3, 1.4, 1.5, 1.6) if args.include_exit_research else ():
            label = f"SAMPLED_TAKE_PROFIT_{target}X"
            result = replay(test, predict, 1.0, exit_factor=target, label=label)
            all_ledger.extend(result.pop("ledger"))
            result["label"] = label
            result["target_factor"] = target
            report["scenarios"].append(result)
        for target in (1.3, 1.4, 1.5) if args.include_exit_research else ():
            label = f"SAMPLED_TP_{target}X_WITH_0.8X_STOP"
            result = replay(test, predict, 1.0, exit_factor=target, stop_factor=0.8, label=label)
            all_ledger.extend(result.pop("ledger"))
            result["label"] = label
            result["target_factor"] = target
            result["stop_factor"] = 0.8
            report["scenarios"].append(result)
        # Predeclared exploratory revision: do not use these test-week scores for promotion.
        strict = dict(
            min_edge=0.08, min_confidence=0.60, max_spread=0.03, min_price=0.15, max_price=0.75
        )
        for name, target, theta, tick in [
            ("EXPLORATORY_STRICT_ENTRY_HOLD", None, 0.095, 0.01),
            ("EXPLORATORY_STRICT_HIGHER_COSTS", None, 0.12, 0.02),
        ]:
            result = replay(
                test,
                predict,
                1.0,
                label=name,
                exit_factor=target,
                theta=theta,
                tick=tick,
                use_depth=not args.allow_unverified_display_history,
                **strict,
            )
            all_ledger.extend(result.pop("ledger"))
            result["label"] = name
            result["exploratory_after_test_inspection"] = True
            report["scenarios"].append(result)
        for s in report["scenarios"]:
            main = [a for a in s["accounts"] if a["id"].startswith("main")]
            s["combined_main_pnl"] = sum(a["realized"] for a in main)
            s["combined_main_trades"] = sum(a["trades"] for a in main)
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        if all_ledger:
            keys = sorted({k for row in all_ledger for k in row})
            with (output / "scenario-ledger.csv").open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=keys)
                writer.writeheader()
                writer.writerows(all_ledger)
        if model:
            (output / "model.json").write_text(
                json.dumps(dict(model, dataset_sha256=fingerprint, promoted=False), indent=2) + "\n"
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
    p.add_argument("--output", default="data/backtest14")
    p.add_argument(
        "--allow-unverified-display-history",
        action="store_true",
        help="AUDIT ONLY: reproduce withdrawn figures; never valid for profitability",
    )
    p.add_argument(
        "--include-exit-research",
        action="store_true",
        help="Reproduce archived, disabled take-profit comparisons only",
    )
    run(p.parse_args())
