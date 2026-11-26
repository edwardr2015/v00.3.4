"""Continuous paper entries against observed Polymarket US books."""

import hashlib
import json
import math
import time

from .execution import compare_stakes, execute, paper_buy
from .autonomous import Selection, replacement, shadow_reverse
from . import hold_research, unified
from .providers import number, timestamp, validate_market
from .v2risk import CHECKPOINTS, entry_block

STARTING_CASH = 1000
MIN_EDGE = 0.03
ACCOUNTS = tuple((f"adaptive-{stake}", stake) for stake in (1, 2, 3))
CHECKPOINT = 0  # Ledger identifier for an adaptive final skip.
INPUTS = ("market", "book", "reference", "health_market", "health_reference", "collection_paused")


def snapshot(db):
    params = ",".join("?" for _ in INPUTS)
    return {
        row["key"]: json.loads(row["payload"])
        for row in db.execute(f"SELECT key,payload FROM state WHERE key IN ({params})", INPUTS)
    }


def get_state(db, key):
    row = db.execute("SELECT payload FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


REFERENCE_MAX_AGE = 30
BOOK_CONFIRM_MAX_AGE = 10


def freshness(data, now):
    if data.get("collection_paused"):
        return "COLLECTOR_PAUSED"
    for key in ("reference", "book"):
        if not data.get(key):
            return "WAITING_FOR_" + key.upper()
        if data[key]["timestamp"] > now:
            return "FUTURE_" + key.upper()
    if now - data["reference"]["timestamp"] > REFERENCE_MAX_AGE:
        return "STALE_REFERENCE"
    for key, limit in (("market", BOOK_CONFIRM_MAX_AGE), ("reference", REFERENCE_MAX_AGE)):
        health = data.get("health_" + key, {})
        if not health.get("ok") or not 0 <= now - health.get("checked", 0) <= limit:
            return "PROVIDER_ERROR_" + key.upper()
    return None


def open_position_marks(db, now):
    """Return executable liquidation estimates for open adaptive positions."""
    positions = db.execute("""SELECT o.account,o.slug,COALESCE(f.side,o.side) AS side,
        COALESCE(f.quantity,o.quantity) AS quantity,o.details
        FROM v2_orders o LEFT JOIN paper_flips f ON f.order_id=o.id
        WHERE o.status='OPEN' AND o.account LIKE 'adaptive-%'""").fetchall()
    if not positions:
        return {}
    marks = {position["account"]:None for position in positions}
    data = snapshot(db)
    market, book = data.get("market") or {}, data.get("book") or {}
    if (freshness(data, now) or not validate_market(market, now)
            or book.get("state") != "MARKET_STATE_OPEN" or book.get("slug") != market.get("slug")):
        return marks
    for position in positions:
        if position["slug"] != book["slug"]:
            continue
        try:
            details = json.loads(position["details"] or "{}")
            terms = (details.get("fill") or {}).get("assumptions", {})
            if not all(key in terms for key in ("theta", "tick", "quantum")):
                continue
            fill = execute(book, position["side"], quantity=position["quantity"], buying=False, **terms)
        except (AttributeError, ValueError, TypeError):
            continue
        if fill["quantity"] + 1e-9 >= position["quantity"]:
            marks[position["account"]] = fill["total"]
    return marks


def opportunities(book, budget, up, terms):
    candidates = []
    for side, probability in (("UP", up), ("DOWN", 1 - up)):
        fill, reason = paper_buy(book, side, budget, **terms)
        edge = probability * fill["quantity"] - fill["total"] if fill else None
        candidates.append(dict(side=side, probability=probability, fill=fill, ev=edge,
                               ev_rate=edge / fill["total"] if fill else None, reason=reason))
    return candidates


class AdaptiveEntry:
    def __init__(self, paper):
        self.paper = paper
        self.store = paper.store
        self.selection = Selection(self.store)
        with self.store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS adaptive_windows (
                slug TEXT PRIMARY KEY, start REAL NOT NULL, end REAL NOT NULL, finalized REAL);
            CREATE INDEX IF NOT EXISTS adaptive_pending ON adaptive_windows(end) WHERE finalized IS NULL;
            CREATE TABLE IF NOT EXISTS adaptive_decisions (
                id INTEGER PRIMARY KEY, slug TEXT NOT NULL, account TEXT NOT NULL, time REAL NOT NULL,
                model TEXT NOT NULL, up REAL, decision TEXT NOT NULL, reason TEXT NOT NULL,
                observation TEXT NOT NULL, details TEXT NOT NULL, UNIQUE(slug, account, observation));
            CREATE TABLE IF NOT EXISTS adaptive_later (
                order_id INTEGER NOT NULL, delay INTEGER NOT NULL, target REAL NOT NULL,
                time REAL NOT NULL, book_timestamp REAL, quantity REAL, cost REAL, fee REAL,
                reason TEXT NOT NULL, details TEXT, PRIMARY KEY(order_id,delay));
            CREATE INDEX IF NOT EXISTS adaptive_latest ON adaptive_decisions(slug,account,id DESC);
            CREATE INDEX IF NOT EXISTS v2_orders_open ON v2_orders(account) WHERE status='OPEN';
            """)
            hold_research.initialize(db)
            db.executemany("INSERT OR IGNORE INTO v2_accounts(id,budget,balance) VALUES (?,?,?)",
                           [(account, stake, STARTING_CASH) for account, stake in ACCOUNTS])

    @staticmethod
    def log(db, slug, account, now, model, up, decision, reason, key, details):
        db.execute("""INSERT OR IGNORE INTO adaptive_decisions
                      (slug,account,time,model,up,decision,reason,observation,details)
                      VALUES (?,?,?,?,?,?,?,?,?)""",
                   (slug, account, now, model, up, decision, reason, key, json.dumps(details)))

    def finalize(self, now):
        with self.store.connect() as db:
            if not db.execute("SELECT 1 FROM adaptive_windows WHERE finalized IS NULL AND end<=? LIMIT 1", (now,)).fetchone():
                return
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT * FROM adaptive_windows WHERE finalized IS NULL AND end<=?", (now,)).fetchall():
                slug = row["slug"]
                for account, _ in ACCOUNTS:
                    if db.execute("SELECT 1 FROM v2_orders WHERE account=? AND slug=?", (account, slug)).fetchone():
                        continue
                    last = db.execute(
                        "SELECT model,up,reason FROM adaptive_decisions WHERE slug=? AND account=? ORDER BY id DESC LIMIT 1",
                        (slug, account),
                    ).fetchone()
                    reason = last["reason"] if last else "NO_EVALUATION"
                    model, up = (last["model"], last["up"]) if last else ("", None)
                    self.log(db, slug, account, now, model, up, "SKIP", "WINDOW_CLOSED", "final",
                             dict(last_reason=reason, window_end=row["end"]))
                    self.paper._skip(db, account, slug, CHECKPOINT, now, reason)
                db.execute("UPDATE adaptive_windows SET finalized=? WHERE slug=?", (now, slug))

    def compare_later(self, now):
        """Observe same-side counterfactual fills from the book at each later time."""
        with self.store.connect() as db:
            orders = db.execute("""SELECT o.*,a.budget,w.end FROM v2_orders o
                JOIN v2_accounts a ON a.id=o.account JOIN adaptive_windows w ON w.slug=o.slug
                WHERE o.account LIKE 'adaptive-%'
                AND (SELECT count(*) FROM adaptive_later l WHERE l.order_id=o.id) < ?
                AND min(o.time+?,w.end)<=?""", (len(CHECKPOINTS), CHECKPOINTS[0], now)).fetchall()
            if not orders:
                return
            data = snapshot(db)
            book, market = data.get("book") or {}, data.get("market")
            block = freshness(data, now)
            rows = []
            for order in orders:
                completed = {r[0] for r in db.execute("SELECT delay FROM adaptive_later WHERE order_id=?",
                                                       (order["id"],))}
                terms = json.loads(order["details"])["fill"]["assumptions"]
                for delay in CHECKPOINTS:
                    target = order["time"] + delay
                    if delay in completed or now < min(target, order["end"]):
                        continue
                    fill = None
                    if target >= order["end"]:
                        reason = "TARGET_AFTER_CLOSE"
                    elif now > target + 3 or now >= order["end"]:
                        reason = "LATER_OBSERVATION_MISSED"
                    elif (block or not market or market["slug"] != order["slug"]
                          or not validate_market(market, now) or book.get("slug") != order["slug"]
                          or book.get("state") != "MARKET_STATE_OPEN"):
                        continue  # Allow the next fresh sample within the processing window.
                    else:
                        fill, reason = paper_buy(book, order["side"], order["budget"], **terms)
                        if fill:
                            reason = fill["status"]
                    rows.append((order["id"], delay, target, now, book.get("timestamp") if fill else None,
                                 fill["quantity"] if fill else None, fill["total"] if fill else None,
                                 fill["fee"] if fill else None, reason, json.dumps(fill) if fill else None))
            db.executemany("INSERT OR IGNORE INTO adaptive_later VALUES (?,?,?,?,?,?,?,?,?,?)", rows)

    def evaluate(self, now, mode, live_clock):
        with self.store.connect() as db:
            data = snapshot(db)
            strategy = db.execute("SELECT id,model FROM strategy_changes ORDER BY id DESC LIMIT 1").fetchone()
            open_position = db.execute("""SELECT model FROM v2_orders WHERE account LIKE 'adaptive-%'
                AND status='OPEN' ORDER BY id LIMIT 1""").fetchone()
            if self.paper.unified_enabled:
                open_position = db.execute("""SELECT model FROM unified_trades
                    WHERE status='OPEN' AND stake=5""").fetchone()
            pinned = open_position["model"] if open_position else None
        market, book = data.get("market"), data.get("book") or {}
        if not market or not market.get("slug") or not validate_market(market, now):
            return
        slug = market["slug"]
        start, end = (timestamp(market["assetPriceTerms"][key]) for key in ("windowStart", "windowEnd"))
        reason = freshness(data, now)
        if not reason and (book["slug"] != slug or book.get("state") != "MARKET_STATE_OPEN"):
            reason = "WAITING_FOR_OPEN_BOOK"
        if not reason and (not book["bids"] or not book["asks"]):
            reason = "NO_LIQUIDITY"
        terms = {}
        try:
            opening = number(market["assetPriceTerms"]["priceToBeat"], True)
            terms.update(theta=max(.095, number(market.get("feeCoefficient", .095), True)),
                         tick=number(market["orderPriceMinTickSize"], True),
                         quantum=number(market["minimumTradeQty"], True))
            if terms["theta"] > 1 or terms["tick"] >= 1 or terms["quantum"] > 100:
                raise ValueError("Unsupported execution terms")
        except (KeyError, TypeError, ValueError):
            opening = None
            reason = reason or "INVALID_EXECUTION_TERMS"

        up, features = self.paper.probability(now, book=book if not reason else {})
        if up is not None and (not math.isfinite(up) or not 0 <= up <= 1):
            raise ValueError("Invalid adaptive probability")
        model, evidence = strategy["model"], None
        if not reason and (mode == "SIMULATE" or self.paper.unified_enabled):
            context = dict(features,**terms,window_end=end,reference_price=data["reference"].get("price"),
                           bid_depth=sum(x["quantity"] for x in book["bids"]),
                           ask_depth=sum(x["quantity"] for x in book["asks"]))
            if self.paper.unified_enabled and up is not None and features.get("volatility") is not None:
                from .model_paper import FORMULA_VERSIONS, REFERENCE_GAP, probabilities_from_market

                midpoint = (book["bids"][0]["price"]+book["asks"][0]["price"])/2
                raw = probabilities_from_market(midpoint,context["reference_price"],opening,
                                                features["volatility"],end-now,up)
                up, model = raw[REFERENCE_GAP], FORMULA_VERSIONS[REFERENCE_GAP]
            model, up, evidence = self.selection.predict(slug,now,up,features,model,pinned_model=pinned,
                pattern_context=(context,book,opening))
            features = dict(features,model_evidence=evidence)
            if up is None and pinned is not None:
                features["reason"] = "OPEN_POSITION_MODEL_UNAVAILABLE"
        strategy = dict(strategy, model=model)
        reason = reason or (features.get("reason", "INSUFFICIENT_DATA") if up is None else None)
        reference = data.get("reference") or {}
        bid, ask = book.get("bids", []), book.get("asks", [])
        details = dict(features, elapsed=now - start, window_start=start, window_end=end,
                       book_timestamp=book.get("timestamp"), reference_timestamp=reference.get("timestamp"),
                       reference_price=reference.get("price"), opening_price=opening, mode=mode,
                       book_checked=(data.get("health_market") or {}).get("checked"),
                       bid=bid[0]["price"] if bid else None, ask=ask[0]["price"] if ask else None,
                       observed_at=now,
                       spread=ask[0]["price"] - bid[0]["price"] if bid and ask else None,
                       bid_depth=sum(row["quantity"] for row in bid),
                       ask_depth=sum(row["quantity"] for row in ask), min_ev_rate=MIN_EDGE, **terms)
        # Transport receipt times may advance while the source is unchanged.
        # Keep one audit row per materially different observation or decision.
        identity = json.dumps((book, reference, start, end, opening, terms, features, up,
                               strategy["id"], mode, reason), sort_keys=True)
        key = hashlib.sha256(identity.encode()).hexdigest()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if get_state(db, "mode") != mode or get_state(db, "emergency") or snapshot(db) != data:
                return
            if db.execute("SELECT max(id) FROM strategy_changes").fetchone()[0] != strategy["id"]:
                return
            decision_time = time.time() if live_clock else now
            if not validate_market(market, decision_time):
                return
            experiment = get_state(db, "v2_experiment")
            if mode == "SIMULATE" and experiment and decision_time >= experiment["end"]:
                return
            reason = freshness(data, decision_time) or reason
            if mode == "SIMULATE":
                if not self.paper.unified_enabled:
                    db.execute("INSERT OR IGNORE INTO adaptive_windows(slug,start,end) VALUES (?,?,?)",
                               (slug, start, end))
                if evidence and not reason:
                    if self.paper.unified_enabled:
                        current = db.execute("SELECT model FROM unified_trades WHERE status='OPEN' AND stake=5").fetchone()
                    else:
                        current = db.execute("""SELECT model FROM v2_orders WHERE account LIKE 'adaptive-%'
                            AND status='OPEN' ORDER BY id LIMIT 1""").fetchone()
                    if current and current["model"]!=model:
                        return
                    prior = db.execute("SELECT model FROM paper_model_switches ORDER BY id DESC LIMIT 1").fetchone()
                    previous = prior["model"] if prior else None
                    if previous != model and current is None:
                        db.execute("INSERT INTO paper_model_switches(time,slug,previous,model,evidence) VALUES (?,?,?,?,?)",
                                   (decision_time,slug,previous,model,json.dumps(evidence)))
                    db.execute("""INSERT INTO paper_model_choices VALUES (?,?,?,?,?)
                        ON CONFLICT(slug,regime) DO UPDATE SET time=excluded.time,model=excluded.model,
                            evidence=excluded.evidence WHERE model<>excluded.model OR evidence<>excluded.evidence""",
                        (slug,evidence["regime"],decision_time,model,json.dumps(evidence)))
            if up is not None:
                label = dict(details, protocol="adaptive-observation-v1")
                db.execute("""INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(slug) DO UPDATE SET time=excluded.time,model=excluded.model,up=excluded.up,
                    decision=excluded.decision,reason=excluded.reason,details=excluded.details
                    WHERE predictions.up IS NULL""",
                           (slug, now, strategy["model"], up, "UP" if up > .5 else "DOWN" if up < .5 else "SKIP",
                            "ADAPTIVE_OBSERVATION", 0, json.dumps(label)))
            if self.paper.unified_enabled:
                def commit_time():
                    checked = time.time() if live_clock else decision_time
                    if checked >= end or (not reason and freshness(data,checked)):
                        return None
                    return checked

                if mode == "LEARN" or not db.execute("SELECT 1 FROM v2_accounts WHERE halted IS NOT NULL").fetchone():
                    unified.observe(db,slug,decision_time,mode,model,up,reason,details,book,terms,commit_time)
                return
            for account, _ in ACCOUNTS:
                self.evaluate_account(db, account, slug, decision_time, mode, strategy, up, reason,
                                      key, details, book, terms, live_clock, end)

    def evaluate_account(self, db, account, slug, now, mode, strategy, up, reason,
                         key, details, book, terms, live_clock, end):
        own = db.execute("""SELECT * FROM v2_orders
                            WHERE account=? AND (status='OPEN' OR slug=?)""", (account, slug)).fetchall()
        a = db.execute("SELECT * FROM v2_accounts WHERE id=?", (account,)).fetchone()
        existing = next((row for row in own if row["slug"] == slug), None)
        if existing:
            if mode == "SIMULATE" and existing["status"] == "OPEN" and not reason and now > existing["time"]:
                candidates = opportunities(book, a["budget"], up, terms)
                now = time.time() if live_clock else now
                if (now < end and now-details["reference_timestamp"] <= REFERENCE_MAX_AGE
                        and now-details["book_checked"] <= BOOK_CONFIRM_MAX_AGE):
                    def commit_clock():
                        checked = time.time() if live_clock else now
                        if (checked < end and checked-details["reference_timestamp"] <= REFERENCE_MAX_AGE
                                and checked-details["book_checked"] <= BOOK_CONFIRM_MAX_AGE):
                            return checked
                        return None

                    flipped = shadow_reverse(db, existing, a, candidates, book, terms, up, strategy["model"],
                                      now, MIN_EDGE, commit_clock=commit_clock)
                    if flipped:
                        if account == hold_research.ACCOUNT:
                            hold_research.record_flip(db, existing, a, book, terms, details, up, replacement)
                        self.log(db, slug, account, flipped["time"], strategy["model"], up, "SHADOW_FLIP",
                                 "UNFUNDED_AFTER_COST_IMPROVEMENT", key+":flip", dict(details, flip=flipped))
            return
        if mode == "SIMULATE":
            reason = entry_block(a, own, a["budget"], now, daily_cap=False) or reason
        candidates = opportunities(book, a["budget"], up, terms) if not reason else []
        fills = [candidate for candidate in candidates if candidate["ev_rate"] is not None]
        eligible = [candidate for candidate in fills if candidate["ev_rate"] >= MIN_EDGE]
        best = max(eligible, key=lambda candidate: candidate["ev"], default=None)
        audit = dict(details, candidates=candidates, selected=best)
        decision = "WAIT" if mode == "SIMULATE" else "OBSERVE"
        out_of_depth = ",".join(candidate["reason"] for candidate in candidates if candidate["reason"])
        why = reason or ("FAVORABLE_EV" if best else "NO_POSITIVE_EDGE" if fills else out_of_depth)
        if best and mode == "SIMULATE":
            now = time.time() if live_clock else now
            if now >= end:
                return
            if (now - details["reference_timestamp"] > REFERENCE_MAX_AGE
                    or now - details["book_checked"] > BOOK_CONFIRM_MAX_AGE):
                why = "STALE_DURING_EVALUATION"
            else:
                fill = best["fill"]
                if account == hold_research.ACCOUNT:
                    midpoint = (book["bids"][0]["price"]+book["asks"][0]["price"])/2
                    implied = midpoint if best["side"]=="UP" else 1-midpoint
                    stakes = (a["budget"],3*a["budget"])
                    if implied>.5 and best["probability"]>.5:
                        audit["stake_comparison"] = compare_stakes(
                            book,best["side"],best["probability"],terms,MIN_EDGE,stakes)
                    else:
                        audit["stake_comparison"] = [dict(stake=stake,reason="MODEL_MARKET_DISAGREEMENT")
                                                     for stake in stakes]
                inserted = db.execute("""INSERT OR IGNORE INTO v2_orders
                    (account,slug,side,time,model,probability,quantity,cost,fee,status,details,segment)
                    VALUES (?,?,?,?,?,?,?,?,?,'OPEN',?,?)""",
                    (account, slug, best["side"], now, strategy["model"], up, fill["quantity"],
                     fill["total"], fill["fee"], json.dumps(dict(prediction=audit, fill=fill,
                     source="ADAPTIVE PAPER")), strategy["id"]))
                if inserted.rowcount != 1:
                    return
                debit = db.execute("""UPDATE v2_accounts SET balance=balance-?
                                      WHERE id=? AND balance>=? AND halted IS NULL""",
                                   (fill["total"], account, fill["total"]))
                if debit.rowcount != 1:
                    raise ValueError("Critical accounting error: adaptive debit failed")
                if account == hold_research.ACCOUNT:
                    hold_research.record_entry(db,inserted.lastrowid,a,best["side"],book,terms,details)
                decision = best["side"]
        self.log(db, slug, account, now, strategy["model"], up, decision, why, key + ":" + why, audit)
