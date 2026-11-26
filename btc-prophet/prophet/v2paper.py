"""v0.2 primary and research ledgers. Legacy tables are preserved verbatim."""

import asyncio
import hashlib
import inspect
import json
import math
import time

from .paper import PaperEngine as LegacyEngine
from .paper import model_fingerprint as legacy_fingerprint
from .providers import timestamp, validate_market, number
from .v2risk import CHECKPOINTS, daily_loss, local_day
from . import adaptive_entry, analytics, autonomous, hold_research, intervention, pattern_odds, unified

MODEL = "momentum-book-v0.2"


def fingerprint():
    from .execution import execute, paper_buy
    from .lab import EXITS, Lab
    from .v2risk import entry_block

    return hashlib.sha256(
        (
            legacy_fingerprint()
            + inspect.getsource(Engine.tick)
            + inspect.getsource(Engine.probability)
            + inspect.getsource(adaptive_entry)
            + inspect.getsource(autonomous)
            + inspect.getsource(hold_research)
            + inspect.getsource(pattern_odds)
            + inspect.getsource(unified)
            + inspect.getsource(Engine.settle)
            + inspect.getsource(Lab.tick)
            + inspect.getsource(Lab.enter_timing)
            + inspect.getsource(Lab.exit_and_settle)
            + repr(EXITS)
            + inspect.getsource(execute)
            + inspect.getsource(paper_buy)
            + inspect.getsource(entry_block)
        ).encode()
    ).hexdigest()


