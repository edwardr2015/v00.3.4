"""Evidence-based paper model selection and a single fully funded reversal."""
import json
import math
import pickle

from .execution import execute, paper_buy

MIN_MATCHED = 20
VOLATILE = .001
# Incumbent-based selection for the primary account (0.3.23-dev). A challenger must beat the
# model that actually trades the account, on the same settled markets, held to settlement.
INCUMBENT_LOOKBACK = 100        # most recent matched markets used as evidence
REDUNDANT_AGREEMENT = .90       # challengers that pick the incumbent's side this often add nothing
MIN_IMPROVEMENT_SE = 1.0        # mean improvement must exceed one standard error
BRIER_TOLERANCE = .01           # challenger calibration may not be worse than incumbent + tolerance
NOT_SELECTABLE = {"model_vote", "reference_gap_agreement"}   # derived consensus / rule on incumbent
MODEL_ERRORS = (ValueError, ImportError, OSError, TypeError, RuntimeError, EOFError, pickle.UnpicklingError)


class Selection:
    def __init__(self, store):
        self.store = store
        self.models = None
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS paper_model_choices (
                  slug TEXT, regime TEXT, time REAL, model TEXT, evidence TEXT,
                  PRIMARY KEY(slug,regime)
                );
                CREATE TABLE IF NOT EXISTS paper_model_switches (
                  id INTEGER PRIMARY KEY, time REAL NOT NULL, slug TEXT NOT NULL,
                  previous TEXT, model TEXT NOT NULL, evidence TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_flips (
                  order_id INTEGER PRIMARY KEY REFERENCES v2_orders(id),
                  time REAL NOT NULL, model TEXT NOT NULL, side TEXT NOT NULL,
                  quantity REAL NOT NULL, debit REAL NOT NULL, credit REAL NOT NULL,
                  buy_fee REAL NOT NULL, sell_fee REAL NOT NULL, improvement REAL NOT NULL,
                  book_timestamp REAL NOT NULL, details TEXT NOT NULL
                );
            """)

    def rank_incumbent(self, slug, volatility, now, incumbent):
        """Rank challengers against the incumbent on matched, settled, held-to-settlement markets."""
        from .model_paper import FORMULA_VERSIONS, ModelPaper

        regime = "higher volatility" if volatility >= VOLATILE else "lower volatility"
        family_of = {v: k for k, v in FORMULA_VERSIONS.items()}
        home = family_of.get(incumbent)
        with self.store.connect() as db:
            if home is None or not db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='model_paper_positions'").fetchone():
                return regime, []
            current = ModelPaper.versions(db, now)
            rows = db.execute("""
                SELECT c.family, c.version, c.slug, c.side, c.quantity, c.cost, c.outcome,
                       i.side AS i_side, i.quantity AS i_quantity, i.cost AS i_cost,
                       (SELECT up FROM model_paper_decisions d WHERE d.family=c.family AND d.slug=c.slug
                          AND d.time=c.entered AND d.action=c.side LIMIT 1) AS up,
                       (SELECT up FROM model_paper_decisions d WHERE d.family=i.family AND d.slug=i.slug
                          AND d.time=i.entered AND d.action=i.side LIMIT 1) AS i_up
                FROM model_paper_positions c
                JOIN model_paper_positions i ON i.slug=c.slug AND i.family=?
                JOIN model_paper_windows w ON w.slug=c.slug
                JOIN verified_markets v ON v.slug=c.slug AND v.end=w.end
                WHERE c.family<>? AND c.slug<>? AND c.outcome IS NOT NULL AND i.outcome IS NOT NULL
                  AND v.end<=? AND v.verified_at<=? AND c.entered<? AND i.entered<?
                ORDER BY v.end DESC""", (home, home, slug, now, now, now, now)).fetchall()
        by = {}
        for r in rows:
            if r["family"] in NOT_SELECTABLE:
                continue
            bucket = by.setdefault(r["family"], [])
            if len(bucket) < INCUMBENT_LOOKBACK:
                bucket.append(r)
        ranked = []
        for family, matched in by.items():
            version = current.get(family)
            n = len(matched)
            if not version or version.startswith("UNAVAILABLE") or n < MIN_MATCHED:
                continue
            cand = [(r["side"] == r["outcome"]) * r["quantity"] - r["cost"] for r in matched]
            inc = [(r["i_side"] == r["outcome"]) * r["i_quantity"] - r["i_cost"] for r in matched]
            diff = [a - b for a, b in zip(cand, inc)]
            mean = sum(diff) / n
            se = (sum((d - mean) ** 2 for d in diff) / (n - 1)) ** .5 / n ** .5 if n > 1 else float("inf")
            agreement = sum(r["side"] == r["i_side"] for r in matched) / n
            scored = [r for r in matched if r["up"] is not None and r["i_up"] is not None]
            brier = (sum((r["up"] - (r["outcome"] == "UP")) ** 2 for r in scored) / len(scored)) if scored else None
            i_brier = (sum((r["i_up"] - (r["outcome"] == "UP")) ** 2 for r in scored) / len(scored)) if scored else None
            evidence = dict(family=family, version=version, matched=n, pnl=sum(cand), incumbent_pnl=sum(inc),
                            improvement=mean, improvement_se=se, agreement=agreement, brier=brier,
                            incumbent_brier=i_brier, compared_with=incumbent, basis="hold-to-settlement")
            if (sum(cand) > 0 and mean > MIN_IMPROVEMENT_SE * se and agreement < REDUNDANT_AGREEMENT
                    and brier is not None and brier <= i_brier + BRIER_TOLERANCE):
                ranked.append(evidence)
        ranked.sort(key=lambda e: (-e["improvement"], -e["matched"], e["version"]))
        return regime, ranked

    def rank(self, slug, volatility, now):
        regime = "higher volatility" if volatility >= VOLATILE else "lower volatility"
        with self.store.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='lab_orders'").fetchone():
                return regime, []
            rows = db.execute("""WITH recent AS (
                SELECT b.slug,b.pnl FROM lab_orders b
                JOIN lab_configs c ON c.id=b.config
                WHERE c.id='timing/v2/momentum-v2/90/HOLD/1' AND b.slug<>?
                  AND b.status='SETTLED' AND b.settled<=?
                  AND json_extract(b.details,'$.prediction.volatility') IS NOT NULL
                  AND (json_extract(b.details,'$.prediction.volatility')>=?)=?
                ORDER BY b.settled DESC,b.id DESC LIMIT 100
              )
              SELECT c.version,count(*) AS matched,sum(o.pnl) AS pnl,
                     sum(o.pnl-recent.pnl) AS improvement
              FROM recent JOIN verified_markets v ON v.slug=recent.slug AND v.end<=?
              JOIN lab_orders o ON o.slug=recent.slug
              JOIN lab_configs c ON c.id=o.config JOIN model_registry m ON m.version=c.version
              WHERE c.checkpoint=90 AND c.stake=1 AND c.exit='HOLD' AND c.status='FORWARD SHADOW'
                AND c.id LIKE 'timing/v2/%' AND c.version<>'momentum-v2'
                AND o.status='SETTLED' AND o.settled<=? AND (m.trained<=o.time OR m.kind='pattern_odds')
              GROUP BY c.version HAVING count(*)>=? AND sum(o.pnl)>0 AND avg(o.pnl-recent.pnl)>0
              ORDER BY avg(o.pnl-recent.pnl) DESC,count(*) DESC,c.version""",
                (slug, now, VOLATILE, int(volatility >= VOLATILE), now, now, MIN_MATCHED)).fetchall()
        return regime, [dict(row) for row in rows]

    def estimate(self, version, slug, now, features, pattern_context=None):
        from .model_paper import FORMULA_VERSIONS, probabilities_from_market
        from .models import Models, feature_sequence
        from .pattern_odds import VERSION,forecast_from_snapshot

        formula = {v: k for k, v in FORMULA_VERSIONS.items()}.get(version)
        if formula:
            if not pattern_context:
                return None
            context, book, opening = pattern_context
            if not book.get("bids") or not book.get("asks") or not opening:
                return None
            midpoint = (book["bids"][0]["price"] + book["asks"][0]["price"]) / 2
            raw = probabilities_from_market(midpoint, context["reference_price"], opening,
                                            features["volatility"], context["window_end"] - now,
                                            context.get("momentum_up", .5))
            return raw.get(formula)

        if version==VERSION and pattern_context:
            p,_=forecast_from_snapshot(self.store,slug,now,*pattern_context)
            return p
        if version==VERSION:
            return None
        if self.models is None:
            self.models = Models(self.store)
        volatility = features["volatility"]
        x = [features["momentum_60"],features["trend_300"],math.log(max(volatility,1e-7))]
        sequence = feature_sequence(self.store,x,90,slug,now)
        estimate = self.models.predict(version,x,sequence)
        if estimate is None or not math.isfinite(estimate) or not 0<=estimate<=1:
            return None
        return estimate

    def predict(self, slug, now, up, features, default_model, pinned_model=None, pattern_context=None):
        volatility = features.get("volatility")
        if up is None or volatility is None or not all(k in features for k in ("momentum_60","trend_300")):
            return pinned_model or default_model, None if pinned_model and pinned_model!=default_model else up, None
        regime = "higher volatility" if volatility>=VOLATILE else "lower volatility"
        if pinned_model:
            try:
                estimate = up if pinned_model==default_model else self.estimate(pinned_model,slug,now,features,pattern_context)
            except MODEL_ERRORS:
                estimate = None
            return pinned_model,estimate,dict(regime=regime,reason="OPEN_POSITION_MODEL_LOCK"
                        if estimate is not None else "OPEN_POSITION_MODEL_UNAVAILABLE")
        from .model_paper import FORMULA_VERSIONS
        if default_model in FORMULA_VERSIONS.values():
            regime, rows = self.rank_incumbent(slug,volatility,now,default_model)
        else:
            regime, rows = self.rank(slug,volatility,now)
        errors=[]
        for candidate in rows:
            try:
                estimate=self.estimate(candidate["version"],slug,now,features,pattern_context)
                if estimate is not None:
                    return candidate["version"],estimate,dict(regime=regime,**candidate)
                errors.append(candidate["version"]+": waiting for usable inputs")
            except MODEL_ERRORS as exc:
                errors.append(candidate["version"]+": "+type(exc).__name__)
        if errors:
            return default_model,up,dict(regime=regime,reason="MODEL_UNAVAILABLE",errors=errors)
        return default_model,up,dict(regime=regime,reason="NO_COMPARABLE_WINNER",minimum_matched=MIN_MATCHED)


def replacement(book, side, quantity, budget, terms, *, slippage_ticks=1):
    """Price both legs against shared depth without mutating the input quote."""
    sold = execute(book, side, quantity=quantity, buying=False, **terms, slippage_ticks=slippage_ticks)
    if sold["quantity"] + 1e-9 < quantity:
        return None,None,"FULL_EXIT_UNAVAILABLE"
    # The sale and opposite buy consume the same side of the YES book.
    # Deduct the sale from the permitted participation before sizing the buy.
    remaining = dict(book)
    book_side = "bids" if side == "UP" else "asks"
    used = {}
    for fill in sold["fills"]:
        quote = fill["price"] + slippage_ticks*terms["tick"]
        if book_side == "asks":
            quote = 1 - quote
        key = round(quote, 10)
        used[key] = used.get(key, 0) + fill["quantity"]/sold["assumptions"]["participation"]
    remaining[book_side] = []
    for level in book[book_side]:
        key = round(level["price"], 10)
        taken = min(level["quantity"], used.get(key,0))
        used[key] = used.get(key,0) - taken
        if level["quantity"] > taken:
            remaining[book_side].append(dict(level, quantity=level["quantity"]-taken))
    new, reason = paper_buy(remaining, "DOWN" if side=="UP" else "UP", budget,
                            **terms, slippage_ticks=slippage_ticks)
    return sold,new,reason


def shadow_reverse(db, order, account, candidates, book, terms, up, model, now, min_edge, *, commit_clock=None):
    """Record an executable counterfactual without changing the ledger."""
    if account["halted"] or db.execute(
        """SELECT 1 FROM paper_flips WHERE order_id=?
           UNION ALL SELECT 1 FROM shadow_paper_flips WHERE order_id=?""",
        (order["id"], order["id"]),
    ).fetchone():
        return
    other = next(c for c in candidates if c["side"] != order["side"])
    if other["ev_rate"] is None or other["ev_rate"] < min_edge:
        return
    sold,new,_ = replacement(book,order["side"],order["quantity"],account["budget"],terms)
    if not new or (other["probability"]*new["quantity"]-new["total"])/new["total"] < min_edge:
        return
    old_probability = up if order["side"] == "UP" else 1-up
    improvement = sold["total"] - new["total"] + other["probability"]*new["quantity"] - old_probability*order["quantity"]
    net_cash = sold["total"] - new["total"]
    if improvement < min_edge * order["cost"] or account["balance"] + net_cash < 0:
        return
    if commit_clock:
        now = commit_clock()
        if now is None:
            return
    db.execute("INSERT INTO shadow_paper_flips VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
               (order["id"],now,model,other["side"],new["quantity"],new["total"],sold["total"],
                new["fee"],sold["fee"],improvement,book["timestamp"],
                json.dumps(dict(sale=sold,buy=new,up=up,hold_prob=old_probability))))
    return dict(side=other["side"], improvement=improvement, time=now)
