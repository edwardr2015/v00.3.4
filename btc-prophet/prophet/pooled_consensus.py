"""Unfunded agreement comparisons at a common paper budget."""
import json

from .consensus import vote
from .execution import compare_stakes

BASELINE_BUDGET = 3
POOLED_BUDGET = 100
CONTROL = "unanimous"
MARKET_UNANIMOUS = "unanimous_market"
REFERENCE_MARKET = "reference_market"
REFERENCE = "volatility_reference_gap"
LABELS = {
    CONTROL: "All 10 models agree",
    MARKET_UNANIMOUS: "All 10 models + market agree",
    REFERENCE_MARKET: "Reference Gap + market agree",
}
COLUMNS = """
    slug TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL, observed REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('WAITING','SKIP','FILLED')),
    reason TEXT NOT NULL, side TEXT, probability REAL, quantity REAL, cost REAL,
    baseline_quantity REAL, baseline_cost REAL, evidence TEXT NOT NULL
"""


def initialize(db):
    db.execute(f"""CREATE TABLE IF NOT EXISTS pooled_consensus_rounds (
        {COLUMNS}, PRIMARY KEY(slug))""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS pooled_agreement_rounds (
        scenario TEXT NOT NULL, {COLUMNS}, PRIMARY KEY(scenario,slug))""")
    db.execute("""CREATE INDEX IF NOT EXISTS pooled_consensus_waiting ON pooled_consensus_rounds(end)
                  WHERE status='WAITING'""")
    db.execute("""CREATE INDEX IF NOT EXISTS pooled_agreement_waiting ON pooled_agreement_rounds(end)
                  WHERE status='WAITING'""")


def evaluate(scenario, versions, forecasts, required, book, terms, minimum_edge, blocked):
    if scenario == REFERENCE_MARKET:
        required = (REFERENCE,)
    components = {family: {"up": forecasts[family], "version": versions[family]}
                  for family in required if family in forecasts and versions.get(family)}
    decision = vote(components, required)
    decision.update(side=None, probability=None, reason=blocked)
    if blocked:
        return decision
    if decision["missing"]:
        decision["reason"] = "POOL_MODELS_NOT_READY"
        return decision
    if max(decision["up_votes"], decision["down_votes"]) != len(required):
        decision["reason"] = "POOL_NOT_UNANIMOUS" if len(required) > 1 else "REFERENCE_TIE"
        return decision
    side = "UP" if decision["up_votes"] else "DOWN"
    probability = decision["mean_up"] if side == "UP" else 1 - decision["mean_up"]
    midpoint = (book["bids"][0]["price"] + book["asks"][0]["price"]) / 2
    decision.update(side=side, probability=probability, market_up=midpoint)
    if scenario != CONTROL and (midpoint == .5 or (midpoint > .5) != (side == "UP")):
        decision["reason"] = "MODEL_MARKET_DISAGREEMENT"
        return decision
    trials = compare_stakes(book, side, probability, terms, minimum_edge, (BASELINE_BUDGET, POOLED_BUDGET))
    decision.update(trials=trials, reason=trials[1]["reason"])
    return decision


def observe(db, slug, start, end, now, versions, forecasts, required, book, terms,
            minimum_edge, blocked, *, book_confirmed=None, reference_time=None, compare=False):
    if not start <= now < end:
        return
    for scenario in LABELS if compare else (CONTROL,):
        if scenario == CONTROL:
            table, key, extra = "pooled_consensus_rounds", "slug", ()
        else:
            table, key, extra = "pooled_agreement_rounds", "scenario,slug", (scenario,)
        condition = "scenario=? AND slug=?" if extra else "slug=?"
        prior = db.execute(f"SELECT status,observed FROM {table} WHERE {condition}", (*extra, slug)).fetchone()
        if prior and (prior["status"] != "WAITING" or prior["observed"] >= now):
            continue
        evidence = evaluate(scenario, versions, forecasts, required, book, terms, minimum_edge, blocked)
        evidence.update(book_source=book.get("timestamp"), book_confirmed=book_confirmed,
                        reference_time=reference_time)
        small, large = evidence.get("trials", ({}, {}))
        quantity, cost = approved_fill(large)
        baseline_quantity, baseline_cost = approved_fill(small) if quantity is not None else (None, None)
        cells = (*extra, slug, start, end, now, "FILLED" if quantity is not None else "WAITING",
                 evidence["reason"], evidence["side"], evidence["probability"], quantity, cost,
                 baseline_quantity, baseline_cost, json.dumps(evidence))
        marks = ",".join("?" for _ in cells)
        db.execute(f"""INSERT INTO {table} VALUES ({marks})
            ON CONFLICT({key}) DO UPDATE SET
              observed=excluded.observed,status=excluded.status,reason=excluded.reason,side=excluded.side,
              probability=excluded.probability,quantity=excluded.quantity,cost=excluded.cost,
              baseline_quantity=excluded.baseline_quantity,baseline_cost=excluded.baseline_cost,evidence=excluded.evidence
            WHERE {table}.status='WAITING' AND {table}.observed<excluded.observed""", cells)


