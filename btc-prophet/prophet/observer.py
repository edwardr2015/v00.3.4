"""Independent paper-only accounts for observer picks and comparison scenarios."""
import json

from .adaptive_entry import get_state
from .execution import paper_buy
from .v2risk import local_day
from .analytics import streaks

STARTING_CASH = 1000.0
ENTRY_BUDGET = 1.0
DAILY_LIMIT = None
LOSS_STREAK = 3
MAIN = "observer"


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS observer_accounts (
            account TEXT PRIMARY KEY, cash REAL NOT NULL CHECK(cash>=0),
            loss_streak INTEGER NOT NULL DEFAULT 0, learning_since REAL
        );
        CREATE TABLE IF NOT EXISTS observer_rounds (
            account TEXT, slug TEXT, time REAL NOT NULL, side TEXT,
            status TEXT NOT NULL, reason TEXT NOT NULL, quantity REAL, cost REAL, fee REAL,
            outcome TEXT, settled REAL, settlement_day TEXT, pnl REAL, PRIMARY KEY(account,slug)
        );
        CREATE INDEX IF NOT EXISTS observer_pending ON observer_rounds(status,account);
        CREATE INDEX IF NOT EXISTS observer_daily ON observer_rounds(account,settlement_day);
    """)
    db.execute("INSERT OR IGNORE INTO observer_accounts(account,cash) VALUES (?,?)", (MAIN, STARTING_CASH))


def daily_loss(db, account, now):
    row = db.execute("""SELECT coalesce(sum(-pnl),0) FROM observer_rounds
        WHERE account=? AND settlement_day=? AND pnl<0""", (account, local_day(now))).fetchone()
    return row[0]


def status(db, account, now):
    state = db.execute("SELECT * FROM observer_accounts WHERE account=?", (account,)).fetchone()
    lost = daily_loss(db, account, now)
    mode = "LEARN" if state["learning_since"] is not None else "OBSERVE"
    return dict(cash=state["cash"], loss_streak=state["loss_streak"], mode=mode, daily_loss=lost,
                starting_cash=STARTING_CASH, daily_limit=DAILY_LIMIT, streak_limit=LOSS_STREAK,
                entry_budget=ENTRY_BUDGET)


def record(db, account, slug, when, now, side, fill, reason):
    """Record at most one immutable pick per market for an individual account."""
    db.execute("INSERT OR IGNORE INTO observer_accounts(account,cash) VALUES (?,?)", (account, STARTING_CASH))
    state = status(db, account, now)
    if state["mode"] == "LEARN":
        status_code, why = "LEARN", "LOSS_STREAK_REVIEW"
    elif not side or not fill:
        status_code, why = "SKIP", reason
    elif state["cash"] < fill["total"]:
        status_code, why = "SKIP", "INSUFFICIENT_CASH"
    elif db.execute("SELECT 1 FROM observer_rounds WHERE account=? AND status='OPEN'", (account,)).fetchone():
        status_code, why = "SKIP", "PREVIOUS_ROUND_UNSETTLED"
    else:
        status_code, why = "OPEN", "PAPER_OBSERVATION"
    order = fill if status_code == "OPEN" else None
    saved = db.execute("""INSERT OR IGNORE INTO observer_rounds
        (account,slug,time,side,status,reason,quantity,cost,fee) VALUES (?,?,?,?,?,?,?,?,?)""",
        (account, slug, when, side, status_code, why, order["quantity"] if order else None,
         order["total"] if order else None, order["fee"] if order else None))
    if saved.rowcount and order:
        db.execute("UPDATE observer_accounts SET cash=cash-? WHERE account=?", (order["total"], account))


def settle(db, now):
    rows = db.execute("""SELECT p.*,m.outcome AS official FROM observer_rounds p
        JOIN verified_markets m ON m.slug=p.slug
        WHERE p.status IN ('OPEN','LEARN','SKIP') AND m.end<=?
        ORDER BY m.end,p.time,p.account""", (now,)).fetchall()
    for row in rows:
        account = row["account"]
        won = row["side"] == row["official"]
        pnl = None
        if row["status"] == "OPEN":
            payout = row["quantity"] if won else 0
            pnl = payout-row["cost"]
            db.execute("""UPDATE observer_accounts SET cash=cash+?,
                learning_since=CASE WHEN ?<0 AND loss_streak+1>=? THEN ? ELSE learning_since END,
                loss_streak=CASE WHEN ?<0 THEN loss_streak+1 ELSE 0 END WHERE account=?""",
                (payout,pnl,LOSS_STREAK,now,pnl,account))
        elif row["status"] == "LEARN" and won:
            db.execute("""UPDATE observer_accounts SET learning_since=NULL,loss_streak=0
                WHERE account=? AND learning_since IS NOT NULL AND ?>=learning_since""",
                (account,row["time"]))
        db.execute("""UPDATE observer_rounds SET status='SETTLED',outcome=?,settled=?,settlement_day=?,pnl=?
            WHERE account=? AND slug=?""",
            (row["official"],now,local_day(now),pnl,account,row["slug"]))


def scoreboard(db, account):
    pnls = [r[0] for r in db.execute("""SELECT pnl FROM observer_rounds
              WHERE account=? AND pnl IS NOT NULL ORDER BY settled,slug""",(account,))]
    return dict(trades=len(pnls),wins=sum(p>0 for p in pnls),losses=sum(p<0 for p in pnls),
                pnl=sum(pnls), streaks=streaks(
                    ["WIN" if p>0 else "LOSS" if p<0 else "FLAT" for p in pnls], "WIN","LOSS"))
