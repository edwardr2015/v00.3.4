"""Independent, continuously evaluated paper accounts for model research."""
from hashlib import sha256
import json
import math
import time

from .analytics import streaks
from . import pooled_consensus, selective_forecast, viability
from .adaptive_entry import MIN_EDGE, freshness, get_state, opportunities, snapshot
from .consensus import vote
from .execution import compare_stakes, execute
from .models import BASE_PARTICIPANTS, PARTICIPANTS, VOTE_FAMILY, VOTE_VERSION
from .providers import number, timestamp, validate_market
from .v2risk import CHECKPOINTS

STARTING_CASH = 1000
STAKE = 3
SIZING_CHECKPOINT = 45
SHADOW_STAKES = (STAKE, 3 * STAKE)
REFERENCE_GAP = "volatility_reference_gap"
REFERENCE_AGREEMENT = "reference_gap_agreement"
HIGH_RETURN_BUDGET = 20
HIGH_RETURN_MIN_EDGE = 0.10
CHALLENGERS = {
    "market_odds_adjustment": "Market Odds Adjustment",
    REFERENCE_GAP: "Volatility-Adjusted Reference Gap",
}
POOLED_MODELS = (*BASE_PARTICIPANTS, *CHALLENGERS)
TIMED_FAMILIES = {**PARTICIPANTS, **CHALLENGERS}
FAMILIES = {**TIMED_FAMILIES, REFERENCE_AGREEMENT: "Reference Gap + Market Agreement"}
FORMULA_VERSIONS = {family: f"{family.replace('_','-')}-v1"
                    for family in (*CHALLENGERS, REFERENCE_AGREEMENT)}


def probabilities_from_market(midpoint, reference, opening, volatility, seconds, momentum):
    """Two frozen research hypotheses. Calibration must be measured forward."""
    sigma = max(1e-5,volatility * math.sqrt(max(1,seconds/15)))
    gap = math.log(reference/opening) / sigma
    p_gap = min(.98,max(.02,.5*(1+math.erf(gap/math.sqrt(2)))))
    p_market = .70*midpoint + .20*p_gap + .10*momentum
    return dict(market_odds_adjustment=p_market,volatility_reference_gap=p_gap)


def decide(position, cash, book, terms, up, blocked, *, require_agreement=False):
    """Return one action plus the executable inputs that justify it."""
    if blocked or up is None:
        return ("HOLD" if position else "SKIP"), blocked or "MODEL_NOT_READY", {}
    if position:
        entry_terms = json.loads(position["terms"])
        sale = execute(book,position["side"],quantity=position["quantity"],buying=False,**entry_terms)
        hold_ev = position["quantity"] * (up if position["side"]=="UP" else 1-up)
        audit = dict(sale=sale,hold_ev=hold_ev,required_improvement=MIN_EDGE*position["cost"])
        if sale["quantity"]+1e-9 < position["quantity"]:
            return "HOLD","FULL_EXIT_DEPTH_UNAVAILABLE",audit
        if sale["total"] >= hold_ev + audit["required_improvement"]:
            return "CASH_OUT","EXECUTABLE_EXIT_ABOVE_HOLD_EV",audit
        return "HOLD","HOLD_EV_FAVORED",audit
    if cash < STAKE:
        return "SKIP","INSUFFICIENT_VIRTUAL_CASH",{}
    market_up = None
    favored = None
    if require_agreement:
        market_up = (book["bids"][0]["price"] + book["asks"][0]["price"]) / 2
        if up == .5 or market_up == .5 or (up > .5) != (market_up > .5):
            return "SKIP", "MODEL_MARKET_DISAGREEMENT", dict(market_up=market_up)
        favored = "UP" if up > .5 else "DOWN"
    candidates = opportunities(book,STAKE,up,terms)
    eligible = [c for c in candidates if c["ev_rate"] is not None and c["ev_rate"]>=MIN_EDGE
                and (favored is None or c["side"] == favored)]
    best = max(eligible,key=lambda candidate:candidate["ev"],default=None)
    audit = dict(candidates=candidates,selected=best)
    if require_agreement:
        audit["market_up"] = market_up
    return (best["side"],"EXECUTABLE_ENTRY_ABOVE_MINIMUM_EV",audit) if best else (
        "SKIP","NO_EXECUTABLE_EDGE",audit)


