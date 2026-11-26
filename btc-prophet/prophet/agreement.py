"""Side-by-side counterfactuals based on the captured fixed-time forecasts."""

import json
import math
import time

from . import observer, viability
from .adaptive_entry import BOOK_CONFIRM_MAX_AGE, REFERENCE_MAX_AGE, get_state
from .execution import paper_buy
from .exit_research import ExitResearch
from .model_paper import ModelPaper
from .providers import number, validate_market
from .research import baseline_probability
from .v2risk import CHECKPOINTS

BUDGET = observer.ENTRY_BUDGET
MIN_GAP_BPS = 8.202539307810008
MIN_CONFIDENCE = .65
SCENARIOS = (
    ("control", "Momentum control", None),
    ("agreement", "Agreement", None),
    ("edge_3", "Agreement + 3% edge", .03),
    ("edge_5", "Agreement + 5% edge", .05),
)


def agreement_block(p, side, gap):
    if gap is None:
        return "NO_CLOSED_REFERENCE_CANDLE"
    if max(p,1-p)<MIN_CONFIDENCE:
        return "BELOW_CONFIDENCE_THRESHOLD"
    if abs(gap)<MIN_GAP_BPS:
        return "BELOW_REFERENCE_MOVE_THRESHOLD"
    if (side=="UP")!=(gap>0):
        return "MODEL_AND_PRICE_DISAGREE"
    return None


