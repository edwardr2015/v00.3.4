"""Durable diagnostics and reversible local repair actions."""
import json
from pathlib import Path
import resource
import shutil
import sys
import time

from .adaptive_entry import get_state


def initialize(store):
    with store.connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS operational_incidents (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, opened REAL NOT NULL, last_seen REAL NOT NULL,
            resolved REAL, details TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS operations_open_kind
            ON operational_incidents(kind) WHERE resolved IS NULL;
        CREATE TABLE IF NOT EXISTS repair_history (
            id INTEGER PRIMARY KEY, time REAL NOT NULL, action TEXT NOT NULL, state TEXT NOT NULL,
            details TEXT NOT NULL
        );
        """)


def record_release_repairs(store):
    findings = (
        ("UNGATED_FORMAL_START", "v2paper.Engine.mode", "test_formal_preflight_persists_and_blocks_unready"),
        ("FUNDED_AUTOMATIC_FLIP", "adaptive_entry.AdaptiveEntry.evaluate_account",
         "test_flip_requires_fresh_full_liquidity_and_only_settles_once"),
        ("LATE_MODEL_ON_EARLIER_CAPTURE", "lab.Lab.tick", "test_future_model_version_cannot_predict_earlier_capture"),
        ("SETTLEMENT_BEFORE_VERIFICATION", "v2paper.Engine.settle, lab.Lab.exit_and_settle",
         "test_delayed_official_settlement_waits_for_verification"),
    )
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for kind, location, regression in findings:
            if db.execute("SELECT 1 FROM operational_incidents WHERE kind=?", (kind,)).fetchone():
                continue
            now = time.time()
            audit = dict(code=location, regression=regression, fixed_in="0.3.4")
            db.execute("""INSERT INTO operational_incidents(kind,opened,last_seen,resolved,details)
                         VALUES (?,?,?,?,?)""", (kind, now, now, now, json.dumps(audit)))
            journal(db, now, kind, "REGRESSION ADDED", audit)


def journal(db, now, action, state, details):
    db.execute("INSERT INTO repair_history(time,action,state,details) VALUES (?,?,?,?)",
               (now,action,state,json.dumps(details)))


def sample(store, now=None, *, check_integrity=False):
    now=time.time() if now is None else now
    free=shutil.disk_usage(Path(store.path).parent).free
    usage=resource.getrusage(resource.RUSAGE_SELF)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        status={name:get_state(db,"health_"+name) for name in ("market","reference")}
        integrity=db.execute("PRAGMA quick_check").fetchone()[0] if check_integrity else None
        issues={"DISK_HEADROOM":free<256*2**20}
        if check_integrity:
            issues["DATABASE_INTEGRITY"]=integrity!="ok"
        for name, health in status.items():
            if health:
                last=health.get("last_success") or health.get("checked") or 0
                issues[name.upper()+"_FEED"]=not health.get("ok") or now-last>40
        previous_eval=get_state(db,"next_evaluation")
        if previous_eval:
            issues["DECISION_LOOP_DELAY"]=now-previous_eval>15
        for kind, active in issues.items():
            detail=json.dumps(dict(checked=now))
            if active:
                db.execute("""INSERT INTO operational_incidents(kind,opened,last_seen,details) VALUES (?,?,?,?)
                    ON CONFLICT(kind) WHERE resolved IS NULL DO UPDATE SET last_seen=excluded.last_seen""",
                    (kind,now,now,detail))
            else:
                db.execute("UPDATE operational_incidents SET resolved=?,last_seen=? WHERE kind=? AND resolved IS NULL",
                           (now,now,kind))
        counts=[dict(r) for r in db.execute("SELECT * FROM operational_incidents ORDER BY id DESC LIMIT 30")]
        if check_integrity:
            journal(db,now,"diagnose","VERIFIED" if integrity=="ok" else "REVIEW_REQUIRED",dict(integrity=integrity))
    return dict(time=now,free_disk_bytes=free,user_cpu_s=usage.ru_utime,system_cpu_s=usage.ru_stime,
                peak_ram_bytes=usage.ru_maxrss*(1 if sys.platform=="darwin" else 1024),
                evaluation_delay_s=max(0,now-previous_eval) if previous_eval else None,
                integrity=integrity,incidents=counts)


async def reconnect(store, collector, *, started):
    now=time.time()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        last=db.execute("SELECT max(time) FROM repair_history WHERE action='reconnect' AND state='STARTED'").fetchone()[0]
        if last is not None and now-last<60:
            return False,"Reconnect already attempted in the last minute."
        if not started:
            return False,"Feed workers are not running in this application session."
        if get_state(db,"collection_paused"):
            return False,"Resume collection before reconnecting."
        journal(db,now,"reconnect","STARTED",dict(manual=True))
        db.execute("DELETE FROM state WHERE key IN ('health_market','health_reference')")
    try:
        await collector.restart()
    except Exception as exc:
        with store.connect() as db:
            journal(db,time.time(),"reconnect","FAILED",dict(error=type(exc).__name__))
        return False,"Feed restart failed. Check diagnostics."
    with store.connect() as db:
        journal(db,time.time(),"reconnect","WORKERS_RESTARTED",dict(feed_verification="pending"))
    return True,"Feed workers restarted. Quotes must refresh before fills can resume."