def shadow_stake_fills(votes, book, terms, blocked=None):
    """Price separate $3 and $9 trials against the same observed depth."""
    votes = votes or {}
    mean_up = votes.get("mean_up")
    unanimous = 8 in (votes.get("up_votes"), votes.get("down_votes")) and not votes.get("missing")
    if blocked or not unanimous or mean_up is None:
        return [dict(stake=stake,reason=blocked or "NO_UNANIMOUS_VOTE") for stake in SHADOW_STAKES]
    side = "UP" if votes["up_votes"] == 8 else "DOWN"
    probability = mean_up if side == "UP" else 1-mean_up
    return compare_stakes(book,side,probability,terms,MIN_EDGE,SHADOW_STAKES)


def reference_gap_stake(book, terms, selected, votes):
    """Test a larger shadow fill on the existing Reference Gap entry signal."""
    side = selected["side"]
    probability = selected["probability"]
    midpoint = (book["bids"][0]["price"] + book["asks"][0]["price"]) / 2
    implied = midpoint if side == "UP" else 1 - midpoint
    votes = votes or {}
    trial, = compare_stakes(book, side, probability, terms, HIGH_RETURN_MIN_EDGE, (HIGH_RETURN_BUDGET,))
    if probability <= .5 or implied <= .5:
        trial["reason"] = "MODEL_MARKET_DISAGREEMENT"
    elif trial["reason"] == "NO_POSITIVE_EDGE":
        trial["reason"] = "BELOW_10_PERCENT_EXPECTED_RETURN"
    return dict(trial, market_probability=implied,
                votes_for_side=votes.get("up_votes" if side == "UP" else "down_votes"),
                missing_votes=votes.get("missing"))


