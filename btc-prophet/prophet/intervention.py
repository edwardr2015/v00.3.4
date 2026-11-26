"""Human review records for problems that automatic retry cannot resolve."""

import time

REQUESTS = {
    "EMERGENCY_STOP": "The emergency stop is active. Confirm it is safe to resume paper research.",
    "ACTIVE_MODEL_CHANGED": "The frozen experiment code changed. Export the results and review the model before resuming.",
    "CRITICAL_SYSTEM_ERROR": "The app encountered a critical error. Check the log and database before approving a restart.",
    "PAPER_TASK_EXITED": "The paper research task stopped unexpectedly. Review the error log before resuming.",
    "SERVICE_RESTART_REVIEW": "The app restarted during a paper session. Check open positions and the feeds before resuming.",
}


def initialize(store):
    with store.connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS intervention_requests (
            id INTEGER PRIMARY KEY, time REAL NOT NULL, issue TEXT NOT NULL,
            account TEXT, request TEXT NOT NULL, status TEXT NOT NULL,
            decided REAL, note TEXT, parent INTEGER)""")


        if not any(r["name"] == "parent" for r in db.execute("PRAGMA table_info(intervention_requests)")):
            db.execute("ALTER TABLE intervention_requests ADD COLUMN parent INTEGER")


def ask(db, issue, account=None):
    if db.execute("""SELECT 1 FROM intervention_requests WHERE issue=? AND account IS ?
                     AND status='PENDING'""", (issue,account)).fetchone():
        return
    question = REQUESTS.get(issue, "Paper research is locked. Review the dashboard and error log before resuming.")
    db.execute("""INSERT INTO intervention_requests(time,issue,account,request,status)
                  VALUES (?,?,?,?,'PENDING')""", (time.time(),issue,account,question))


def resolve(store, request_id, action, note):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM intervention_requests WHERE id=? AND status='PENDING'",
                         (request_id,)).fetchone()
        if not row:
            return False
        db.execute("UPDATE intervention_requests SET status=?,decided=?,note=? WHERE id=?",
                   (action,time.time(),note,request_id))
        if action == "DENIED" and row["parent"] is None:
            explanation = (
                "This issue is still unresolved, so the paper safety lock remains. "
                + row["request"]
                + " If you prefer another approach, describe it in your response."
            )
            db.execute("""INSERT INTO intervention_requests(time,issue,account,request,status,parent)
                         VALUES (?,?,?,?,'PENDING',?)""",
                       (time.time(),row["issue"],row["account"],explanation,request_id))
    return True
