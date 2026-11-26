"""Smart Exit/Flip are counterfactual shadow positions, isolated from account cash."""

import json
import time

from .execution import execute


class SmartResearch:
    def __init__(self, store, paper, version=1):
        self.store = store
        self.paper = paper
        if version not in (1, 2):
            raise ValueError("Unknown research ledger version")
        self.version = version
        self.orders = "v2_orders" if version == 2 else "orders"
        self.experiments = "v2_smart_experiments" if version == 2 else "smart_experiments"
        self.actions = "v2_smart_actions" if version == 2 else "smart_actions"
        self.account_filter = (
            "(account='primary' OR account LIKE 'research-%')"
            if version == 2
            else "account LIKE 'main-%'"
        )
        with store.connect() as db:
            db.executescript(f"""
              CREATE TABLE IF NOT EXISTS {self.experiments} (
                id INTEGER PRIMARY KEY, order_id INTEGER, kind TEXT, state TEXT,
                UNIQUE(order_id,kind));
              CREATE TABLE IF NOT EXISTS {self.actions} (
                id INTEGER PRIMARY KEY, experiment INTEGER, time REAL, action TEXT, details TEXT);
            """)

    def tick(self, now=None):
        now = time.time() if now is None else now
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for o in db.execute(
                f"SELECT * FROM {self.orders} WHERE {self.account_filter}"
            ).fetchall():
                kinds = ["HOLD", "FLIP"]
                if self.store.get("smart_exit_enabled", False):
                    kinds += ["EXIT_1.2", "EXIT_1.3", "EXIT_1.4", "EXIT_1.5", "EXIT_1.6"]
                for kind in kinds:
                    factor = float(kind.split("_")[1]) if kind.startswith("EXIT") else None
                    state = dict(
                        side=o["side"],
                        quantity=o["quantity"],
                        initial_quantity=o["quantity"],
                        cost=o["cost"],
                        cash=0.0,
                        fees=o["fee"],
                        flips=0,
                        status="OPEN",
                        target_factor=factor,
                        target_impossible=bool(factor and factor * o["cost"] > o["quantity"]),
                        last_book=None,
                        source="COUNTERFACTUAL_FROM_PAPER_ENTRY",
                        pnl=None,
                    )
                    db.execute(
                        f"INSERT OR IGNORE INTO {self.experiments}(order_id,kind,state) VALUES (?,?,?)",
                        (o["id"], kind, json.dumps(state)),
                    )
            experiments = [dict(r) for r in db.execute(f"SELECT * FROM {self.experiments}")]
        book = self.store.get("book")
        mode = self.store.get("mode")
        p, _ = self.paper.probability(now) if mode in ("LEARN", "SIMULATE") else (None, {})
        for experiment in experiments:
            state = json.loads(experiment["state"])
            if state["status"] != "OPEN":
                continue
            with self.store.connect() as db:
                o = db.execute(
                    f"SELECT * FROM {self.orders} WHERE id=?", (experiment["order_id"],)
                ).fetchone()
            result = self.store.get("outcome_" + o["slug"])
            if self.version == 2:
                with self.store.connect() as db:
                    verified = db.execute(
                        "SELECT * FROM verified_markets WHERE slug=?", (o["slug"],)
                    ).fetchone()
                result = dict(verified) if verified else None
            if result and now >= json.loads(o["details"])["prediction"]["window_end"]:
                payout = state["quantity"] if state["side"] == result["outcome"] else 0
                state.update(
                    status="SETTLED",
                    cash=state["cash"] + payout,
                    pnl=state["cash"] + payout - state["cost"],
                    settled=now,
                )
                self.save(experiment["id"], state, now, "SETTLE", dict(outcome=result["outcome"]))
                continue
            if now >= json.loads(o["details"])["prediction"]["window_end"]:
                continue
            if experiment["kind"].startswith("EXIT") and not self.store.get(
                "smart_exit_enabled", False
            ):
                continue
            if self.version == 2 and (
                mode != "SIMULATE"
                or self.store.get("emergency", False)
                or self.paper.data_block(now)
            ):
                continue
            if (
                mode not in ("LEARN", "SIMULATE")
                or not book
                or book["slug"] != o["slug"]
                or not -5 <= now - book["timestamp"] <= 15
            ):
                continue
            if state["last_book"] == book["timestamp"] or experiment["kind"] == "HOLD":
                continue
            state["last_book"] = book["timestamp"]
            d = json.loads(o["details"])["prediction"]
            kwargs = dict(theta=d["theta"], tick=d["tick"], quantum=d["quantum"])
            sale = execute(book, state["side"], quantity=state["quantity"], buying=False, **kwargs)
            if not sale["quantity"]:
                self.save(experiment["id"], state)
                continue
            action = "HOLD"
            detail = {}
            if experiment["kind"].startswith("EXIT"):
                target = (
                    state["cost"]
                    / state["initial_quantity"]
                    * sale["quantity"]
                    * state["target_factor"]
                )
                if not state["target_impossible"] and sale["total"] >= target:
                    action = "EXIT"
            elif experiment["kind"] == "FLIP" and p is not None:
                prob = p if state["side"] == "UP" else 1 - p
                hold_ev = state["quantity"] * prob
                exit_ev = sale["total"] + (state["quantity"] - sale["quantity"]) * prob
                other = "DOWN" if state["side"] == "UP" else "UP"
                purchase = execute(book, other, budget=max(0, sale["total"]), **kwargs)
                flip_ev = purchase["quantity"] * (1 - prob) + (sale["total"] - purchase["total"])
                detail = dict(
                    hold_ev=hold_ev,
                    exit_ev=exit_ev,
                    flip_ev=flip_ev,
                    quantity_available=sale["quantity"],
                    up_probability=p,
                )
                full_sale = sale["quantity"] >= state["quantity"] - d["quantum"] / 2
                if (
                    full_sale
                    and not state["flips"]
                    and purchase["quantity"]
                    and flip_ev > max(hold_ev, exit_ev) + 0.02
                ):
                    state.update(
                        side=other,
                        quantity=purchase["quantity"],
                        cash=state["cash"] + sale["total"] - purchase["total"],
                        fees=state["fees"] + sale["fee"] + purchase["fee"],
                        flips=1,
                    )
                    action = "FLIP"
                elif exit_ev > hold_ev + 0.02:
                    action = "EXIT"
            if action == "EXIT":
                state.update(
                    quantity=max(0, state["quantity"] - sale["quantity"]),
                    cash=state["cash"] + sale["total"],
                    fees=state["fees"] + sale["fee"],
                )
                if state["quantity"] < d["quantum"] / 2:
                    state.update(status="EXITED", pnl=state["cash"] - state["cost"])
            self.save(experiment["id"], state, now, action, detail)

    def save(self, id, state, now=None, action=None, details=None):
        with self.store.connect() as db:
            db.execute(f"UPDATE {self.experiments} SET state=? WHERE id=?", (json.dumps(state), id))
            if action:
                db.execute(
                    f"INSERT INTO {self.actions}(experiment,time,action,details) VALUES (?,?,?,?)",
                    (id, now, action, json.dumps(details or {})),
                )