class ModelPaper:
    def __init__(self, store, lab, estimates):
        self.store,self.lab,self.estimates = store,lab,estimates
        with store.connect() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS model_paper_accounts(
                family TEXT PRIMARY KEY, cash REAL NOT NULL CHECK(cash>=0));
              CREATE TABLE IF NOT EXISTS model_paper_windows(
                slug TEXT PRIMARY KEY, start REAL NOT NULL, end REAL NOT NULL,
                versions TEXT NOT NULL, closed REAL);
              CREATE TABLE IF NOT EXISTS model_paper_decisions(
                id INTEGER PRIMARY KEY, family TEXT NOT NULL, slug TEXT NOT NULL, time REAL NOT NULL,
                version TEXT, up REAL, action TEXT NOT NULL CHECK(action IN ('UP','DOWN','HOLD','SKIP','CASH_OUT')),
                reason TEXT NOT NULL, observation TEXT NOT NULL, evidence TEXT NOT NULL,
                UNIQUE(family,slug,observation));
              CREATE TABLE IF NOT EXISTS model_paper_positions(
                family TEXT, slug TEXT, version TEXT NOT NULL, side TEXT NOT NULL, entered REAL NOT NULL,
                quantity REAL NOT NULL, cost REAL NOT NULL, terms TEXT NOT NULL, entry_fee REAL NOT NULL,
                status TEXT NOT NULL, closed REAL, outcome TEXT, proceeds REAL, exit_fee REAL, pnl REAL,
                PRIMARY KEY(family,slug));
              CREATE TABLE IF NOT EXISTS model_paper_timing (
                slug TEXT, family TEXT, checkpoint INTEGER, version TEXT, time REAL NOT NULL,
                side TEXT, up REAL, quantity REAL, cost REAL, fee REAL, reason TEXT, evidence TEXT,
                PRIMARY KEY(slug,family,checkpoint));
              CREATE UNIQUE INDEX IF NOT EXISTS model_paper_one_open
                ON model_paper_positions(family) WHERE status='OPEN';
              CREATE INDEX IF NOT EXISTS model_paper_calibration ON model_paper_positions(family,version,slug,entered);
              CREATE INDEX IF NOT EXISTS model_paper_unverified
                ON model_paper_positions(slug) WHERE outcome IS NULL;
              CREATE INDEX IF NOT EXISTS model_paper_final_skips
                ON model_paper_decisions(family) WHERE observation='final';
              CREATE INDEX IF NOT EXISTS model_paper_decisions_latest ON model_paper_decisions(family,id DESC);
              CREATE INDEX IF NOT EXISTS model_paper_windows_pending ON model_paper_windows(end) WHERE closed IS NULL;
            """)
            viability.initialize(db)
            pooled_consensus.initialize(db)
            selective_forecast.initialize(db)
            db.executemany("INSERT OR IGNORE INTO model_paper_accounts VALUES (?,?)",
                           ((family,STARTING_CASH) for family in FAMILIES))

            for row in db.execute("""SELECT p.*,w.end,d.id AS entry_id,d.evidence
                FROM model_paper_positions p JOIN model_paper_windows w USING(slug)
                JOIN model_paper_decisions d ON d.family=p.family AND d.slug=p.slug
                  AND d.time=p.entered AND d.action=p.side
                WHERE p.status='OPEN'""").fetchall():
                self.estimates.add_entry(db,"autonomous",row["entry_id"],row["slug"],row["family"],
                    row["version"],None,STAKE,row["entered"],row["end"],row["side"],row["quantity"],
                    row["cost"],row["entry_fee"],json.loads(row["terms"]),json.loads(row["evidence"]))

    @staticmethod
    def versions(db, now):
        versions = dict(db.execute("""SELECT c.family,c.version FROM lab_configs c
            JOIN model_registry m ON m.version=c.version
            WHERE c.checkpoint=90 AND c.stake=1 AND c.exit='HOLD' AND c.status='FORWARD SHADOW'
                AND (m.trained IS NULL OR m.trained<=?)
            ORDER BY c.created,c.id""", (now,)))
        return {**versions,**FORMULA_VERSIONS}

    def settle(self, db, now):
        for row in db.execute("""SELECT p.*,v.outcome AS result FROM model_paper_positions p
            JOIN model_paper_windows w USING(slug) JOIN verified_markets v ON v.slug=p.slug AND v.end=w.end
            WHERE p.outcome IS NULL AND v.verified_at<=? AND v.end<=?""",(now,now)).fetchall():
            if row["status"]=="OPEN":
                payout = row["quantity"] if row["side"]==row["result"] else 0
                self.close(db,row,now,"SETTLED",payout,0,row["result"])
            else:
                db.execute("UPDATE model_paper_positions SET outcome=? WHERE family=? AND slug=?",
                           (row["result"],row["family"],row["slug"]))

    @staticmethod
    def close(db, row, now, status, proceeds, fee, outcome=None):
        changed=db.execute("""UPDATE model_paper_positions SET status=?,closed=?,outcome=?,proceeds=?,exit_fee=?,pnl=?
                      WHERE family=? AND slug=? AND status='OPEN'""",
                   (status,now,outcome,proceeds,fee,proceeds-row["cost"],row["family"],row["slug"])).rowcount
        if changed:
            db.execute("UPDATE model_paper_accounts SET cash=cash+? WHERE family=?",(proceeds,row["family"]))

    def finalize(self, db, now):
        for window in db.execute("SELECT * FROM model_paper_windows WHERE closed IS NULL AND end<=?",(now,)).fetchall():
            versions = json.loads(window["versions"])
            for family in FAMILIES:
                if family == REFERENCE_AGREEMENT and family not in versions:
                    continue
                if db.execute("SELECT 1 FROM model_paper_positions WHERE family=? AND slug=?",
                              (family,window["slug"])).fetchone():
                    continue
                last = db.execute("""SELECT reason FROM model_paper_decisions WHERE family=? AND slug=?
                    ORDER BY id DESC LIMIT 1""",(family,window["slug"])).fetchone()
                db.execute("""INSERT OR IGNORE INTO model_paper_decisions
                    (family,slug,time,version,action,reason,observation,evidence)
                    VALUES (?,?,?,?,'SKIP','WINDOW_CLOSED','final',?)""",
                    (family,window["slug"],now,versions.get(family),
                     json.dumps(dict(last_reason=last[0] if last else None,window_end=window["end"]))))
            db.execute("UPDATE model_paper_windows SET closed=? WHERE slug=?",(now,window["slug"]))

    def tick(self, now=None):
        live_clock = now is None
        now = time.time() if live_clock else now
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.settle(db,now)
            self.finalize(db,now)
            pooled_consensus.finalize(db,now)
            selective_forecast.close_missed(db,now)
            mode = get_state(db,"mode")
            if mode not in ("LEARN","SIMULATE") or get_state(db,"emergency"):
                return
            experiment = get_state(db,"v2_experiment")
            if experiment and now>=experiment["end"]:
                return
            data = snapshot(db)
            selective_forecast.observe(db,data,now,model_fills=mode=="SIMULATE")
            if mode != "SIMULATE":
                return
            market,book = data.get("market") or {},data.get("book") or {}
            if not validate_market(market,now):
                return
            slug = market["slug"]
            start,end = (timestamp(market["assetPriceTerms"][key]) for key in ("windowStart","windowEnd"))
            existing = db.execute("SELECT versions FROM model_paper_windows WHERE slug=?",(slug,)).fetchone()
            versions = json.loads(existing[0]) if existing else self.versions(db,now)
        reason = freshness(data,now)
        if not reason and (book.get("slug")!=slug or book.get("state")!="MARKET_STATE_OPEN"
                            or not book.get("bids") or not book.get("asks")):
            reason = "BOOK_UNAVAILABLE"
        terms,opening = {},None
        try:
            opening = number(market["assetPriceTerms"]["priceToBeat"],True)
            terms = dict(theta=max(.095,number(market.get("feeCoefficient",.095),True)),
                         tick=number(market["orderPriceMinTickSize"],True),quantum=number(market["minimumTradeQty"],True))
            if terms["theta"]>1 or terms["tick"]>=1 or terms["quantum"]>100:
                raise ValueError("Unsupported execution terms")
        except (ValueError,TypeError,KeyError):
            reason = reason or "INVALID_EXECUTION_TERMS"
        context = dict(opening=opening, close=end, start=start)
        ps,evidence = ({},{}) if reason else self.forecast(versions,now,slug,book,terms,data,context)
        ps = {family:p for family,p in ps.items() if p is not None and math.isfinite(p) and 0<=p<=1}
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            checked = time.time() if live_clock else now
            latest = snapshot(db)
            if (get_state(db,"mode")!="SIMULATE" or get_state(db,"emergency") or not validate_market(market,checked)
                    or any(latest.get(key)!=data.get(key) for key in ("market","book","reference"))):
                return
            experiment = get_state(db,"v2_experiment")
            if experiment and checked>=experiment["end"]:
                return
            reason = freshness(latest,checked) or reason
            serialized = json.dumps(versions,sort_keys=True)
            db.execute("INSERT OR IGNORE INTO model_paper_windows VALUES (?,?,?,?,NULL)",(slug,start,end,serialized))
            if db.execute("SELECT versions FROM model_paper_windows WHERE slug=?",(slug,)).fetchone()[0]!=serialized:
                return
            identity = sha256(json.dumps([book,data.get("reference"),ps,evidence,reason],sort_keys=True).encode()).hexdigest()
            if not reason:
                key = sha256(json.dumps([book,data["reference"],evidence],sort_keys=True).encode()).hexdigest()
                evidence["market_conditions"] = viability.observe(db,slug,key,checked,book=book,
                    ref=data["reference"],opening=opening,features=evidence.get("features") or {},
                    votes=evidence.get("vote") or {},tick=terms["tick"])
            pooled_consensus.observe(db, slug, start, end, checked, versions, ps, POOLED_MODELS, book, terms,
                                     MIN_EDGE, reason, book_confirmed=(latest.get("health_market") or {}).get("checked"),
                                     reference_time=(latest.get("reference") or {}).get("timestamp"), compare=True)
            self.record_timing(db,slug,now,checked,start,versions,ps,reason,book,terms,latest,evidence.get("vote"))
            for family in FAMILIES:
                if family == REFERENCE_AGREEMENT and family not in versions:
                    continue
                self.record(db,family,slug,checked,versions.get(family),ps.get(family),identity,reason,
                            book,terms,latest,context,evidence)

    @staticmethod
    def record_timing(db, slug, observed, checked, start, versions, forecasts, reason, book, terms, data, votes):
        captured = {r[0] for r in db.execute("SELECT DISTINCT checkpoint FROM model_paper_timing WHERE slug=?",(slug,))}
        for checkpoint in CHECKPOINTS:
            if checkpoint in captured or observed < start+checkpoint:
                continue
            blocked = "MISSED_CHECKPOINT" if checked > start+checkpoint+3 else reason
            for family in TIMED_FAMILIES:
                up = forecasts.get(family) if not blocked else None
                side,why,audit = decide(None,STAKE,book,terms,up,blocked)
                fill = (audit.get("selected") or {}).get("fill") or {}
                evidence = dict(audit,book_timestamp=book.get("timestamp"),
                                book_confirmed=(data.get("health_market") or {}).get("checked"))
                if family == VOTE_FAMILY and checkpoint == SIZING_CHECKPOINT:
                    evidence["stake_comparison"] = shadow_stake_fills(
                        votes, book, terms, blocked or ("MODEL_NOT_READY" if up is None else None))
                db.execute("INSERT INTO model_paper_timing VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (slug,family,checkpoint,versions.get(family),checked,side if fill else None,up,
                     fill.get("quantity"),fill.get("total"),fill.get("fee"),why,json.dumps(evidence)))

    def forecast(self, versions, now, slug, book, terms, data, context):
        from .lab import MODEL_ERRORS

        try:
            baseline,features = self.lab.paper.probability(now,book=book)
        except MODEL_ERRORS:
            baseline,features = None,{}
        ps = {}
        for family in BASE_PARTICIPANTS:
            version = versions.get(family)
            if version:
                ps[family] = self.estimates.estimate(dict(source="fixed",model=version,slug=slug,checkpoint=90,
                    window_end=context["close"],terms=json.dumps(terms)),now,baseline,features,data)
        vote_evidence = vote({family:dict(up=p,version=versions[family]) for family,p in ps.items()
                             if p is not None},BASE_PARTICIPANTS)
        if versions.get(VOTE_FAMILY)==VOTE_VERSION:
            ps[VOTE_FAMILY] = vote_evidence["up_share"]
        volatility = features.get("volatility")
        reference = data["reference"].get("price")
        if baseline is not None and volatility and reference and reference>0:
            midpoint=(book["bids"][0]["price"]+book["asks"][0]["price"])/2
            ps.update(probabilities_from_market(midpoint,reference,context["opening"],volatility,
                                               context["close"]-now,baseline))
        if REFERENCE_AGREEMENT in versions and REFERENCE_GAP in ps:
            ps[REFERENCE_AGREEMENT] = ps[REFERENCE_GAP]
        return ps,dict(features=features,vote=vote_evidence)

    def record(self, db, family, slug, now, version, up, observation, reason, book, terms, data, context, forecast):
        previous = db.execute("SELECT time FROM model_paper_decisions WHERE family=? ORDER BY id DESC LIMIT 1",
                              (family,)).fetchone()
        if previous and previous["time"]>now:
            return
        existing = db.execute("SELECT * FROM model_paper_positions WHERE family=? AND slug=?",(family,slug)).fetchone()
        if existing and existing["status"]!="OPEN":
            return
        if not existing and db.execute("SELECT 1 FROM model_paper_positions WHERE family=? AND status='OPEN'",
                                       (family,)).fetchone():
            reason = "PRIOR_POSITION_AWAITING_SETTLEMENT"
        if existing and (now<=existing["entered"] or (data.get("health_market") or {}).get("checked",0)<=existing["entered"]):
            return
        cash = db.execute("SELECT cash FROM model_paper_accounts WHERE family=?",(family,)).fetchone()[0]
        can_observe_exit = existing and not reason
        action,reason,audit = decide(existing,cash,book,terms,up,reason,
                                     require_agreement=family == REFERENCE_AGREEMENT)
        if existing:
            audit.update(shadow_exit_action=action, shadow_exit_reason=reason)
            action,reason = "HOLD","HOLD_TO_SETTLEMENT_POLICY"
        trail = dict(audit,features=forecast.get("features"),market_conditions=forecast.get("market_conditions"),
                     vote=forecast.get("vote") if family==VOTE_FAMILY else None,
                     market_open=context["start"],market_close=context["close"],reference_open=context["opening"],
                     reference=data.get("reference"),book_timestamp=book.get("timestamp"),
                     book_checked=(data.get("health_market") or {}).get("checked"),cash_before=cash,terms=terms,
                     bid=(book.get("bids") or [{}])[0],ask=(book.get("asks") or [{}])[0])
        if action in ("UP","DOWN"):
            trail["calibration_shadow"] = viability.calibrate_entry(db,family,version,slug,up,now)
            if family == REFERENCE_GAP:
                trail["high_return_shadow"] = reference_gap_stake(book, terms, audit["selected"], forecast.get("vote"))
        recorded = db.execute("""INSERT OR IGNORE INTO model_paper_decisions
            (family,slug,time,version,up,action,reason,observation,evidence) VALUES (?,?,?,?,?,?,?,?,?)""",
            (family,slug,now,version,up,action,reason,observation,json.dumps(trail)))
        if not recorded.rowcount:
            return
        if can_observe_exit:
            entry = db.execute("""SELECT id FROM model_paper_decisions
                WHERE family=? AND slug=? AND time=? AND action=? ORDER BY id LIMIT 1""",
                (family,slug,existing["entered"],existing["side"])).fetchone()
            if entry:
                self.estimates.observe_autonomous(db,entry["id"],now,up,book,data,observation)
        if action in ("UP","DOWN"):
            fill = audit["selected"]["fill"]
            db.execute("""INSERT INTO model_paper_positions
                (family,slug,version,side,entered,quantity,cost,terms,entry_fee,status)
                VALUES (?,?,?,?,?,?,?,?,?,'OPEN')""",(family,slug,version,action,now,fill["quantity"],
                    fill["total"],json.dumps(terms),fill["fee"]))
            db.execute("UPDATE model_paper_accounts SET cash=cash-? WHERE family=?",(fill["total"],family))
            self.estimates.add_entry(db,"autonomous",recorded.lastrowid,slug,family,version,None,
                STAKE,now,context["close"],action,fill["quantity"],fill["total"],fill["fee"],terms,trail)


def agreement_rounds(db):
    """Compare the new rule with both controls on its verified round cohort."""
    return db.execute("""WITH round_cohort AS (
        SELECT w.slug,w.start,w.end,v.outcome
        FROM model_paper_windows w JOIN verified_markets v ON v.slug=w.slug AND v.end=w.end
        WHERE w.closed IS NOT NULL AND json_extract(w.versions, '$.reference_gap_agreement') IS NOT NULL
    ), entries AS (
        SELECT p.slug,p.family,p.version,p.entered,p.side,p.quantity,p.cost,p.entry_fee AS fee
        FROM model_paper_positions p JOIN round_cohort r ON r.slug=p.slug
        WHERE p.family IN (?,?)
        UNION ALL
        SELECT t.slug,'lightgbm_45',t.version,t.time,t.side,t.quantity,t.cost,t.fee
        FROM model_paper_timing t JOIN round_cohort r ON r.slug=t.slug
        WHERE t.family='lightgbm' AND t.checkpoint=45 AND t.quantity IS NOT NULL
    ), comparisons AS (
        SELECT ? AS family,'Reference Gap + Market Agreement' AS label
        UNION ALL SELECT ?,'Reference Gap baseline'
        UNION ALL SELECT 'lightgbm_45','LightGBM +45s'
    )
    SELECT c.label,r.slug,r.start,r.end,r.outcome,e.version,e.entered,e.side,e.cost,e.fee,
           CASE WHEN e.quantity IS NOT NULL THEN (e.side=r.outcome)*e.quantity-e.cost END AS pnl
    FROM round_cohort r CROSS JOIN comparisons c
    LEFT JOIN entries e ON e.slug=r.slug AND e.family=c.family
    ORDER BY r.start,c.label""",(REFERENCE_AGREEMENT,REFERENCE_GAP,REFERENCE_AGREEMENT,REFERENCE_GAP))


def standings(db):
    rows=[]
    for family,label in FAMILIES.items():
        cash=db.execute("SELECT cash FROM model_paper_accounts WHERE family=?",(family,)).fetchone()[0]
        total=db.execute("""SELECT count(pnl),sum(pnl>0),sum(pnl<0),sum(pnl),
            sum(status='OPEN'),sum(status='CASHED_OUT') FROM model_paper_positions WHERE family=?""",(family,)).fetchone()
        last=db.execute("""SELECT slug,version,up,time,action,reason FROM model_paper_decisions
                           WHERE family=? ORDER BY id DESC LIMIT 1""",(family,)).fetchone()
        skipped=db.execute("SELECT count(*) FROM model_paper_decisions WHERE family=? AND observation='final'",
                           (family,)).fetchone()[0]
        done=[r[0] for r in db.execute("SELECT pnl FROM model_paper_positions WHERE family=? AND pnl IS NOT NULL ORDER BY closed,slug",(family,))]
        runs=streaks(["WIN" if p>0 else "LOSS" if p<0 else "FLAT" for p in done],"WIN","LOSS")
        rows.append(dict(family=family,label=label,cash=cash,closed=total[0],wins=total[1] or 0,skips=skipped,streak=runs,
                         losses=total[2] or 0,pnl=total[3],open=total[4] or 0,cashouts=total[5] or 0,
                         latest=dict(last) if last else None))
    return rows