class Engine(LegacyEngine):
    def __init__(self, store):
        super().__init__(store)  # Initializes, but never executes against, legacy ledgers.
        self.unified_enabled = False
        analytics.initialize(store)
        intervention.initialize(store)
        with store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS v2_accounts (
              id TEXT PRIMARY KEY, budget REAL NOT NULL CHECK(budget>0 AND budget<=3),
              balance REAL NOT NULL DEFAULT 100 CHECK(balance>=-0.00000001),
              realized REAL NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0, halted TEXT);
            CREATE TABLE IF NOT EXISTS v2_decisions (
              slug TEXT, checkpoint INTEGER, time REAL, model TEXT, up REAL,
              decision TEXT, reason TEXT, details TEXT, PRIMARY KEY(slug,checkpoint));
            CREATE TABLE IF NOT EXISTS v2_orders (
              id INTEGER PRIMARY KEY, account TEXT, slug TEXT, side TEXT CHECK(side IN ('UP','DOWN')),
              time REAL, model TEXT, probability REAL, quantity REAL CHECK(quantity>0),
              cost REAL CHECK(cost>0 AND cost<=3.00000001), fee REAL, status TEXT,
              result TEXT, pnl REAL, settled REAL, details TEXT, segment INTEGER,
              UNIQUE(account,slug));
            CREATE TABLE IF NOT EXISTS v2_skips (
              id INTEGER PRIMARY KEY, account TEXT, slug TEXT, checkpoint INTEGER,
              time REAL, reason TEXT, UNIQUE(account,slug,checkpoint));
            CREATE TABLE IF NOT EXISTS v2_equity (
              id INTEGER PRIMARY KEY, time REAL, account TEXT, balance REAL, realized REAL);
            CREATE TABLE IF NOT EXISTS strategy_changes (
              id INTEGER PRIMARY KEY, time REAL, action TEXT, previous TEXT,
              model TEXT, reason TEXT, config TEXT, git_commit TEXT);
            CREATE TABLE IF NOT EXISTS timing_checkpoint_books (
              slug TEXT, checkpoint INTEGER, payload TEXT, PRIMARY KEY(slug,checkpoint));
            CREATE TABLE IF NOT EXISTS v2_hourly (
              hour INTEGER PRIMARY KEY, time REAL, slug TEXT, up REAL, details TEXT);
            """)
            db.execute("PRAGMA user_version=2")
            for account, stake in [
                ("primary", 3),
                ("research-1", 1),
                ("research-2", 2),
                ("research-3", 3),
                ("hourly-control", 3),
                ("hourly-skip", 3),
            ]:
                db.execute(
                    "INSERT OR IGNORE INTO v2_accounts(id,budget) VALUES (?,?)", (account, stake)
                )
            if not db.execute("SELECT 1 FROM strategy_changes").fetchone():
                import subprocess

                try:
                    commit = subprocess.check_output(
                        ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
                    ).strip()
                except (OSError, subprocess.SubprocessError):
                    commit = "NOT AVAILABLE"
                db.execute(
                    "INSERT INTO strategy_changes(time,action,model,reason,config,git_commit) VALUES (?,?,?,?,?,?)",
                    (
                        time.time(),
                        "ACTIVATE",
                        MODEL,
                        "Initial fixed baseline; profitability unvalidated",
                        json.dumps(
                            dict(checkpoints=CHECKPOINTS, exit="HOLD", stake=3, model=MODEL)
                        ),
                        commit,
                    ),
                )
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM state WHERE key='timing_protocol_v1'").fetchone():
                db.execute("""INSERT OR IGNORE INTO state(key,payload,received)
                    SELECT 'pre_timing_experiment',payload,received FROM state WHERE key='v2_experiment'""")
                db.execute("DELETE FROM state WHERE key='v2_experiment'")
                self._state(db, "mode", "OFF")
                self._state(db, "adaptive_enabled", False)
                self._state(db, "v2_initialized", True)
                self._state(db, "timing_protocol_v1", True)
                for pattern in ("STALE_%", "WAITING_FOR_%", "PROVIDER_ERROR_%", "FUTURE_%"):
                    db.execute("UPDATE v2_accounts SET halted=NULL WHERE halted LIKE ?", (pattern,))

        with store.connect() as db:
            unified.initialize(db)
        self.continuous = adaptive_entry.AdaptiveEntry(self)
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not adaptive_entry.get_state(db,"unified_research_v1"):
                old = adaptive_entry.get_state(db,"v2_experiment")
                if old and old.get("model_hash") == "198d136604c366b52781a4f6ca5be526d0683ecc44e0bdbce825643462172544":
                    self._state(db,"pre_unified_experiment",old)
                    db.execute("DELETE FROM state WHERE key='v2_experiment'")
                    self._state(db,"mode","OFF")
                self._state(db,"unified_research_v1",True)

        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not adaptive_entry.get_state(db, "autonomous_protocol_v1"):
                old = adaptive_entry.get_state(db, "v2_experiment")
                if old and old.get("model_hash") == "dc7dc1148150c68cc4ee568cc389d66fb83413a09b6203c49809f25ce1458c1d":
                    self._state(db, "pre_autonomous_experiment", old)
                    db.execute("DELETE FROM state WHERE key='v2_experiment'")
                    self._state(db, "mode", "OFF")
                self._state(db, "autonomous_protocol_v1", True)
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not adaptive_entry.get_state(db, "hold_flip_research_v1"):
                old = adaptive_entry.get_state(db,"v2_experiment")
                if old and old.get("model_hash") == "98918d2d57dc23ebeed31aa0879a418089d970785b6f63fcde6ba9894a5b818a":
                    self._state(db,"pre_hold_flip_experiment",old)
                    db.execute("DELETE FROM state WHERE key='v2_experiment'")
                    self._state(db,"mode","OFF")
                self._state(db,"hold_flip_research_v1",True)
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM state WHERE key='continuous_entry_v1'").fetchone():
                db.execute("""INSERT OR IGNORE INTO state(key,payload,received)
                    SELECT 'pre_continuous_experiment',payload,received FROM state WHERE key='v2_experiment'""")
                db.execute("DELETE FROM state WHERE key='v2_experiment'")
                self._state(db, "mode", "OFF")
                self._state(db, "continuous_entry_v1", True)

        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not adaptive_entry.get_state(db, "competition_repair_v1"):
                old = adaptive_entry.get_state(db, "v2_experiment")
                if old and not old.get("preflight"):
                    self._state(db, "pre_competition_repair_experiment", old)
                    if old.get("start") is not None and adaptive_entry.get_state(db, "paper_pilot_started") is None:
                        self._state(db, "paper_pilot_started", old["start"])
                    db.execute("DELETE FROM state WHERE key='v2_experiment'")
                    self._state(db, "mode", "OFF")
                    self._state(db, "paused_mode", None)
                self._state(db, "competition_repair_v1", True)

    def recover_startup(self, now=None):
        """Recover routine restarts after acquiring the process lock."""
        now = time.time() if now is None else now
        self.unified_enabled = True
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            first = db.execute("INSERT OR IGNORE INTO unified_account VALUES (1,?,?)",
                               (now,unified.STARTING_CASH)).rowcount
            self._state(db, "collection_paused", False)
            db.execute("DELETE FROM state WHERE key IN ('health_market','health_reference','health_spot')")
            archived = db.execute("UPDATE v2_accounts SET halted=NULL WHERE halted='SERVICE_RESTART_REVIEW'").rowcount
            db.execute("""UPDATE risk_events SET reviewed=? WHERE reason='SERVICE_RESTART_REVIEW'
                AND reviewed IS NULL""", (now,))
            db.execute("""UPDATE intervention_requests SET status='RECOVERED',decided=?,
                note='Routine restart recovered; fresh quotes are required before entries.'
                WHERE issue='SERVICE_RESTART_REVIEW' AND status='PENDING'""", (now,))
            mode = adaptive_entry.get_state(db, "mode") or "OFF"
            experiment = adaptive_entry.get_state(db, "v2_experiment")
            blocked = (adaptive_entry.get_state(db, "emergency")
                       or db.execute("SELECT 1 FROM v2_accounts WHERE halted IS NOT NULL").fetchone()
                       or (experiment and experiment["model_hash"] != fingerprint()))
            if not blocked and not experiment and (first or archived) and mode == "OFF":
                self._state(db, "mode", "SIMULATE")
                self._state(db, "paused_mode", None)
                if adaptive_entry.get_state(db, "paper_pilot_started") is None:
                    self._state(db, "paper_pilot_started", now)
            if blocked and mode == "SIMULATE":
                self._state(db, "mode", "OFF")

    async def start(self):
        if not self.unified_enabled:
            return await super().start()
        self.task = asyncio.create_task(self.loop())

    def halt(self, reason, account=None):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if account:
                db.execute("UPDATE v2_accounts SET halted=? WHERE id=?", (reason, account))
            else:
                db.execute("UPDATE v2_accounts SET halted=? WHERE halted IS NULL", (reason,))
                self._state(db, "mode", "OFF")
            prior = db.execute(
                "SELECT 1 FROM risk_events WHERE reason=? AND account IS ? AND reviewed IS NULL",
                (reason, account),
            ).fetchone()
            if not prior:
                intervention.ask(db, reason, account)
                db.execute(
                    "INSERT INTO risk_events(time,account,reason) VALUES (?,?,?)",
                    (time.time(), account, reason),
                )

    def review(self):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE v2_accounts SET halted=NULL")
            db.execute("UPDATE risk_events SET reviewed=? WHERE reviewed IS NULL", (time.time(),))
        # No balance, streak, exposure or daily-loss history is reset.

    def mode(self, value, now=None, *, formal=False):
        if value not in ("OFF", "LEARN", "SIMULATE"):
            raise ValueError("LIVE is permanently locked")
        now = time.time() if now is None else now
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if value == "SIMULATE":
                if adaptive_entry.get_state(db, "emergency"):
                    raise ValueError("Clear emergency stop after manual review")
                if db.execute("SELECT 1 FROM v2_accounts WHERE halted IS NOT NULL").fetchone():
                    raise ValueError("Review critical shutdowns before restarting")
                experiment = adaptive_entry.get_state(db, "v2_experiment")
                if experiment and now >= experiment["end"]:
                    if formal:
                        self._state(db, f"archived_formal_experiment_{experiment['start']}", experiment)
                        experiment = None
                    else:
                        raise ValueError("Seven-day comparison ended; review before starting a new segment")
                code = fingerprint() if formal or experiment else None
                if experiment and experiment["model_hash"] != code:
                    raise ValueError("Frozen experiment code changed; export and review")
                if not formal and experiment is None and adaptive_entry.get_state(db, "paper_pilot_started") is None:
                    self._state(db, "paper_pilot_started", now)
                if formal and experiment is None:
                    from .competition import preflight

                    ready = preflight(db, now)
                    if not ready["ready"]:
                        raise ValueError("Formal comparison blocked: " + "; ".join(ready["reasons"]))
                    self._state(db, "v2_experiment", dict(
                        start=now, end=now+7*86400, status="RUNNING",
                        model_hash=code, preflight=ready,
                        model=adaptive_entry.get_state(db, "active_model") or MODEL,
                        policy=("nine fixed shadow participants; separate unified adaptive paper account"
                                if self.unified_enabled else "nine fixed participants; adaptive hold with unfunded flips"),
                        timezone="UTC", stake_sizes=[1, 2, 3], daily_limit=None,
                    ))
                    self._state(db, "v2_initial_freeze_end", now+7*86400)
            self._state(db, "mode", value)
        self.store.event("INFO", "Paper research mode: " + value)

    def _skip(self, db, account, slug, checkpoint, now, reason):
        db.execute(
            "INSERT OR IGNORE INTO v2_skips(account,slug,checkpoint,time,reason) VALUES (?,?,?,?,?)",
            (account, slug, checkpoint, now, reason),
        )

    def data_block(self, now):
        with self.store.connect() as db:
            return adaptive_entry.freshness(adaptive_entry.snapshot(db), now)

    def probability(self, now, *, book=None):
        p, details = super().probability(now, book=book)
        with self.store.connect() as db:
            active = db.execute(
                "SELECT * FROM strategy_changes ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if active["model"] != MODEL and p is not None:
            from .models import Models

            x = [
                details["momentum_60"],
                details["trend_300"],
                math.log(max(details["volatility"], 1e-7)),
            ]
            from .models import feature_sequence

            config = json.loads(active["config"])
            sequence = feature_sequence(
                self.store,
                x,
                config["checkpoints"][0],
                (self.store.get("market") or {}).get("slug", ""),
                now,
            )
            p = Models(self.store).predict(active["model"], x, sequence)
        return p, details

    def tick(self, now=None):
        live_clock = now is None
        now = time.time() if live_clock else now
        self.settle(now)
        self.continuous.finalize(now)
        self.continuous.compare_later(time.time() if live_clock else now)
        mode = self.store.get("mode", "OFF")
        experiment = self.store.get("v2_experiment")
        if experiment and now >= experiment["end"]:
            if experiment["status"] == "RUNNING":
                experiment["status"] = "ENDED_REVIEW_REQUIRED"
                self.store.put("v2_experiment", experiment)
                if mode == "SIMULATE":
                    self.store.put("mode", "OFF")
            if mode == "SIMULATE":
                return
        if mode not in ("LEARN", "SIMULATE"):
            return
        if self.store.get("emergency", False):
            self.halt("EMERGENCY_STOP")
            return
        if mode == "SIMULATE" and experiment and experiment["model_hash"] != fingerprint():
            self.halt("ACTIVE_MODEL_CHANGED")
            return
        block = self.data_block(now)
        market, book = self.store.get("market"), self.store.get("book")
        if not market or not validate_market(market, now):
            return
        if not block and (book["slug"] != market["slug"] or book.get("state") != "MARKET_STATE_OPEN"):
            block = "WAITING_FOR_OPEN_BOOK"
        start = timestamp(market["assetPriceTerms"]["windowStart"])
        end = timestamp(market["assetPriceTerms"]["windowEnd"])
        elapsed = now - start
        for checkpoint in CHECKPOINTS:
            if elapsed < checkpoint or (block and elapsed-checkpoint <= 3):
                continue
            with self.store.connect() as db:
                if db.execute(
                    "SELECT 1 FROM v2_decisions WHERE slug=? AND checkpoint=?",
                    (market["slug"], checkpoint),
                ).fetchone():
                    continue
            if elapsed - checkpoint > 3:
                p, details = None, {"reason": block or "MISSED_CHECKPOINT"}
            else:
                p, details = LegacyEngine.probability(self, now)
                if p is not None and not math.isfinite(p):
                    raise ValueError("Invalid probability")
            reason = details.get("reason") or ("PREDICTED_SIDE" if p is not None else "INSUFFICIENT_DATA")
            side = "SKIP" if p is None else "UP" if p > 0.5 else "DOWN" if p < 0.5 else "SKIP"
            ref = self.store.get("reference")
            if p is not None:
                try:
                    number(market["assetPriceTerms"]["priceToBeat"], True)
                    details.update(theta=max(0.095, number(market.get("feeCoefficient", 0.095), True)),
                                   tick=number(market["orderPriceMinTickSize"], True),
                                   quantum=number(market["minimumTradeQty"], True))
                except (KeyError, TypeError, ValueError):
                    reason = "INVALID_EXECUTION_TERMS"
            bids, asks = (book or {}).get("bids", []), (book or {}).get("asks", [])
            details.update(
                elapsed=elapsed, window_end=end, window_start=start, checkpoint=checkpoint,
                book_timestamp=(book or {}).get("timestamp"),
                book_checked=(self.store.get("health_market") or {}).get("checked"),
                reference_price=(ref or {}).get("price"),
                reference_timestamp=(ref or {}).get("timestamp"),
                spread=asks[0]["price"]-bids[0]["price"] if bids and asks else None,
                bid_depth=sum(x["quantity"] for x in bids[:5]),
                ask_depth=sum(x["quantity"] for x in asks[:5]),
                protocol="fixed-timing-v1",
            )
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = json.loads(
                    db.execute("SELECT payload FROM state WHERE key='mode'").fetchone()[0]
                )
                emergency = db.execute("SELECT payload FROM state WHERE key='emergency'").fetchone()
                if current != mode or emergency and json.loads(emergency[0]):
                    return
                if p is not None:
                    for state_key, expected in (("market", market), ("book", book), ("reference", ref)):
                        row = db.execute("SELECT payload FROM state WHERE key=?", (state_key,)).fetchone()
                        if not row or json.loads(row[0]) != expected:
                            return
                c = db.execute(
                    "INSERT OR IGNORE INTO v2_decisions VALUES (?,?,?,?,?,?,?,?)",
                    (
                        market["slug"],
                        checkpoint,
                        now,
                        MODEL,
                        p,
                        side,
                        reason,
                        json.dumps(details),
                    ),
                )
                if not c.rowcount:
                    continue
                # First decision mirrored for compatible label collection, not execution.
                db.execute(
                    """INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET
                    time=excluded.time,model=excluded.model,up=excluded.up,decision=excluded.decision,
                    reason=excluded.reason,details=excluded.details WHERE predictions.up IS NULL AND excluded.up IS NOT NULL""",
                    (
                        market["slug"],
                        now,
                        MODEL,
                        p,
                        side,
                        reason,
                        int(start) % 3600 == 0,
                        json.dumps(details),
                    ),
                )
                if p is not None:
                    db.execute(
                        "INSERT OR IGNORE INTO v2_hourly VALUES (?,?,?,?,?)",
                        (int(start) // 3600, now, market["slug"], p, json.dumps(details)),
                    )
                if p is not None and book and book["slug"] == market["slug"]:
                    db.execute("INSERT OR IGNORE INTO timing_checkpoint_books VALUES (?,?,?)",
                               (market["slug"], checkpoint, json.dumps(book)))

        self.continuous.evaluate(now, mode, live_clock)

    def settle(self, now):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            unified.settle(db, now)
            for row in db.execute("SELECT * FROM v2_orders WHERE status='OPEN'").fetchall():
                o = dict(row)
                outcome = db.execute(
                    "SELECT * FROM verified_markets WHERE slug=?", (o["slug"],)
                ).fetchone()
                if not outcome or now < max(outcome["end"], outcome["verified_at"]):
                    continue
                flip = db.execute("SELECT * FROM paper_flips WHERE order_id=?", (o["id"],)).fetchone()
                position = flip if flip else o
                payout = position["quantity"] if position["side"] == outcome["outcome"] else 0
                pnl = payout - o["cost"] + (flip["credit"]-flip["debit"] if flip else 0)
                db.execute(
                    "UPDATE v2_orders SET status='SETTLED',result=?,pnl=?,settled=? WHERE id=? AND status='OPEN'",
                    (outcome["outcome"], pnl, now, o["id"]),
                )
                db.execute(
                    """UPDATE v2_accounts SET balance=balance+?,realized=realized+?,
                              streak=CASE WHEN ?<0 THEN streak+1 ELSE 0 END WHERE id=?""",
                    (payout, pnl, pnl, o["account"]),
                )
                a = db.execute("SELECT * FROM v2_accounts WHERE id=?", (o["account"],)).fetchone()
                db.execute(
                    "INSERT INTO v2_equity(time,account,balance,realized) VALUES (?,?,?,?)",
                    (now, o["account"], a["balance"], a["realized"]),
                )

    def summary(self):
        now = time.time()
        with self.store.connect() as db:
            db.execute("BEGIN")
            accounts = [dict(r) for r in db.execute("SELECT * FROM v2_accounts ORDER BY id")]
            marks = adaptive_entry.open_position_marks(db, now)
            orders = [
                dict(r)
                for r in db.execute(
                    """SELECT o.id,o.account,o.slug,o.side,o.time,o.model,o.probability,o.quantity,o.cost,
                          o.fee,o.status,o.result,o.pnl,o.settled,o.segment,
                          f.side AS flipped_side,f.model AS flip_model,f.time AS flip_time,
                          f.debit AS flip_debit,f.credit AS flip_credit
                          FROM v2_orders o LEFT JOIN paper_flips f ON f.order_id=o.id ORDER BY o.id DESC"""
                )
            ]
            for a in accounts:
                own = [o for o in orders if o["account"] == a["id"]]
                done = [o for o in own if o["status"] == "SETTLED"]
                a.update(
                    trades=len(done),
                    wins=sum(o["pnl"] > 0 for o in done),
                    losses=sum(o["pnl"] < 0 for o in done),
                    flats=sum(o["pnl"] == 0 for o in done),
                    win_rate=sum(o["pnl"] > 0 for o in done) / len(done) if done else None,
                    streaks=analytics.streaks(
                        ["WIN" if o["pnl"]>0 else "LOSS" if o["pnl"]<0 else "FLAT"
                         for o in sorted(done, key=lambda o:(o["settled"],o["id"]))], "WIN","LOSS"),
                    exposure=sum(o["flip_debit"] if o["flipped_side"] else o["cost"]
                                 for o in own if o["status"] == "OPEN"),
                    daily_loss=daily_loss(own, now),
                    daily_limit=None if a["id"].startswith("adaptive-") else 20,
                    starting_cash=adaptive_entry.STARTING_CASH if a["id"].startswith("adaptive-") else 100,
                    day=local_day(now),
                    day_pnl=sum(
                        o["pnl"] for o in done if local_day(o["settled"]) == local_day(now)
                    ),
                )
                a["equity_at_cost"] = a["balance"] + a["exposure"]
                if a["id"].startswith("adaptive-"):
                    proceeds = marks.get(a["id"], 0)
                    a["market_equity"] = a["balance"] + proceeds if proceeds is not None else None
                    a["unrealized_pnl"] = a["market_equity"] - a["starting_cash"] - a["realized"] \
                        if a["market_equity"] is not None else None
            prediction = db.execute(
                "SELECT * FROM v2_decisions ORDER BY time DESC,checkpoint DESC LIMIT 1"
            ).fetchone()
            changes = [
                dict(r) for r in db.execute("SELECT * FROM strategy_changes ORDER BY id DESC")
            ]
            active_done = [
                o
                for o in orders
                if o["account"] == "adaptive-1"
                and o["segment"] == changes[0]["id"]
                and o["pnl"] is not None
            ]
            performance = dict(
                net_profit=sum(o["pnl"] for o in active_done),
                trades=len(active_done),
                win_rate=sum(o["pnl"] > 0 for o in active_done) / len(active_done)
                if active_done
                else None,
            )
            adaptive = db.execute("SELECT * FROM adaptive_decisions ORDER BY id DESC LIMIT 1").fetchone()
            risks = [
                dict(r) for r in db.execute("SELECT * FROM risk_events ORDER BY id DESC LIMIT 50")
            ]
            skips = [
                dict(r) for r in db.execute("SELECT * FROM v2_skips ORDER BY id DESC LIMIT 100")
            ]
        return dict(
            accounts=accounts,
            orders=orders[:500],
            prediction=dict(prediction) if prediction else None,
            adaptive_decision=dict(adaptive) if adaptive else None,
            model=changes[0]["model"],
            experiment=self.store.get("v2_experiment"),
            risk_events=risks,
            skips=skips,
            strategy=changes[0],
            strategy_performance=performance,
            strategy_history=changes,
            checkpoints=CHECKPOINTS,
            hourly_comparison={
                a["id"]: a["realized"] for a in accounts if a["id"].startswith("hourly-")
            },
        )
