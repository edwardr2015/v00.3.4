"""Explicit simulation-only activation, with fresh forward-evidence gates."""

import json
import subprocess
import time
from .models import Models, score
from .research import baseline_probability


def promote(store, config_id, reason, now=None, rollback=False):
    now = time.time() if now is None else now
    if len(reason.strip()) < 10:
        raise ValueError("Explain the promotion in at least 10 characters")
    models = Models(store)
    with store.connect() as db:
        config = db.execute("SELECT * FROM lab_configs WHERE id=?", (config_id,)).fetchone()
    if not config:
        raise ValueError("Unknown research configuration")
    c = dict(config)
    if c["exit"] != "HOLD" or c["stake"] > 3:
        raise ValueError("Primary v0.2 promotions support hold-to-settlement with stake <= $3")
    model, _ = models.load(c["version"])
    if not model["trained"]:
        raise ValueError("A trained challenger and fresh out-of-sample evidence are required")
    with store.connect() as db:
        rows = db.execute(
            """SELECT p.*,m.outcome FROM lab_predictions p JOIN verified_markets m ON p.slug=m.slug
          WHERE p.version=? AND p.checkpoint=? AND p.time>? AND p.time<m.end""",
            (c["version"], c["checkpoint"], model["trained"]),
        ).fetchall()
        done = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM lab_orders WHERE config=? AND status='SETTLED' AND time>?",
                (config_id, model["trained"]),
            )
        ]
    if len(rows) < 100 or len(done) < 30:
        raise ValueError(
            "Need 100 fresh verified predictions and 30 settled forward shadow trades for this exact configuration"
        )
    ys = [int(r["outcome"] == "UP") for r in rows]
    metric = score([r["up"] for r in rows], ys)
    baseline = score([baseline_probability(json.loads(r["features"])) for r in rows], ys)
    if metric["brier"] >= baseline["brier"] or sum(o["pnl"] for o in done) <= 0:
        raise ValueError(
            "Fresh validation must improve baseline Brier score and have positive net simulated P&L"
        )
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (OSError, subprocess.SubprocessError):
        commit = "NOT AVAILABLE"
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        experiment = db.execute("SELECT payload FROM state WHERE key='v2_experiment'").fetchone()
        experiment = json.loads(experiment[0]) if experiment else None
        freeze = db.execute(
            "SELECT payload FROM state WHERE key='v2_initial_freeze_end'"
        ).fetchone()
        freeze = json.loads(freeze[0]) if freeze else experiment["end"] if experiment else None
        if freeze is not None and now < freeze:
            raise ValueError("Primary model is frozen for the initial seven-day experiment")
        if db.execute(
            "SELECT 1 FROM v2_orders WHERE (account='primary' OR account LIKE 'adaptive-%') AND status='OPEN'"
        ).fetchone():
            raise ValueError("Cannot change primary strategy while a position is open")
        previous = db.execute("SELECT * FROM strategy_changes ORDER BY id DESC LIMIT 1").fetchone()
        exact = dict(
            model=c["version"],
            checkpoints=[c["checkpoint"]],
            exit=c["exit"],
            stake=c["stake"],
            research_config=config_id,
            artifact_sha256=model["sha256"],
            validation=metric,
        )
        db.execute(
            "INSERT INTO strategy_changes(time,action,previous,model,reason,config,git_commit) VALUES (?,?,?,?,?,?,?)",
            (
                now,
                "ROLLBACK" if rollback else "PROMOTE",
                previous["model"],
                c["version"],
                reason,
                json.dumps(exact),
                commit,
            ),
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS approved_configs (id TEXT PRIMARY KEY,approved REAL,config TEXT,evidence TEXT)"
        )
        db.execute(
            "INSERT OR REPLACE INTO approved_configs VALUES (?,?,?,?)",
            (config_id, now, json.dumps(exact), json.dumps(metric)),
        )
        if experiment:
            db.execute(
                "CREATE TABLE IF NOT EXISTS experiment_segments (id INTEGER PRIMARY KEY,time REAL,payload TEXT)"
            )
            db.execute(
                "INSERT INTO experiment_segments(time,payload) VALUES (?,?)",
                (now, json.dumps(experiment)),
            )
        from .paper import PaperEngine

        PaperEngine._state(db, "v2_experiment", None)
        PaperEngine._state(db, "mode", "OFF")
    return exact


def rollback_strategy(store, strategy_id, reason, now=None):
    """Restore an exact previously active configuration, never a new candidate."""
    now = time.time() if now is None else now
    if len(reason.strip()) < 10:
        raise ValueError("Explain the rollback in at least 10 characters")
    with store.connect() as db:
        previous = db.execute(
            "SELECT * FROM strategy_changes WHERE id=?", (strategy_id,)
        ).fetchone()
        current = db.execute("SELECT * FROM strategy_changes ORDER BY id DESC LIMIT 1").fetchone()
    if not previous or previous["id"] >= current["id"]:
        raise ValueError("Select a previously active strategy")
    config = json.loads(previous["config"])
    from .v2paper import MODEL

    if previous["model"] != MODEL:
        record, _ = Models(store).load(previous["model"])
        if record["sha256"] != config.get("artifact_sha256"):
            raise ValueError("Previously approved model artifact changed")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT payload FROM state WHERE key='v2_experiment'").fetchone()
        experiment = json.loads(row[0]) if row else None
        freeze = db.execute(
            "SELECT payload FROM state WHERE key='v2_initial_freeze_end'"
        ).fetchone()
        freeze = json.loads(freeze[0]) if freeze else experiment["end"] if experiment else None
        if freeze is not None and now < freeze:
            raise ValueError("Primary model is frozen for the seven-day experiment")
        if db.execute(
            "SELECT 1 FROM v2_orders WHERE (account='primary' OR account LIKE 'adaptive-%') AND status='OPEN'"
        ).fetchone():
            raise ValueError("Cannot roll back while a primary position is open")
        db.execute(
            "INSERT INTO strategy_changes(time,action,previous,model,reason,config,git_commit) VALUES (?,?,?,?,?,?,?)",
            (
                now,
                "ROLLBACK",
                current["model"],
                previous["model"],
                reason,
                previous["config"],
                previous["git_commit"],
            ),
        )
        if experiment:
            db.execute(
                "CREATE TABLE IF NOT EXISTS experiment_segments (id INTEGER PRIMARY KEY,time REAL,payload TEXT)"
            )
            db.execute(
                "INSERT INTO experiment_segments(time,payload) VALUES (?,?)",
                (now, json.dumps(experiment)),
            )
        from .paper import PaperEngine

        PaperEngine._state(db, "v2_experiment", None)
        PaperEngine._state(db, "mode", "OFF")
    return config
