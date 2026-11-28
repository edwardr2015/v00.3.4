"""Durable paper engine. All order and balance mutations use a SQLite write lock."""

import asyncio
import json
import math
import hashlib
import inspect
import time

from .execution import execute
from .providers import number, timestamp, validate_market
from .research import baseline_probability, features
from .risk import entry_block, settlement_block, utc_day

MODEL = "baseline-forward-v0.1"


def model_fingerprint():
    source = (
        inspect.getsource(PaperEngine.probability)
        + inspect.getsource(baseline_probability)
        + inspect.getsource(features)
    )
    return hashlib.sha256(source.encode()).hexdigest()


class PaperEngine:
    def __init__(self, store):
        self.store = store
        self.task = None
        self.smart = None
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                  id TEXT PRIMARY KEY, budget REAL NOT NULL, balance REAL NOT NULL DEFAULT 100,
                  realized REAL NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0,
                  halted TEXT, day TEXT NOT NULL DEFAULT '', day_pnl REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS predictions (
                  slug TEXT PRIMARY KEY, time REAL, model TEXT, up REAL, decision TEXT,
                  reason TEXT, checkpoint INTEGER, details TEXT);
                CREATE TABLE IF NOT EXISTS orders (
                  id INTEGER PRIMARY KEY, account TEXT, slug TEXT, side TEXT, time REAL,
                  model TEXT, probability REAL, quantity REAL, cost REAL, fee REAL,
                  status TEXT, result TEXT, pnl REAL, settled REAL, details TEXT,
                  UNIQUE(account,slug));
                CREATE TABLE IF NOT EXISTS skips (
                  id INTEGER PRIMARY KEY, account TEXT, slug TEXT, time REAL, reason TEXT,
                  UNIQUE(account,slug));
                CREATE TABLE IF NOT EXISTS risk_events (
                  id INTEGER PRIMARY KEY, time REAL, account TEXT, reason TEXT, reviewed REAL);
                CREATE TABLE IF NOT EXISTS equity (
                  id INTEGER PRIMARY KEY, time REAL, account TEXT, balance REAL, realized REAL);
            """)
            for group in ["main", "control"]:
                for budget in (1, 2, 3):
                    db.execute(
                        "INSERT OR IGNORE INTO accounts(id,budget) VALUES (?,?)",
                        (f"{group}-{budget}", budget),
                    )
        if store.get("mode") is None:
            store.put("mode", "OFF")

    def halt(self, reason, account=None):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if account:
                db.execute("UPDATE accounts SET halted=? WHERE id=?", (reason, account))
            else:
                db.execute("UPDATE accounts SET halted=? WHERE halted IS NULL", (reason,))
                self._state(db, "mode", "OFF")
            db.execute(
                "INSERT INTO risk_events(time,account,reason) VALUES (?,?,?)",
                (time.time(), account, reason),
            )

    @staticmethod
    def _state(db, key, value):
        db.execute(
            "INSERT OR REPLACE INTO state VALUES (?,?,?)",
            (key, json.dumps(value, allow_nan=False), time.time()),
        )

    def mode(self, value, now=None):
        if value not in ("OFF", "LEARN", "SIMULATE"):
            raise ValueError("LIVE is permanently locked in v0.1")
        now = time.time() if now is None else now
        if value == "SIMULATE":
            if self.store.get("emergency", False):
                raise ValueError("Clear emergency stop after manual review")
            with self.store.connect() as db:
                if db.execute("SELECT count(*) FROM accounts WHERE halted IS NOT NULL").fetchone()[
                    0
                ]:
                    raise ValueError("Review all risk shutdowns before restarting")
            reason = self.data_block(now)
            if reason:
                raise ValueError("Cannot start simulation: " + reason)
            experiment = self.store.get("experiment")
            if experiment and experiment.get("model_hash") != model_fingerprint():
                raise ValueError("Active model changed; the frozen experiment cannot resume")
            if experiment and now >= experiment["end"]:
                raise ValueError("Seven-day experiment has ended; export and review it")
            if not experiment:
                self.store.put(
                    "experiment",
                    dict(
                        start=now,
                        end=now + 7 * 86400,
                        model=MODEL,
                        model_hash=model_fingerprint(),
                        fee_theta=0.095,
                        slippage_ticks=1,
                        participation=0.25,
                        status="RUNNING",
                        hourly_checkpoint_minute=0,
                    ),
                )
        self.store.put("mode", value)
        self.store.event("INFO", "Decision mode: " + value)

    def review(self):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT * FROM accounts WHERE halted IS NOT NULL").fetchall():
                account = dict(row)
                # Permanent experiment loss limits cannot be reset by clicking review.
                if account["realized"] <= -20:
                    continue
                if account["day"] == utc_day(time.time()) and account["day_pnl"] <= -10:
                    continue
                db.execute("UPDATE accounts SET halted=NULL,streak=0 WHERE id=?", (account["id"],))
            db.execute("UPDATE risk_events SET reviewed=? WHERE reviewed IS NULL", (time.time(),))

    def data_block(self, now):
        for name in ("reference", "book"):
            value = self.store.get(name)
            if not value:
                return "WAITING_FOR_" + name.upper()
            age = now - value["timestamp"]
            if not -5 <= age <= 15:
                return "STALE_" + name.upper()
        for provider in ("reference", "market"):
            h = self.store.get("health_" + provider, {})
            if not h.get("ok") or now - h.get("checked", 0) > 20:
                return "PROVIDER_ERROR_" + provider.upper()
        return None

    def probability(self, now, *, book=None):
        bars = self.store.candles(15, 40, now=now)
        decision_time = int(now) // 15 * 15
        data = {b["time"]: dict(close=b["close"], ticks=15 if b["complete"] else 0) for b in bars}
        x = features(data, decision_time)
        if x is None:
            return None, dict(reason="INCOMPLETE_FEATURE_WINDOW")
        p = baseline_probability(x)
        book = (self.store.get("book") or {}) if book is None else book
        bid_depth = sum(r["quantity"] for r in book.get("bids", [])[:5])
        ask_depth = sum(r["quantity"] for r in book.get("asks", [])[:5])
        imbalance = (
            (bid_depth - ask_depth) / (bid_depth + ask_depth) if bid_depth + ask_depth else 0
        )
        closed = [b for b in bars if b["complete"] and b["time"] + 15 <= now]
        pattern = (closed[-1]["close"] - closed[-1]["open"]) / max(
            closed[-1]["high"] - closed[-1]["low"], 0.01
        )
        p = max(0.05, min(0.95, p + 0.01 * imbalance + 0.005 * pattern))
        return p, dict(
            momentum_60=x[0],
            trend_300=x[1],
            volatility=math.exp(x[2]),
            order_book_imbalance=imbalance,
            candle_body=pattern,
            feature_end=decision_time,
        )

    def tick(self, now=None):
        now = time.time() if now is None else now
        self.settle(now)
        mode = self.store.get("mode", "OFF")
        experiment = self.store.get("experiment")
        if experiment and now >= experiment["end"] and experiment["status"] == "RUNNING":
            experiment["status"] = "COMPLETED"
            self.store.put("experiment", experiment)
            self.store.put("mode", "OFF")
            mode = "OFF"
        if mode not in ("SIMULATE", "LEARN"):
            return
        if (
            mode == "SIMULATE"
            and experiment
            and experiment.get("model_hash") != model_fingerprint()
        ):
            self.halt("ACTIVE_MODEL_CHANGED")
            return
        if self.store.get("emergency", False):
            if mode == "SIMULATE":
                self.halt("EMERGENCY_STOP")
            return
        block = self.data_block(now)
        if block:
            if mode == "SIMULATE":
                self.halt(block)
            return
        m = self.store.get("market")
        book = self.store.get("book")
        if not m or not validate_market(m, now) or book["slug"] != m["slug"]:
            return
        if book.get("state") != "MARKET_STATE_OPEN":
            return
        elapsed = now - timestamp(m["assetPriceTerms"]["windowStart"])
        if not 60 <= elapsed <= 120:
            return
        with self.store.connect() as db:
            if db.execute("SELECT 1 FROM predictions WHERE slug=?", (m["slug"],)).fetchone():
                return
        p, details = self.probability(now)
        checkpoint = int(timestamp(m["assetPriceTerms"]["windowStart"])) % 3600 == 0
        reason = "INCOMPLETE_FEATURE_WINDOW" if p is None else "NO_POSITIVE_EXPECTED_VALUE"
        side = "SKIP"
        plans = {}
        if p is not None:
            try:
                tick = number(m["orderPriceMinTickSize"], True)
                quantum = number(m["minimumTradeQty"], True)
                theta = max(0.095, number(m.get("feeCoefficient", 0.095), True))
                alternatives = []
                for direction, prob in [("UP", p), ("DOWN", 1 - p)]:
                    fill = execute(
                        book, direction, budget=1, theta=theta, tick=tick, quantum=quantum
                    )
                    edge = prob * fill["quantity"] - fill["total"]
                    alternatives.append((edge, direction))
                edge, direction = max(alternatives)
                if edge >= 0.03:
                    side, reason = direction, "POSITIVE_EXPECTED_VALUE"
                    for budget in (1, 2, 3):
                        plans[budget] = execute(
                            book, side, budget=budget, theta=theta, tick=tick, quantum=quantum
                        )
                details.update(
                    theta=theta,
                    tick=tick,
                    quantum=quantum,
                    elapsed=elapsed,
                    reference_price=self.store.get("reference")["price"],
                    book_timestamp=book["timestamp"],
                    estimated_edge_per_dollar=edge,
                    window_end=timestamp(m["assetPriceTerms"]["windowEnd"]),
                )
            except (KeyError, ValueError, TypeError) as exc:
                reason = "INVALID_EXECUTION_TERMS"
                details["error"] = str(exc)
        # One transaction freezes the prediction and applies the independent controller.
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Mode/stop can change while computing: re-read inside the write lock.
            row = db.execute("SELECT payload FROM state WHERE key='mode'").fetchone()
            if not row or json.loads(row[0]) != mode:
                return
            if db.execute("SELECT 1 FROM predictions WHERE slug=?", (m["slug"],)).fetchone():
                return
            emergency = db.execute("SELECT payload FROM state WHERE key='emergency'").fetchone()
            if emergency and json.loads(emergency[0]):
                return
            db.execute(
                "INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?)",
                (m["slug"], now, MODEL, p, side, reason, checkpoint, json.dumps(details)),
            )
            for account in db.execute("SELECT * FROM accounts").fetchall():
                account = dict(account)
                why = reason if side == "SKIP" else None
                if mode != "SIMULATE":
                    why = "LEARN_SHADOW_ONLY"
                elif checkpoint and account["id"].startswith("main"):
                    why = "HOURLY_OBSERVATION_CHECKPOINT"
                if not why:
                    exposure = db.execute(
                        "SELECT coalesce(sum(cost),0) FROM orders WHERE account=? AND status='OPEN'",
                        (account["id"],),
                    ).fetchone()[0]
                    why = entry_block(account, exposure, account["budget"], now)
                    if why and why not in ("ONE_POSITION_LIMIT", "MANUAL_REVIEW_REQUIRED"):
                        db.execute("UPDATE accounts SET halted=? WHERE id=?", (why, account["id"]))
                        db.execute(
                            "INSERT INTO risk_events(time,account,reason) VALUES (?,?,?)",
                            (now, account["id"], why),
                        )
                fill = plans.get(account["budget"])
                if not why and (not fill or not fill["quantity"]):
                    why = "UNFILLED_NO_LIQUIDITY"
                if (
                    not why
                    and (p if side == "UP" else 1 - p) * fill["quantity"] - fill["total"]
                    < 0.03 * fill["total"]
                ):
                    why = "INSUFFICIENT_EDGE_AFTER_DEPTH"
                if why:
                    db.execute(
                        "INSERT OR IGNORE INTO skips(account,slug,time,reason) VALUES (?,?,?,?)",
                        (account["id"], m["slug"], now, why),
                    )
                    continue
                cursor = db.execute(
                    "INSERT OR IGNORE INTO orders(account,slug,side,time,model,probability,quantity,cost,fee,status,details) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        account["id"],
                        m["slug"],
                        side,
                        now,
                        MODEL,
                        p,
                        fill["quantity"],
                        fill["total"],
                        fill["fee"],
                        "OPEN",
                        json.dumps(dict(fill=fill, prediction=details, book_snapshot=book)),
                    ),
                )
                if cursor.rowcount:
                    db.execute(
                        "UPDATE accounts SET balance=balance-? WHERE id=?",
                        (fill["total"], account["id"]),
                    )

    def settle(self, now):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for order in db.execute("SELECT * FROM orders WHERE status='OPEN'").fetchall():
                result = db.execute(
                    "SELECT payload FROM state WHERE key=?", ("outcome_" + order["slug"],)
                ).fetchone()
                if not result:
                    continue
                outcome = json.loads(result[0])
                order_details = json.loads(order["details"])
                if now < order_details["prediction"]["window_end"]:
                    continue
                if outcome.get("slug") != order["slug"]:
                    raise ValueError("Settlement market mismatch")
                if outcome.get("yes_payout") not in (0, 1):
                    raise ValueError("Invalid settlement payout")
                win = order["side"] == outcome["outcome"]
                payout = order["quantity"] if win else 0
                pnl = payout - order["cost"]
                a = dict(
                    db.execute("SELECT * FROM accounts WHERE id=?", (order["account"],)).fetchone()
                )
                day_pnl = (a["day_pnl"] if a["day"] == utc_day(now) else 0) + pnl
                streak = a["streak"] + 1 if pnl < 0 else 0
                db.execute(
                    "UPDATE orders SET status='SETTLED',result=?,pnl=?,settled=? WHERE id=?",
                    (outcome["outcome"], pnl, now, order["id"]),
                )
                db.execute(
                    "UPDATE accounts SET balance=balance+?,realized=realized+?,streak=?,day=?,day_pnl=? WHERE id=?",
                    (payout, pnl, streak, utc_day(now), day_pnl, a["id"]),
                )
                a.update(realized=a["realized"] + pnl, streak=streak, day_pnl=day_pnl)
                why = settlement_block(a)
                if why:
                    db.execute("UPDATE accounts SET halted=? WHERE id=?", (why, a["id"]))
                    db.execute(
                        "INSERT INTO risk_events(time,account,reason) VALUES (?,?,?)",
                        (now, a["id"], why),
                    )
                db.execute(
                    "INSERT INTO equity(time,account,balance,realized) VALUES (?,?,?,?)",
                    (now, a["id"], a["balance"] + payout, a["realized"]),
                )

    def summary(self):
        with self.store.connect() as db:
            accounts = [dict(r) for r in db.execute("SELECT * FROM accounts ORDER BY id")]
            for a in accounts:
                counts = db.execute(
                    "SELECT count(*),coalesce(sum(pnl>0),0) FROM orders WHERE account=? AND status='SETTLED'",
                    (a["id"],),
                ).fetchone()
                exposure = db.execute(
                    "SELECT coalesce(sum(cost),0) FROM orders WHERE account=? AND status='OPEN'",
                    (a["id"],),
                ).fetchone()[0]
                a.update(
                    trades=counts[0],
                    wins=counts[1],
                    win_rate=counts[1] / counts[0] if counts[0] else None,
                    exposure=exposure,
                    equity_at_cost=a["balance"] + exposure,
                )
            prediction = db.execute(
                "SELECT * FROM predictions ORDER BY time DESC LIMIT 1"
            ).fetchone()
            orders = [
                dict(r) for r in db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 200")
            ]
            risks = [
                dict(r) for r in db.execute("SELECT * FROM risk_events ORDER BY id DESC LIMIT 30")
            ]
            skips = [dict(r) for r in db.execute("SELECT * FROM skips ORDER BY id DESC LIMIT 30")]
        return dict(
            accounts=accounts,
            prediction=dict(prediction) if prediction else None,
            orders=orders,
            risk_events=risks,
            experiment=self.store.get("experiment"),
            model=MODEL,
            skips=skips,
        )

    async def start(self):
        # Restart never silently resumes paper entries after an unobserved outage.
        if self.store.get("mode") == "SIMULATE":
            self.halt("SERVICE_RESTART_REVIEW")
        self.task = asyncio.create_task(self.loop())

    async def loop(self):
        while True:
            try:
                self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.halt("CRITICAL_SYSTEM_ERROR")
                self.store.event("ERROR", "Paper engine stopped: " + str(exc))
            if self.smart:
                # 2.3: research runs on its own. An error in a research model is logged and skipped;
                # it can never halt the paper engine (and with it the live trader) again.
                try:
                    self.smart.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.store.event("WARNING", "Research step skipped (trading unaffected): " + str(exc)[:200])
            self.store.put("next_evaluation",time.time()+2)
            await asyncio.sleep(2)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
