"""Matched late-sale comparisons for the unfunded Reference Gap trials."""
import json

from .execution import execute

POLICY = "gap_agreement_40_70"
TARGET = 240
GRACE = 8


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS corrective_late_exits (
        slug TEXT NOT NULL, cost_case TEXT NOT NULL, observed REAL NOT NULL,
        proceeds REAL, reason TEXT NOT NULL, evidence TEXT NOT NULL,
        PRIMARY KEY(slug,cost_case))""")


def observe(db, slug, start, end, now, book, terms, block, metadata):
    if not start + TARGET <= now < end:
        return
    entries = db.execute("""SELECT t.* FROM corrective_trials t
        LEFT JOIN corrective_late_exits x USING(slug,cost_case)
        WHERE t.slug=? AND t.policy=? AND x.slug IS NULL AND t.time<?""",
        (slug,POLICY,start+TARGET)).fetchall()
    for row in entries:
        if now > start+TARGET+GRACE:
            reason,fill = "WINDOW_MISSED",None
        elif block or not metadata.get("received") or metadata["received"] <= row["time"]:
            continue
        else:
            costs = dict(terms)
            if row["cost_case"]=="higher":
                costs.update(theta=min(1,costs["theta"]+.02),slippage_ticks=2)
            fill = execute(book,row["side"],quantity=row["quantity"],buying=False,**costs)
            if fill["quantity"]+1e-9 < row["quantity"]:
                continue
            reason = "EXECUTABLE_FULL_SALE"
        proceeds = fill["total"] if fill else None
        db.execute("INSERT OR IGNORE INTO corrective_late_exits VALUES (?,?,?,?,?,?)",
                   (slug,row["cost_case"],now,proceeds,reason,json.dumps(dict(metadata,fill=fill))))


def results(db, now):
    return db.execute("""SELECT t.slug,t.cost_case,t.time AS entry_time,t.side,t.cost,
        t.quantity,r.end,v.outcome,x.observed AS exit_time,x.proceeds,x.reason,x.evidence,
        (t.side=v.outcome)*t.quantity-t.cost AS hold_pnl,
        coalesce(x.proceeds,(t.side=v.outcome)*t.quantity)-t.cost AS late_pnl
        FROM corrective_trials t JOIN corrective_rounds r USING(slug)
        JOIN verified_markets v ON v.slug=r.slug AND v.end=r.end
        LEFT JOIN corrective_late_exits x USING(slug,cost_case)
        WHERE t.policy=? AND t.time<r.start+? AND v.verified_at<=?
        ORDER BY r.end,t.cost_case""",(POLICY,TARGET,now))
