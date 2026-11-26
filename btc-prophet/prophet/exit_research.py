"""Matched, unfunded exits from the same recorded paper entries."""
from hashlib import sha256
import json
import math
import time
from collections import defaultdict

from .adaptive_entry import MIN_EDGE, freshness, get_state, snapshot
from .autonomous import replacement
from .execution import execute
from .lab import MODEL_ERRORS
from .consensus import vote
from .models import BASE_PARTICIPANTS, VOTE_VERSION
from .pattern_odds import VERSION, forecast_from_snapshot
from .providers import number, validate_market
from .v2paper import MODEL

TARGETS = (1.2, 1.3, 1.4, 1.5, 1.6)
POLICIES = ("HOLD", *(f"TAKE_PROFIT_{target}" for target in TARGETS), "PROBABILITY_EXIT", "FLIP")
SOURCES = ("adaptive", "fixed")


class ExitResearch:
    def __init__(self, store, lab):
        self.store = store
        self.lab = lab
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS exit_research_cursor (
                    source TEXT PRIMARY KEY, last_id INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS exit_research_entries (
                    source TEXT, order_id INTEGER, slug TEXT NOT NULL, configuration TEXT NOT NULL,
                    model TEXT NOT NULL, checkpoint INTEGER, stake REAL NOT NULL, entry_time REAL NOT NULL,
                    window_end REAL NOT NULL, side TEXT NOT NULL, quantity REAL NOT NULL,
                    cost REAL NOT NULL, fee REAL NOT NULL, terms TEXT NOT NULL, entry_details TEXT NOT NULL,
                    PRIMARY KEY(source,order_id)
                );
                CREATE TABLE IF NOT EXISTS exit_research_results (
                    source TEXT, order_id INTEGER, policy TEXT, side TEXT NOT NULL,
                    quantity REAL NOT NULL, credit REAL NOT NULL DEFAULT 0, debit REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'OPEN', eligible_quotes INTEGER NOT NULL DEFAULT 0,
                    usable_forecasts INTEGER NOT NULL DEFAULT 0, last_book TEXT,
                    acted_at REAL, action TEXT, evidence TEXT, outcome TEXT, settled REAL, pnl REAL,
                    PRIMARY KEY(source,order_id,policy)
                );
                CREATE INDEX IF NOT EXISTS exit_research_pending ON exit_research_results(source,order_id)
                    WHERE status != 'SETTLED';
            """)
            db.execute("INSERT OR IGNORE INTO exit_research_cursor SELECT 'adaptive',coalesce(max(id),0) FROM v2_orders")
            db.execute("INSERT OR IGNORE INTO exit_research_cursor SELECT 'fixed',coalesce(max(id),0) FROM lab_orders")

    def capture(self, db, source):
        last = db.execute("SELECT last_id FROM exit_research_cursor WHERE source=?", (source,)).fetchone()[0]
        if source == "adaptive":
            entries = db.execute("""SELECT o.*,o.account AS configuration,a.budget AS stake,NULL AS checkpoint
                FROM v2_orders o JOIN v2_accounts a ON a.id=o.account
                WHERE o.id>? AND o.account LIKE 'adaptive-%' ORDER BY o.id""", (last,)).fetchall()
        else:
            entries = db.execute("""SELECT o.*,c.id AS configuration,c.stake,c.checkpoint,c.version AS model,
                o.fees AS fee FROM lab_orders o JOIN lab_configs c ON c.id=o.config
                WHERE o.id>? AND c.exit='HOLD' AND c.id LIKE 'timing/v2/%' ORDER BY o.id""", (last,)).fetchall()
        for row in entries:
            audit = json.loads(row["details"] or "{}")
            fill = audit.get("fill") or {}
            terms = fill.get("assumptions") or {}
            end = (audit.get("prediction") or {}).get("window_end")
            if end is None or not all(key in terms for key in ("theta","tick","quantum")):
                continue
            self.add_entry(db,source,row["id"],row["slug"],row["configuration"],row["model"],
                           row["checkpoint"],row["stake"],row["time"],end,row["side"],row["quantity"],
                           row["cost"],row["fee"],{key:terms[key] for key in ("theta","tick","quantum")},audit)
        if entries:
            db.execute("UPDATE exit_research_cursor SET last_id=? WHERE source=?", (entries[-1]["id"],source))

    @staticmethod
    def add_entry(db, source, order_id, slug, configuration, model, checkpoint, stake,
                  entry_time, window_end, side, quantity, cost, fee, terms, audit):
        if db.execute("""INSERT OR IGNORE INTO exit_research_entries VALUES
                         (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (source,order_id,slug,configuration,model,checkpoint,stake,entry_time,
                       window_end,side,quantity,cost,fee,json.dumps(terms),json.dumps(audit))).rowcount:
            db.executemany("""INSERT INTO exit_research_results(source,order_id,policy,side,quantity)
                VALUES (?,?,?,?,?)""", ((source,order_id,policy,side,quantity) for policy in POLICIES))

    @classmethod
    def observe_autonomous(cls, db, order_id, now, up, book, data, observation):
        rows = db.execute("""SELECT e.*,r.policy FROM exit_research_entries e
            JOIN exit_research_results r USING(source,order_id)
            WHERE e.source='autonomous' AND e.order_id=? AND r.status='OPEN' AND r.policy!='HOLD'""",
            (order_id,)).fetchall()
        if not rows:
            return
        entry = rows[0]
        terms = json.loads(entry["terms"])
        sale = execute(book,entry["side"],quantity=entry["quantity"],buying=False,**terms)
        for row in rows:
            action, evidence = cls.evaluate(row,up,book,terms,sale)
            cls.apply_observation(db,row,observation,up,action,evidence,now,book,data)

    @staticmethod
    def apply_observation(db,row,quote_key,p,action,evidence,now,book,data):
        side,qty,credit,debit = (row["side"],row["quantity"],0,0)
        if action:
            side,qty,credit,debit = action
            evidence.update(up=p,book_timestamp=book["timestamp"],reference=data["reference"],
                            book_confirmed=data["health_market"]["checked"],
                            bid=book["bids"][0]["price"],ask=book["asks"][0]["price"])
        db.execute("""UPDATE exit_research_results SET last_book=?, eligible_quotes=eligible_quotes+1,
            usable_forecasts=usable_forecasts+?, side=?, quantity=?, credit=?, debit=?,
            acted_at=?, action=?, evidence=?, status=?
            WHERE source=? AND order_id=? AND policy=? AND status='OPEN' AND last_book IS NOT ?""",
            (quote_key,int(p is not None),side,qty,credit,debit,now if action else None,
             ("FLIP" if debit else "EXIT") if action else None,json.dumps(evidence) if action else None,
             "ACTED" if action else "OPEN",row["source"],row["order_id"],row["policy"],quote_key))

    @staticmethod
    def settle(db, now):
        db.execute("""UPDATE exit_research_results AS r SET status='SETTLED', outcome=v.outcome,
            settled=?, pnl=r.credit-r.debit-e.cost+CASE WHEN r.side=v.outcome THEN r.quantity ELSE 0 END
            FROM exit_research_entries e JOIN verified_markets v ON v.slug=e.slug
            WHERE r.source=e.source AND r.order_id=e.order_id AND r.status!='SETTLED'
                AND v.end=e.window_end AND v.end<=? AND v.verified_at<=?""", (now,now,now))

    def tick(self, now=None):
        live_clock = now is None
        now = time.time() if live_clock else now
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for source in SOURCES:
                self.capture(db,source)
            self.settle(db,now)
            if get_state(db,"mode") != "SIMULATE" or get_state(db,"emergency"):
                return
            data = snapshot(db)
            market, book = data.get("market") or {}, data.get("book") or {}
            slug = market.get("slug")
            if (freshness(data,now) or not validate_market(market,now) or book.get("slug")!=slug
                    or book.get("state")!="MARKET_STATE_OPEN" or not book.get("bids") or not book.get("asks")):
                return
            rows = db.execute("""SELECT e.*,r.policy FROM exit_research_results r
                JOIN exit_research_entries e USING(source,order_id)
                WHERE r.status='OPEN' AND r.policy!='HOLD' AND e.slug=? AND e.entry_time<? AND e.window_end>?""",
                (slug,now,now)).fetchall()
        if not rows:
            return
        try:
            baseline, features = self.lab.paper.probability(now,book=book)
        except MODEL_ERRORS:
            baseline,features = None,{}
        estimates = {}
        for row in rows:
            key = (row["source"],row["model"],row["checkpoint"])
            if key not in estimates:
                estimates[key] = self.estimate(row, now, baseline, features, data)
        book_key = sha256(json.dumps(book, sort_keys=True).encode()).hexdigest()
        priced = {}
        amendments = []
        for row in rows:
            p = estimates[row["source"],row["model"],row["checkpoint"]]
            quote_key = f"{book_key}:{p}"
            key = (row["source"],row["order_id"])
            if key not in priced:
                terms=json.loads(row["terms"])
                sale=execute(book,row["side"],quantity=row["quantity"],buying=False,**terms)
                priced[key] = terms,sale
            terms,sale=priced[key]
            action,evidence = self.evaluate(row,p,book,terms,sale)
            amendments.append((row,quote_key,p,action,evidence))
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            checked = time.time() if live_clock else now
            latest = snapshot(db)
            if (get_state(db,"mode")!="SIMULATE" or get_state(db,"emergency") or freshness(latest,checked)
                    or any(latest.get(key)!=data.get(key) for key in ("market","book","reference"))
                    or not validate_market(market,checked)):
                return
            for row,quote_key,p,action,evidence in amendments:
                if latest["health_market"]["checked"] <= row["entry_time"]:
                    continue
                self.apply_observation(db,row,quote_key,p,action,evidence,checked,book,latest)

    def estimate(self, row, now, baseline, features, data):
        if row["source"]=="adaptive" and row["model"]==MODEL:
            return baseline
        if not all(key in features for key in ("momentum_60","trend_300","volatility")):
            return None
        if row["model"]==VOTE_VERSION:
            with self.store.connect() as db:
                record = db.execute("SELECT evidence FROM model_vote_forecasts WHERE slug=? AND checkpoint=?",
                                    (row["slug"],row["checkpoint"])).fetchone()
            components = {}
            for family, component in (json.loads(record[0])["components"].items() if record else ()):
                p = self.estimate(dict(row, model=component["version"]),now,baseline,features,data)
                if p is not None:
                    components[family] = {"up":p}
            return vote(components,BASE_PARTICIPANTS)["up_share"]
        try:
            if row["model"]==VERSION:
                book=data["book"]
                opening=number(data["market"]["assetPriceTerms"]["priceToBeat"],True)
                context=dict(features,**json.loads(row["terms"]),reference_price=data["reference"]["price"],
                             window_end=row["window_end"],bid_depth=sum(x["quantity"] for x in book["bids"]),
                             ask_depth=sum(x["quantity"] for x in book["asks"]))
                p,_=forecast_from_snapshot(self.store,row["slug"],now,context,book,opening)
            else:
                x=[features["momentum_60"],features["trend_300"],math.log(max(features["volatility"],1e-7))]
                p=self.lab.predict(row["model"],x,row["checkpoint"] or 90,row["slug"],now)
        except (*MODEL_ERRORS, KeyError):
            return None
        return p if p is not None and math.isfinite(p) and 0<=p<=1 else None

    @staticmethod
    def evaluate(row, p, book, terms, sale):
        if sale["quantity"] < row["quantity"]-1e-9:
            return None, {}
        policy=row["policy"]
        net=sale["total"]
        audit=dict(sale=sale,terms=terms)
        if policy.startswith("TAKE_PROFIT_"):
            target=float(policy.rsplit("_",1)[1])*row["cost"]
            if target <= row["quantity"] and net >= target:
                return (row["side"],0,net,0),dict(audit,target_net=target)
        elif p is not None:
            prob=p if row["side"]=="UP" else 1-p
            hold_ev=prob*row["quantity"]
            margin=MIN_EDGE*row["cost"]
            if policy=="PROBABILITY_EXIT" and net >= hold_ev+margin:
                return (row["side"],0,net,0),dict(audit,hold_ev=hold_ev,minimum_improvement=margin)
            if policy=="FLIP" and net>0:
                # The opposite buy must fit within the modeled exit proceeds. Both
                # legs consume the same displayed side and share its depth.
                sold,bought,_=replacement(book,row["side"],row["quantity"],min(net,row["stake"]),terms)
                if bought and bought["total"] <= sold["total"]:
                    flip_ev=sold["total"]-bought["total"]+(1-prob)*bought["quantity"]
                    if flip_ev >= max(hold_ev,net)+margin:
                        side="DOWN" if row["side"]=="UP" else "UP"
                        return (side,bought["quantity"],sold["total"],bought["total"]),dict(
                            audit,hold_ev=hold_ev,flip_ev=flip_ev,minimum_improvement=margin,buy=bought)
        return None, {}


def matched_rows(db):
    return db.execute("""SELECT e.*,r.policy,r.status,r.eligible_quotes,r.usable_forecasts,r.acted_at,r.action,
        r.side AS final_side,r.quantity AS final_quantity,r.credit,r.debit,r.outcome,r.settled,r.pnl,r.evidence,
        h.pnl AS hold_pnl,r.pnl-h.pnl AS improvement FROM exit_research_results r
        JOIN exit_research_entries e USING(source,order_id)
        JOIN exit_research_results h ON h.source=r.source AND h.order_id=r.order_id AND h.policy='HOLD'
        ORDER BY e.entry_time,e.source,e.order_id,r.policy""")


def summarize(rows):
    groups = defaultdict(lambda: dict(entries=0,pairs=0,actions=0,quoted=0,forecast=0,hold=0.0,
                                      pnl=0.0,improvement=0.0,better=0,worse=0))
    for r in rows:
        group = groups[r["source"],r["model"],r["checkpoint"],r["stake"],r["policy"]]
        group["entries"] += 1
        if r["improvement"] is None:
            continue
        group["pairs"] += 1
        group["actions"] += r["acted_at"] is not None
        group["quoted"] += r["eligible_quotes"] > 0
        group["forecast"] += r["usable_forecasts"] > 0
        group["hold"] += r["hold_pnl"]
        group["pnl"] += r["pnl"]
        group["improvement"] += r["improvement"]
        group["better"] += r["improvement"] > 0
        group["worse"] += r["improvement"] < 0
    for dimensions, g in groups.items():
        yield [*dimensions,g["entries"],g["pairs"],g["actions"],g["quoted"],g["forecast"],
               g["hold"] if g["pairs"] else None,g["pnl"] if g["pairs"] else None,
               g["improvement"] if g["pairs"] else None,g["better"],g["worse"]]