def approved_fill(trial):
    if trial.get("reason") != "SHADOW_FILL":
        return None, None
    fill = trial["modeled_fill"]
    return fill["quantity"], fill["total"]


def finalize(db, now):
    for table in ("pooled_consensus_rounds", "pooled_agreement_rounds"):
        db.execute(f"UPDATE {table} SET status='SKIP' WHERE status='WAITING' AND end<=?", (now,))


def round_results(scenario=CONTROL, *, matched=False):
    if scenario == CONTROL:
        source = "pooled_consensus_rounds"
        condition = ""
        if matched:
            condition = """WHERE EXISTS (SELECT 1 FROM pooled_agreement_rounds a
                WHERE a.scenario='unanimous_market' AND a.slug=p.slug)"""
    else:
        source = "pooled_agreement_rounds"
        condition = "WHERE p.scenario=?"
    return f"""SELECT p.*,v.outcome,
        CASE WHEN v.outcome IS NOT NULL AND p.status='FILLED' THEN (p.side=v.outcome)*p.quantity-p.cost END AS pnl,
        CASE WHEN v.outcome IS NOT NULL AND p.baseline_quantity IS NOT NULL
             THEN (p.side=v.outcome)*p.baseline_quantity-p.baseline_cost END AS baseline_pnl
        FROM {source} p LEFT JOIN verified_markets v ON v.slug=p.slug AND v.end=p.end {condition}"""


def results(db, scenario, *, matched=False):
    return db.execute(round_results(scenario, matched=matched) + " ORDER BY p.start,p.slug",
                      () if scenario == CONTROL else (scenario,))


def summary(db, scenario=CONTROL, *, matched=False):
    query = round_results(scenario, matched=matched)
    args = () if scenario == CONTROL else (scenario,)
    result = dict(db.execute(f"""SELECT count(*) AS observed, count(outcome) AS verified,
        count(pnl) AS settled, count(CASE WHEN pnl>0 THEN 1 END) AS wins,
        count(CASE WHEN pnl<0 THEN 1 END) AS losses, sum(pnl) AS net_pnl,
        count(CASE WHEN status='SKIP' THEN 1 END) AS skips,
        count(CASE WHEN status='FILLED' AND outcome IS NULL THEN 1 END) AS open,
        count(baseline_pnl) AS paired, sum(baseline_pnl) AS paired_baseline_pnl,
        sum(CASE WHEN baseline_pnl IS NOT NULL THEN pnl END) AS paired_pooled_pnl
        FROM ({query})""", args).fetchone())
    latest = db.execute(query + " ORDER BY p.start DESC,p.slug DESC LIMIT 1", args).fetchone()
    result.update(label=LABELS[scenario], latest=None)
    if latest:
        evidence = json.loads(latest["evidence"])
        result["latest"] = dict(slug=latest["slug"], status=latest["status"], reason=latest["reason"],
                                side=latest["side"], outcome=latest["outcome"],
                                up=evidence["up_votes"], down=evidence["down_votes"],
                                ties=evidence["ties"], missing=evidence["missing"])
    return result


def comparisons(db):
    return [summary(db, scenario, matched=True) for scenario in LABELS]
