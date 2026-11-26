"""Shadow by default; explicitly enabled selection among previously validated simulations."""

import json
import math
import time
from .models import Models, feature_sequence
from .execution import execute
from .providers import timestamp, validate_market, number


class Adaptive:
    def __init__(self, store, paper):
        self.store, self.paper = store, paper
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS approved_configs (
              id TEXT PRIMARY KEY, approved REAL, config TEXT, evidence TEXT)""")

    def enable(self, value, now=None):
        now = time.time() if now is None else now
        if value:
            boundary = self.store.get("v2_initial_freeze_end")
            if boundary is None or now < boundary:
                raise ValueError(
                    "Complete the initial seven-day experiment before enabling approved simulation selection"
                )
            with self.store.connect() as db:
                if not db.execute("SELECT 1 FROM approved_configs").fetchone():
                    raise ValueError(
                        "No independently validated and manually promoted configuration is approved"
                    )
        self.store.put("adaptive_enabled", bool(value))

    def tick(self, now):
        if not self.store.get("adaptive_enabled", False) or self.store.get("mode") != "SIMULATE":
            return
        if self.store.get("emergency", False) or self.paper.data_block(now):
            return
        boundary = self.store.get("v2_initial_freeze_end")
        if boundary is None or now < boundary:
            return
        market = self.store.get("market")
        book = self.store.get("book")
        if (
            not market
            or not validate_market(market, now)
            or not book
            or book["slug"] != market["slug"]
        ):
            return
        elapsed = now - timestamp(market["assetPriceTerms"]["windowStart"])
        if not 0 <= elapsed < 15 or self.store.get("adaptive_last_slug") == market["slug"]:
            return
        with self.store.connect() as db:
            if db.execute(
                "SELECT 1 FROM v2_orders WHERE account='primary' AND status='OPEN'"
            ).fetchone():
                return
            approved = [dict(r) for r in db.execute("SELECT * FROM approved_configs")]
        p, features = self.paper.probability(now)
        if p is None:
            return
        x = [
            features["momentum_60"],
            features["trend_300"],
            math.log(max(features["volatility"], 1e-7)),
        ]
        candidates = []
        models = Models(self.store)
        for row in approved:
            config = json.loads(row["config"])
            try:
                sequence = feature_sequence(
                    self.store, x, config["checkpoints"][0], market["slug"], now
                )
                probability = models.predict(config["model"], x, sequence)
                terms = market
                best = float("-inf")
                for side, prob in [("UP", probability), ("DOWN", 1 - probability)]:
                    fill = execute(
                        book,
                        side,
                        budget=config["stake"],
                        theta=max(0.095, number(terms.get("feeCoefficient", 0.095))),
                        tick=number(terms["orderPriceMinTickSize"], True),
                        quantum=number(terms["minimumTradeQty"], True),
                    )
                    if fill["quantity"]:
                        best = max(best, (prob * fill["quantity"] - fill["total"]) / fill["total"])
                candidates.append((best, row, probability))
            except (ValueError, ImportError, OSError, KeyError, TypeError):
                continue
        self.store.put("adaptive_last_slug", market["slug"])
        if not candidates:
            return
        edge, chosen, probability = max(candidates, key=lambda v: v[0])
        current = self.paper.summary()["strategy"]
        config = json.loads(chosen["config"])
        reason = f"Approved simulation selection: executable estimated edge {edge:.4f}; UP {probability:.4f}; volatility {features['volatility']:.6g}; no financial limits changed"
        action = "HOLD_CURRENT"
        if edge >= 0.03 and config.get("research_config") != json.loads(current["config"]).get(
            "research_config"
        ):
            from .promotion import promote

            try:
                promote(self.store, chosen["id"], reason, now=now)
                self.paper.mode("SIMULATE", now)
                action = "ACTIVATE_APPROVED_SIMULATION"
            except ValueError as exc:
                reason = "Kept current strategy: " + str(exc)
        with self.store.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO adaptive_selections VALUES (?,?,?,?,?)",
                (
                    market["slug"],
                    now,
                    config["model"],
                    action + ": " + reason,
                    "APPROVED SIMULATION",
                ),
            )