class AgreementResearch:
    def __init__(self, store, lab):
        self.store, self.lab = store, lab
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS agreement_config (
                    id INTEGER PRIMARY KEY CHECK(id=1), installed REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agreement_research (
                    slug TEXT, checkpoint INTEGER, scenario TEXT, time REAL NOT NULL, model TEXT NOT NULL,
                    prediction_up REAL, direction TEXT, opening_price REAL, candle_close REAL,
                    gap_bps REAL, candle_end REAL, book_timestamp REAL, book_checked REAL,
                    quantity REAL, cost REAL, fee REAL, edge_rate REAL, reason TEXT NOT NULL,
                    PRIMARY KEY(slug,checkpoint,scenario)
                );
                CREATE INDEX IF NOT EXISTS agreement_decisions_time ON v2_decisions(time);
                CREATE INDEX IF NOT EXISTS agreement_research_time ON agreement_research(time);
            """)
            observer.initialize(db)
            viability.initialize(db)
            db.execute("INSERT OR IGNORE INTO agreement_config VALUES (1,?)", (time.time(),))
        self.exits = ExitResearch(store, lab)
        self.model_paper = ModelPaper(store,lab,self.exits)

    def tick(self, now=None):
        self.observe(now)
        self.lab.tick(now)
        self.exits.tick(now)
        self.model_paper.tick(now)

    def observe(self, now=None):
        live_clock = now is None
        now = time.time() if live_clock else now
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            observer.settle(db, now)
            installed = db.execute("SELECT installed FROM agreement_config").fetchone()[0]
            if get_state(db, "mode") not in ("LEARN", "SIMULATE") or get_state(db, "emergency"):
                return
            rows = db.execute("""SELECT p.*, b.payload AS captured_book FROM v2_decisions p
                  LEFT JOIN timing_checkpoint_books b USING(slug,checkpoint)
                  WHERE p.time>=? AND p.time<=? AND NOT EXISTS(
                      SELECT 1 FROM agreement_research r
                      WHERE r.slug=p.slug AND r.checkpoint=p.checkpoint AND r.scenario='control')
                  ORDER BY p.time,p.checkpoint""", (installed, now)).fetchall()
            if not rows:
                return
            market = get_state(db, "market")
            for row in rows:
                self.record(db, market, row, time.time() if live_clock else now)

    @staticmethod
    def record(db, market, row, now):
        details = json.loads(row["details"])
        if details.get("protocol") != "fixed-timing-v1" or row["checkpoint"] not in CHECKPOINTS:
            return
        observed = row["time"]
        slug = row["slug"]
        checkpoint = row["checkpoint"]
        deadline = details["window_start"] + checkpoint + 3
        book = json.loads(row["captured_book"]) if row["captured_book"] else None
        p = gap = opening = close = edge = fill = side = None
        feature_end = details.get("feature_end")
        expected_features = ("momentum_60", "trend_300", "volatility")
        if row["up"] is not None and all(key in details for key in expected_features):
            p = baseline_probability([details["momentum_60"], details["trend_300"],
                                      math.log(max(details["volatility"], 1e-7))])
            side = "UP" if p > .5 else "DOWN" if p < .5 else None

        reason = None
        priced = False
        if row["up"] is None:
            reason = row["reason"]
        elif (now > deadline or not market or market["slug"] != slug
              or not validate_market(market, now)):
            reason = "MISSED_CHECKPOINT"
        elif (not book or book["slug"] != slug or book.get("state") != "MARKET_STATE_OPEN"
              or book["timestamp"] > observed
              or not 0 <= now-(details.get("book_checked") or 0) <= BOOK_CONFIRM_MAX_AGE
              or not 0 <= now-(details.get("reference_timestamp") or 0) <= REFERENCE_MAX_AGE):
            reason = "STALE_OR_MISSING_SNAPSHOT"
        elif p is None or not feature_end or feature_end > observed:
            reason = "INSUFFICIENT_DATA"
        elif not all(key in details for key in ("theta", "tick", "quantum")):
            reason = "INVALID_EXECUTION_TERMS"
        elif side is None:
            reason = "MODEL_TIE_NO_DIRECTION"
        else:
            try:
                opening = number(market["assetPriceTerms"]["priceToBeat"], True)
                fill, reason = paper_buy(book, side, BUDGET, theta=details["theta"],
                                         tick=details["tick"], quantum=details["quantum"])
                priced = True
            except (KeyError, TypeError, ValueError):
                reason = "INVALID_EXECUTION_TERMS"
            last = db.execute("SELECT close FROM candles WHERE source='BRTI' AND time=?",
                              (feature_end-1,)).fetchone()
            if opening and last:
                close = last["close"]
                gap = (close/opening-1)*10000
            if fill:
                side_probability = p if side == "UP" else 1-p
                edge = (side_probability*fill["quantity"]-fill["total"])/fill["total"]

        if (get_state(db, "mode") == "LEARN" and get_state(db, "observer_enabled")
            and row["decision"] in ("UP", "DOWN")
            and market and market["slug"] == slug and validate_market(market, now)
            and not db.execute("SELECT 1 FROM observer_rounds WHERE account=? AND slug=?",
                               (observer.MAIN,slug)).fetchone()):
            main_side = row["decision"]
            main_fill = None
            main_reason = reason
            if reason in (None,"ONE_CENT_QUOTE","NO_EXECUTABLE_DEPTH","UNDERSIZED_FILL","MODEL_TIE_NO_DIRECTION"):
                main_fill,main_reason = paper_buy(book,main_side,BUDGET,theta=details["theta"],
                                                   tick=details["tick"],quantum=details["quantum"])
            observer.record(db,observer.MAIN,slug,observed,now,main_side,main_fill,main_reason or "NO_FILL")

        cap_reason = agreement_block(p,side,gap) if priced else reason
        viability.agreement_entry(db,slug,checkpoint,observed,side,p,gap,cap_reason,book,details)
        values = []
        for scenario, _, threshold in SCENARIOS:
            why = reason
            if not why and scenario != "control":
                why = agreement_block(p,side,gap)
                if not why and threshold is not None and edge < threshold:
                    why = "BELOW_AFTER_COST_EDGE"
            taken = fill if not why else None
            account = f"{scenario}@{checkpoint}"
            observer.record(db,account,slug,observed,now,side if not why else None,taken,why or "NO_FILL")
            values.append((slug, checkpoint, scenario, observed, "momentum-v2", p, side,
                           opening, close, gap, feature_end, details.get("book_timestamp"),
                           details.get("book_checked"), taken["quantity"] if taken else None,
                           taken["total"] if taken else None, taken["fee"] if taken else None,
                           edge, why or "HYPOTHETICAL_FILL"))
        db.executemany("INSERT OR IGNORE INTO agreement_research VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       values)

    def results(self):
        with self.store.connect() as db:
            return summary(db)


def results_sql():
    return """SELECT r.*,v.outcome,v.end AS settlement_end,p.pnl,p.reason AS policy_reason,
               p.quantity AS played_quantity,p.cost AS played_cost,p.fee AS played_fee
               FROM agreement_research r LEFT JOIN verified_markets v ON v.slug=r.slug
               LEFT JOIN observer_rounds p ON p.slug=r.slug AND p.account=r.scenario||'@'||r.checkpoint"""


def summary(db):
    totals = db.execute(f"""WITH outcomes AS ({results_sql()})
        SELECT s.scenario,s.checkpoint,count(*) AS observed,
               sum(s.outcome IS NOT NULL) AS settled_rounds,
               count(s.pnl) AS trades,
               sum(coalesce(s.pnl>0,0)) AS wins,
               sum(coalesce(s.pnl<0,0)) AS losses,
               sum(s.outcome IS NOT NULL AND s.played_quantity IS NULL) AS skips,
               sum(s.pnl) AS net_pnl,
               sum(s.pnl IS NOT NULL OR c.pnl IS NOT NULL) AS paired_active,
               sum(CASE WHEN s.outcome IS NOT NULL THEN coalesce(s.pnl,0)-coalesce(c.pnl,0) END) AS vs_control
        FROM outcomes s JOIN outcomes c ON c.slug=s.slug AND c.checkpoint=s.checkpoint
               AND c.scenario='control'
        GROUP BY s.scenario,s.checkpoint""").fetchall()
    lookup = {(r["checkpoint"], r["scenario"]):dict(r) for r in totals}
    empty = dict(observed=0,settled_rounds=0,trades=0,wins=0,losses=0,skips=0,
                 net_pnl=None,paired_active=0,vs_control=None)
    rows = []
    for checkpoint in CHECKPOINTS:
        for scenario, label, threshold in SCENARIOS:
            stats = dict(lookup.get((checkpoint,scenario), empty))
            state = db.execute("SELECT cash FROM observer_accounts WHERE account=?",
                               (f"{scenario}@{checkpoint}",)).fetchone()
            stats.update(cash=state["cash"] if state else observer.STARTING_CASH,
                         starting_cash=observer.STARTING_CASH,daily_limit=observer.DAILY_LIMIT,
                         streak_limit=observer.LOSS_STREAK)
            stats.update(checkpoint=checkpoint,scenario=scenario,label=label,budget=BUDGET,
                         min_edge=threshold,win_rate=stats["wins"]/stats["trades"] if stats["trades"] else None)
            rows.append(stats)
    return rows
