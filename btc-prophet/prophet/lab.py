"""Independent forward shadow configurations sharing causal market observations."""

import json
import math
import pickle
import time
from .models import BASE_PARTICIPANTS, Models, PARTICIPANTS, VOTE_FAMILY, VOTE_VERSION
from . import consensus
from . import pattern_odds
from .adaptive_entry import BOOK_CONFIRM_MAX_AGE, REFERENCE_MAX_AGE, STARTING_CASH, get_state
from .execution import paper_buy
from .v2risk import CHECKPOINTS, entry_block

CURRENT_TIMING = "timing/v2/"


def timing_bankroll(config_id):
    return STARTING_CASH if config_id.startswith(CURRENT_TIMING) else 100


EXITS = ("HOLD",)
MODEL_ERRORS = (ValueError, ImportError, OSError, TypeError, RuntimeError, EOFError, pickle.UnpicklingError)


class Lab:
    def __init__(self, store, paper):
        self.store, self.paper = store, paper
        self.models = Models(store)
        self.model_failures = set()
        from .smart import SmartResearch

        self.flip = SmartResearch(store, paper, version=2)
        with store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS lab_configs (
              id TEXT PRIMARY KEY, family TEXT, version TEXT, checkpoint INTEGER,
              exit TEXT, stake REAL, status TEXT, cash REAL DEFAULT 100,
              created REAL);
            CREATE TABLE IF NOT EXISTS lab_predictions (
              config_family TEXT, version TEXT, slug TEXT, checkpoint INTEGER,
              time REAL, up REAL, features TEXT, PRIMARY KEY(version,slug,checkpoint));
            CREATE INDEX IF NOT EXISTS lab_predictions_recent ON lab_predictions(checkpoint,time);
            CREATE TABLE IF NOT EXISTS model_forecast_health (
              version TEXT PRIMARY KEY, time REAL NOT NULL, slug TEXT NOT NULL, status TEXT NOT NULL,
              detail TEXT);
            CREATE TABLE IF NOT EXISTS lab_orders (
              id INTEGER PRIMARY KEY, config TEXT, slug TEXT, side TEXT, time REAL,
              quantity REAL, remaining REAL, cost REAL, proceeds REAL DEFAULT 0,
              fees REAL, status TEXT, pnl REAL, settled REAL, details TEXT,
              UNIQUE(config,slug));
            CREATE TABLE IF NOT EXISTS lab_books (slug TEXT,time REAL,payload TEXT,PRIMARY KEY(slug,time));
            CREATE INDEX IF NOT EXISTS lab_orders_settled ON lab_orders(config,settled,id);
            CREATE TABLE IF NOT EXISTS lab_processed (slug TEXT, checkpoint INTEGER, PRIMARY KEY(slug,checkpoint));
            CREATE TABLE IF NOT EXISTS lab_skips (
              config TEXT, slug TEXT, time REAL, reason TEXT, PRIMARY KEY(config,slug));
            CREATE TABLE IF NOT EXISTS pattern_forecasts (
              slug TEXT,checkpoint INTEGER,time REAL,version TEXT,up REAL,evidence TEXT,PRIMARY KEY(slug,checkpoint));
            CREATE TABLE IF NOT EXISTS lab_counterfactuals (
              config TEXT,slug TEXT,side TEXT,time REAL,quantity REAL,cost REAL,reason TEXT,
              PRIMARY KEY(config,slug,side));
            CREATE TABLE IF NOT EXISTS lab_runs (
              id INTEGER PRIMARY KEY, time REAL, kind TEXT, details TEXT);
            CREATE TABLE IF NOT EXISTS adaptive_selections (
              slug TEXT PRIMARY KEY, time REAL, selected TEXT, reason TEXT, mode TEXT);
            """)
            consensus.initialize(db)
        from .adaptive import Adaptive

        self.adaptive = Adaptive(store, paper)
        self.refresh()

    def refresh(self):
        self.models.sync_logistic()
        catalog = self.models.catalog()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            experiment = get_state(db, "v2_experiment") or {}
            if experiment.get("preflight") and experiment.get("status") == "RUNNING":
                return
            db.execute("""UPDATE lab_configs SET status='ARCHIVED PROTOCOL'
                        WHERE id NOT LIKE 'timing/v2/%' AND status='FORWARD SHADOW'""")
            db.execute("UPDATE lab_configs SET status='ARCHIVED RESEARCH' "
                       "WHERE family IN ('lstm','hmm') AND status='FORWARD SHADOW'")
            for family in catalog:
                if family["id"] not in PARTICIPANTS:
                    continue
                run = (
                    family["versions"][0]
                    if family["versions"] and family["status"] == "AVAILABLE"
                    else None
                )
                version = run["version"] if run else "UNAVAILABLE-" + family["id"]
                db.execute(
                    "UPDATE lab_configs SET status='ARCHIVED VERSION' WHERE family=? AND version!=? AND status='FORWARD SHADOW'",
                    (family["id"], version),
                )
                for checkpoint in CHECKPOINTS:
                    for exit in EXITS:
                        for stake in (1, 2, 3):
                            id = f"{CURRENT_TIMING}{version}/{checkpoint}/{exit}/{stake}"
                            db.execute(
                                "INSERT OR IGNORE INTO lab_configs VALUES (?,?,?,?,?,?,?,?,?)",
                                (
                                    id,
                                    family["id"],
                                    version,
                                    checkpoint,
                                    exit,
                                    stake,
                                    "FORWARD SHADOW" if run else family["status"],
                                    STARTING_CASH,
                                    time.time(),
                                ),
                            )

    def predict(self, version, x, checkpoint, slug=None, now=None):
        from .models import feature_sequence

        now = time.time() if now is None else now
        sequence = feature_sequence(
            self.store, x, checkpoint, slug or (self.store.get("market") or {}).get("slug", ""), now
        )
        estimate = self.models.predict(version, x, sequence)
        if estimate is None:
            raise ValueError("LSTM needs 16 past feature observations")
        return estimate

    def tick(self, now=None):
        live_clock = now is None
        now = time.time() if live_clock else now
        self.exit_and_settle(now)
        if self.store.get("mode") not in ("LEARN", "SIMULATE") or self.store.get("emergency", False):
            return
        if now - getattr(self, "refreshed", 0) > 60:
            self.refresh()
            self.refreshed = now
        with self.store.connect() as db:
            rows = db.execute("""SELECT p.*, b.payload AS captured_book
                  FROM v2_decisions p LEFT JOIN lab_processed d
                  ON d.slug=p.slug AND d.checkpoint=p.checkpoint
                  LEFT JOIN timing_checkpoint_books b ON p.slug=b.slug AND p.checkpoint=b.checkpoint
                  WHERE d.slug IS NULL ORDER BY p.time,p.checkpoint""").fetchall()
        for decision in rows:
            details = json.loads(decision["details"])
            if details.get("protocol") != "fixed-timing-v1":
                # The prior protocol's observations and results remain archived.
                with self.store.connect() as db:
                    db.execute("INSERT OR IGNORE INTO lab_processed VALUES (?,?)",
                               (decision["slug"],decision["checkpoint"]))
                continue
            observed_at = decision["time"]
            checkpoint = decision["checkpoint"]
            slug = decision["slug"]
            deadline = min(details["window_start"]+checkpoint+3, details["window_end"])
            book = json.loads(decision["captured_book"]) if decision["captured_book"] else None
            reason = None
            has_features = decision["up"] is not None and all(
                k in details for k in ("momentum_60", "trend_300", "volatility")
            )
            has_terms = all(k in details for k in ("theta", "tick", "quantum"))
            if now > deadline or now >= details["window_end"]:
                reason = decision["reason"] if decision["up"] is None else "MISSED_CHECKPOINT"
            elif not has_features or not has_terms:
                reason = decision["reason"] if not has_features else "INVALID_EXECUTION_TERMS"
            elif (not book or book["slug"] != slug or book.get("state") != "MARKET_STATE_OPEN"
                  or book["timestamp"] > observed_at
                  or not 0 <= observed_at-(details.get("book_checked") or 0) <= BOOK_CONFIRM_MAX_AGE
                  or not 0 <= observed_at-(details.get("reference_timestamp") or 0) <= REFERENCE_MAX_AGE):
                reason = "STALE_OR_MISSING_CHECKPOINT_SNAPSHOT"
            with self.store.connect() as db:
                configs = [dict(r) for r in db.execute(
                    """SELECT c.*,m.trained FROM lab_configs c JOIN model_registry m ON m.version=c.version
                       WHERE c.checkpoint=? AND c.status='FORWARD SHADOW' AND c.exit='HOLD'
                       AND c.id LIKE 'timing/v2/%'""",
                    (checkpoint,))]
            probabilities = {}
            vote = None
            pattern_evidence = None
            health = {}
            x = []
            if has_features and now <= deadline and now < details["window_end"]:
                x = [details["momentum_60"],details["trend_300"],math.log(max(details["volatility"],1e-7))]
                causal = {c["version"] for c in configs if c["trained"] is None or c["trained"] <= observed_at}
                for version in sorted(causal - {VOTE_VERSION}):
                    try:
                        if version == pattern_odds.VERSION:
                            if reason:
                                health[version] = ("WAITING", reason)
                                continue
                            p, pattern_evidence = pattern_odds.forecast(self.store,slug,checkpoint,observed_at)
                            if p is None:
                                health[version] = ("WAITING", pattern_evidence["reason"])
                                continue
                        else:
                            p = self.predict(version, x, checkpoint, slug, observed_at)
                        if not math.isfinite(p) or not 0 <= p <= 1:
                            raise ValueError("Invalid model probability")
                        forecast_at = time.time() if live_clock else observed_at
                        if forecast_at >= details["window_end"]:
                            health[version] = ("WAITING", "FORECAST_COMPLETED_AFTER_CLOSE")
                            continue
                        probabilities[version] = (p, forecast_at)
                        health[version] = ("READY", None)
                        self.model_failures.discard(version)
                    except MODEL_ERRORS as exc:
                        health[version] = ("INFERENCE FAILED", f"{type(exc).__name__}: {exc}")
                        if version not in self.model_failures and "16 past" not in str(exc):
                            self.store.event("ERROR", f"Model {version} skipped: {type(exc).__name__}: {exc}")
                            self.model_failures.add(version)
            if VOTE_VERSION in {c["version"] for c in configs}:
                components = {c["family"]:dict(version=c["version"],up=probabilities[c["version"]][0],
                              time=probabilities[c["version"]][1]) for c in configs
                              if c["family"] in BASE_PARTICIPANTS and c["version"] in probabilities}
                vote = consensus.vote(components,BASE_PARTICIPANTS)
                vote.update(spread=details.get("spread"), volatility=details.get("volatility"))
                vote_time = time.time() if live_clock else observed_at
                if vote["up_share"] is not None and vote_time < details["window_end"]:
                    probabilities[VOTE_VERSION] = (vote["up_share"],vote_time)
                    health[VOTE_VERSION] = ("READY",None)
                else:
                    health[VOTE_VERSION] = ("WAITING","MISSING_SYNCHRONIZED_VOTES")
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                mode_row = db.execute("SELECT payload FROM state WHERE key='mode'").fetchone()
                emergency = db.execute("SELECT payload FROM state WHERE key='emergency'").fetchone()
                mode = json.loads(mode_row[0]) if mode_row else "OFF"
                if mode not in ("LEARN","SIMULATE") or emergency and json.loads(emergency[0]):
                    return
                if not db.execute("INSERT OR IGNORE INTO lab_processed VALUES (?,?)",(slug,checkpoint)).rowcount:
                    continue
                if not reason and book:
                    db.execute("INSERT OR IGNORE INTO lab_books VALUES (?,?,?)",
                               (slug,book["timestamp"],json.dumps(book)))
                if checkpoint == 90:
                    db.executemany("""INSERT INTO model_forecast_health VALUES (?,?,?,?,?)
                        ON CONFLICT(version) DO UPDATE SET time=excluded.time,slug=excluded.slug,
                            status=excluded.status,detail=excluded.detail
                        WHERE excluded.time>=model_forecast_health.time""",
                        ((version, observed_at, slug, status, detail)
                         for version, (status, detail) in health.items()))
                if vote is not None:
                    db.execute("INSERT OR IGNORE INTO model_vote_forecasts VALUES (?,?,?,?)",
                               (slug,checkpoint,vote_time,json.dumps(vote)))
                if pattern_evidence is not None:
                    db.execute("INSERT OR IGNORE INTO pattern_forecasts VALUES (?,?,?,?,?,?)",
                        (slug,checkpoint,observed_at,pattern_odds.VERSION,
                         probabilities.get(pattern_odds.VERSION, (None, None))[0],json.dumps(pattern_evidence)))
                for c in configs:
                    p, forecast_at = probabilities.get(c["version"], (None, None))
                    if p is not None:
                        db.execute("INSERT OR IGNORE INTO lab_predictions VALUES (?,?,?,?,?,?,?)",
                                   (c["family"],c["version"],slug,checkpoint,forecast_at,p,json.dumps(x)))
                    if mode != "SIMULATE":
                        continue
                    why = reason
                    if live_clock:
                        execute_at = time.time()
                        if execute_at > deadline or execute_at >= details["window_end"]:
                            why = why or "MISSED_CHECKPOINT"
                        if not why and (execute_at-details["book_checked"] > BOOK_CONFIRM_MAX_AGE
                                        or execute_at-details["reference_timestamp"] > REFERENCE_MAX_AGE):
                            why = "STALE_DURING_EVALUATION"
                    if c["family"] == VOTE_FAMILY and p is None:
                        why = why or "MISSING_SYNCHRONIZED_VOTES"
                    self.enter_timing(db,c,slug,observed_at,p,details,book,why)

    def enter_timing(self, db, config, slug, observed_at, p, details, book, reason):
        own = db.execute("SELECT status,slug FROM lab_orders WHERE config=? AND (status='OPEN' OR slug=?)",
                         (config["id"],slug)).fetchall()
        if any(row["slug"] == slug for row in own):
            return
        cash = db.execute("SELECT cash FROM lab_configs WHERE id=?", (config["id"],)).fetchone()[0]
        reason = reason or entry_block(dict(balance=cash,halted=None),own,config["stake"],observed_at,daily_cap=False)
        if not reason and p is None:
            reason = "MODEL_PREDICTION_UNAVAILABLE"
        if not reason and p == 0.5:
            reason = "MODEL_TIE_NO_DIRECTION"
        if not reason and (not book or not book["bids"] or not book["asks"]):
            reason = "NO_LIQUIDITY"
        if not reason:
            side = "UP" if p > 0.5 else "DOWN"
            fill, reason = paper_buy(book, side, config["stake"], theta=details["theta"],
                                     tick=details["tick"], quantum=details["quantum"])
        if reason:
            db.execute("INSERT OR IGNORE INTO lab_skips VALUES (?,?,?,?)",
                       (config["id"],slug,observed_at,reason))
            valid = (book and book["slug"] == slug and book["state"] == "MARKET_STATE_OPEN"
                     and all(k in details for k in ("theta","tick","quantum","window_end"))
                     and observed_at < details["window_end"]
                     and book["timestamp"] <= observed_at
                     and 0 <= observed_at-(details.get("book_checked") or 0) <= BOOK_CONFIRM_MAX_AGE
                     and 0 <= observed_at-(details.get("reference_timestamp") or 0) <= REFERENCE_MAX_AGE)
            for option in ("UP","DOWN"):
                hypothetical, why = paper_buy(book,option,config["stake"],theta=details["theta"],
                    tick=details["tick"],quantum=details["quantum"]) if valid else (None,"NO_EXECUTABLE_CAPTURE")
                db.execute("INSERT OR IGNORE INTO lab_counterfactuals VALUES (?,?,?,?,?,?,?)",
                    (config["id"],slug,option,observed_at,hypothetical["quantity"] if hypothetical else None,
                     hypothetical["total"] if hypothetical else None,why or "UNFUNDED"))
            return
        audit = dict(prediction=details,fill=fill,probability=p,exit=config["exit"],
                     version=config["version"],source="FIXED TIMING SHADOW",
                     book_reference=dict(slug=slug,timestamp=book["timestamp"],table="lab_books"))
        inserted = db.execute("""INSERT OR IGNORE INTO lab_orders
            (config,slug,side,time,quantity,remaining,cost,fees,status,details) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (config["id"],slug,side,observed_at,fill["quantity"],fill["quantity"],fill["total"],
             fill["fee"],"OPEN",json.dumps(audit)))
        if inserted.rowcount and not db.execute("UPDATE lab_configs SET cash=cash-? WHERE id=? AND cash>=?",
                                                (fill["total"],config["id"],fill["total"])).rowcount:
            raise ValueError("Critical accounting error: shadow debit failed")

    def exit_and_settle(self, now):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            orders = db.execute("""SELECT o.*, v.end, v.outcome FROM lab_orders o
                JOIN verified_markets v ON v.slug=o.slug
                WHERE o.status='OPEN' AND v.end<=? AND v.verified_at<=?""", (now, now)).fetchall()
            for order in orders:
                outcome = db.execute("SELECT * FROM verified_markets WHERE slug=?",
                                     (order["slug"],)).fetchone()
                payout = order["remaining"] if order["side"] == order["outcome"] else 0
                proceeds = order["proceeds"] + payout
                audit = json.loads(order["details"] or "{}")
                audit.setdefault("exit_observations", []).append(
                    dict(time=now, book_timestamp=None, fill=None, settlement=dict(outcome))
                )
                db.execute("""UPDATE lab_orders SET remaining=0,proceeds=?,status='SETTLED',
                              pnl=?,settled=?,details=? WHERE id=?""",
                           (proceeds, proceeds-order["cost"], now, json.dumps(audit), order["id"]))
                db.execute("UPDATE lab_configs SET cash=cash+? WHERE id=?", (payout, order["config"]))

    def model_performance(self):
        """Rank each family's best individual account by observed settled net P&L."""
        catalog = self.models.catalog()
        with self.store.connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT payload FROM state WHERE key='v2_experiment'").fetchone()
            experiment = (json.loads(row[0]) or {}) if row else {}
            start = experiment.get("start", get_state(db, "paper_pilot_started"))
            # Group every account independently. Filter adaptive rows by version
            # so a previously active model does not contribute to a new version.
            fixed = db.execute("""SELECT c.family,c.version,c.stake,c.checkpoint,c.exit,
                          count(*) AS trades,sum(o.pnl>0) AS wins,sum(o.pnl<0) AS losses,sum(o.pnl) AS pnl
                FROM lab_configs c JOIN lab_orders o ON o.config=c.id
                WHERE c.id LIKE 'timing/v2/%' AND c.exit='HOLD' AND o.status='SETTLED' AND o.pnl IS NOT NULL AND o.time>=?
                GROUP BY c.id""", (start,)).fetchall()
            adaptive = db.execute("""SELECT coalesce(r.kind,'momentum') AS family,
                            o.model AS version,a.budget AS stake,NULL AS checkpoint,'ADAPTIVE' AS exit,
                            count(*) AS trades,sum(o.pnl>0) AS wins,sum(o.pnl<0) AS losses,sum(o.pnl) AS pnl
                FROM v2_orders o JOIN v2_accounts a ON a.id=o.account
                LEFT JOIN model_registry r ON r.version=o.model
                WHERE o.account LIKE 'adaptive-%' AND o.status='SETTLED' AND o.pnl IS NOT NULL AND o.time>=?
                GROUP BY o.account,o.model""", (start,)).fetchall()
        result = []
        for family in catalog:
            candidates = [dict(r) for r in (*fixed, *adaptive) if r["family"] == family["id"]]
            candidates.sort(key=lambda r: (-r["pnl"], -r["trades"], r["stake"], r["version"],
                                            r["checkpoint"] or 0, r["exit"]))
            best = candidates[0] if candidates else None
            result.append(dict(model=family["id"], status=family["status"], best=best,
                               accounts_with_results=len(candidates)))
        result.sort(key=lambda r: (r["best"] is None, -r["best"]["pnl"] if r["best"] else 0, r["model"]))
        return dict(start=start, models=result)

    def leaderboard(self):
        with self.store.connect() as db:
            rows = []
            for config in db.execute("SELECT * FROM lab_configs").fetchall():
                c = dict(config)
                orders = [
                    dict(r)
                    for r in db.execute(
                        "SELECT id,slug,time,cost,status,pnl,settled FROM lab_orders WHERE config=? ORDER BY settled,id",
                        (c["id"],),
                    )
                ]
                done = [o for o in orders if o["pnl"] is not None]
                skips = db.execute(
                    "SELECT count(*) FROM lab_skips WHERE config=?", (c["id"],)
                ).fetchone()[0]
                starting_cash = timing_bankroll(c["id"])
                equity = peak = starting_cash
                dd = 0
                for o in done:
                    equity += o["pnl"]
                    peak = max(peak, equity)
                    dd = max(dd, peak - equity)
                loss = -sum(min(0, o["pnl"]) for o in done)
                profit = sum(max(0, o["pnl"]) for o in done)
                c.update(
                    scope="FORWARD SHADOW",
                    profitability="UNKNOWN" if not done else "OBSERVED SIMULATION ONLY",
                    net_profit=sum(o["pnl"] for o in done) if done else None,
                    starting_cash=starting_cash,
                    return_percent=100 * (equity - starting_cash) / starting_cash if done else None,
                    win_rate=sum(o["pnl"] > 0 for o in done) / len(done) if done else None,
                    maximum_drawdown=dd if done else None,
                    profit_factor=profit / loss if loss else None,
                    trades=len(done),
                    skips=skips,
                    test_start=min((o["time"] for o in orders), default=None),
                    test_end=max((o["settled"] or o["time"] for o in orders), default=None),
                    data_completeness="Observed forward decisions only; not a historical-week backtest",
                    calibration=None,
                )
                rows.append(c)
        return dict(
            planned_configurations=len(PARTICIPANTS) * len(CHECKPOINTS) * len(EXITS) * 3,
            configurations=sorted(
                rows,
                key=lambda c: (c["net_profit"] is not None, c["net_profit"] or 0),
                reverse=True,
            ),
            models=self.models.catalog(),
            warning="Multiple comparisons increase overfitting risk. Fresh out-of-sample validation is required; historical rank does not authorize promotion.",
        )
