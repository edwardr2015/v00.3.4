"""Matched $3 hold-versus-flip research with recorded quote cost sensitivity."""
import json
import math

from .execution import paper_buy

ACCOUNT = "adaptive-3"
EXTRA_TICKS = 2  # One more adverse tick than the normal paper fill.


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS shadow_paper_flips (
        order_id INTEGER PRIMARY KEY REFERENCES v2_orders(id), time REAL NOT NULL,
        model TEXT NOT NULL, side TEXT NOT NULL, quantity REAL NOT NULL, debit REAL NOT NULL,
        credit REAL NOT NULL, buy_fee REAL NOT NULL, sell_fee REAL NOT NULL, improvement REAL NOT NULL,
        book_timestamp REAL NOT NULL, details TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hold_flip_inputs (
        order_id INTEGER PRIMARY KEY REFERENCES v2_orders(id),
        entry_alt_quantity REAL, entry_alt_cost REAL, entry_alt_reason TEXT NOT NULL,
        entry_cash REAL NOT NULL, entry_audit TEXT NOT NULL,
        flip_alt_quantity REAL, flip_alt_debit REAL, flip_alt_credit REAL,
        flip_alt_reason TEXT, flip_audit TEXT
    )""")


def quote_audit(book, details):
    # The source and confirmation times have different meanings. Preserve both.
    return dict(book_source=book["timestamp"], confirmed=details.get("book_checked"),
                reference_time=details.get("reference_timestamp"),
                bid=details.get("bid"), ask=details.get("ask"),
                depth_bid=details.get("bid_depth"), depth_ask=details.get("ask_depth"),
                volatility=details.get("volatility"), momentum=details.get("momentum_60"),
                reference=details.get("reference_price"), opening=details.get("opening_price"),
                seconds_remaining=details["window_end"]-details["observed_at"])


def record_entry(db, order_id, account, side, book, terms, details):
    fill, reason = paper_buy(book, side, account["budget"], **terms, slippage_ticks=EXTRA_TICKS)
    if fill and fill["total"] > account["balance"]:
        fill, reason = None, "EXTRA_TICK_INSUFFICIENT_CASH"
    db.execute("""INSERT INTO hold_flip_inputs
        (order_id,entry_alt_quantity,entry_alt_cost,entry_alt_reason,entry_cash,entry_audit)
        VALUES (?,?,?,?,?,?)""", (order_id,fill["quantity"] if fill else None,
        fill["total"] if fill else None,reason or "MODELED",account["balance"],
        json.dumps(quote_audit(book,details))))


def record_flip(db, order, account, book, terms, details, up, simulate_replacement):
    original = db.execute("SELECT * FROM hold_flip_inputs WHERE order_id=?",(order["id"],)).fetchone()
    if original is None:
        return
    reason = "EXTRA_TICK_ENTRY_UNAVAILABLE"
    sold = bought = None
    if original["entry_alt_quantity"] is not None:
        sold, bought, reason = simulate_replacement(
            book, order["side"], original["entry_alt_quantity"], account["budget"], terms,
            slippage_ticks=EXTRA_TICKS)
        if bought and original["entry_cash"]-original["entry_alt_cost"]+sold["total"] < bought["total"]:
            sold = bought = None
            reason = "EXTRA_TICK_INSUFFICIENT_CASH"
    audit = dict(quote_audit(book,details),entry_probability_up=order["probability"],flip_probability_up=up)
    db.execute("""UPDATE hold_flip_inputs SET flip_alt_quantity=?,flip_alt_debit=?,flip_alt_credit=?,
        flip_alt_reason=?,flip_audit=? WHERE order_id=?""",
        (bought["quantity"] if bought else None,bought["total"] if bought else None,
         sold["total"] if bought else None,reason or "MODELED",json.dumps(audit),order["id"]))


def rows(db):
    for r in db.execute("""WITH flips AS (
        SELECT *, 'HISTORICAL PAPER' AS flip_mode FROM paper_flips
        UNION ALL
        SELECT *, 'UNFUNDED SHADOW' FROM shadow_paper_flips s
        WHERE NOT EXISTS (SELECT 1 FROM paper_flips f WHERE f.order_id=s.order_id)
        )
        SELECT o.id,o.slug,o.time,o.side,o.quantity,o.cost,o.fee,o.model,
        o.probability,o.result,o.pnl,o.details,f.side AS flip_side,f.time AS flip_time,
        f.model AS flip_model,f.flip_mode,f.quantity AS flip_quantity,f.credit,f.debit,f.buy_fee,f.sell_fee,f.improvement,
        a.entry_alt_quantity,a.entry_alt_cost,a.entry_alt_reason,a.entry_audit,
        a.flip_alt_quantity,a.flip_alt_debit,a.flip_alt_credit,a.flip_alt_reason,a.flip_audit
        FROM v2_orders o LEFT JOIN flips f ON f.order_id=o.id
        LEFT JOIN hold_flip_inputs a ON a.order_id=o.id
        WHERE o.account=? AND o.status='SETTLED' AND o.pnl IS NOT NULL ORDER BY o.time,o.id""",(ACCOUNT,)):
        row = dict(r)
        hold = (r["quantity"] if r["side"]==r["result"] else 0)-r["cost"]
        flip_pnl = hold if r["flip_side"] is None else (
            (r["flip_quantity"] if r["flip_side"] == r["result"] else 0)
            - r["cost"] + r["credit"] - r["debit"]
        )
        stressed_hold = stressed_flip = None
        if r["entry_alt_quantity"] is not None:
            stressed_hold = (r["entry_alt_quantity"] if r["side"]==r["result"] else 0)-r["entry_alt_cost"]
            if r["flip_side"] is None:
                stressed_flip = stressed_hold
            elif r["flip_alt_quantity"] is not None:
                stressed_flip = ((r["flip_alt_quantity"] if r["flip_side"]==r["result"] else 0)
                                 -r["entry_alt_cost"]+r["flip_alt_credit"]-r["flip_alt_debit"])
        audit = json.loads(r["entry_audit"] or "{}")
        trigger = json.loads(r["flip_audit"] or "{}")
        vol = audit.get("volatility")
        spread = audit["ask"]-audit["bid"] if audit.get("bid") is not None and audit.get("ask") is not None else None
        opening, ref = audit.get("opening"), audit.get("reference")
        gap = abs(ref/opening-1) if opening and ref is not None else None
        row.update(hold_pnl=hold, flip_pnl=flip_pnl, flip_minus_hold=flip_pnl-hold,
                   extra_hold=stressed_hold,extra_flip=stressed_flip,
                   extra_difference=stressed_flip-stressed_hold if stressed_flip is not None else None,
                   entry_inputs=audit,flip_inputs=trigger,spread=spread,gap=gap,
                   volatility_group="unrecorded" if vol is None else "higher" if vol>=.001 else "lower",
                   spread_group="unrecorded" if spread is None else "wide" if spread>=.03-1e-10 else "tight",
                   gap_group="unrecorded" if gap is None else "far" if gap>=.00082 else "near")
        yield row


def breakdown(records):
    groups = {("All","All"):records}
    for row in records:
        mode = row["flip_mode"] or "HOLD ONLY"
        groups.setdefault(("Flip classification", mode), []).append(row)
    for column in ("volatility","spread","gap"):
        for r in records:
            groups.setdefault((column,r[column+"_group"]), []).append(r)
    for (dimension, group), sample in groups.items():
        stressed = [r for r in sample if r["extra_difference"] is not None]
        flipped = [r for r in sample if r["flip_side"] is not None]
        yield [dimension,group,len(sample),len(flipped),
               math.fsum(r["hold_pnl"] for r in sample) if sample else None,
               math.fsum(r["flip_pnl"] for r in sample) if sample else None,
               math.fsum(r["flip_minus_hold"] for r in sample) if sample else None,len(stressed),
               math.fsum(r["extra_hold"] for r in stressed) if stressed else None,
               math.fsum(r["extra_flip"] for r in stressed) if stressed else None,
               math.fsum(r["extra_difference"] for r in stressed) if stressed else None]
