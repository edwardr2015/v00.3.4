"""Optional hourly challengers. They can only enter shadow evaluation, never execution."""

import asyncio
import hashlib
import json
import math
import time
from pathlib import Path

from .history import History
from .research import (
    baseline_probability,
    challenger_probability,
    examples,
    fit_challenger,
    metrics,
)


def matched_forecasts(db, now):
    """Pair the challenger with the stored forecast from the same feature capture."""
    rows = db.execute("""SELECT p.up AS baseline,s.up AS challenger,s.outcome
        FROM model_shadows s JOIN predictions p ON p.slug=s.slug
        JOIN verified_markets v ON v.slug=s.slug
        WHERE s.version=(SELECT json_extract(payload,'$') FROM state WHERE key='shadow_version')
        AND s.outcome IN (0,1) AND p.up IS NOT NULL
        AND p.time=json_extract(s.details,'$.feature_time')
        AND p.time<=s.time AND s.time<v.end AND v.verified_at<=?
        AND s.outcome=(v.outcome='UP')""", (now,)).fetchall()
    n = len(rows)
    result = dict(matched=n)
    for model in ("baseline","challenger"):
        result[model+"_right"] = sum(r[model]!=.5 and (r[model]>.5)==bool(r["outcome"]) for r in rows)
        result[model+"_brier"] = sum((r[model]-r["outcome"])**2 for r in rows)/n if n else None
    return result


class Learning:
    def __init__(self, store, history_path="data/history.sqlite3"):
        self.store = store
        self.history_path = Path(history_path)
        self.task = None
        self.last_learning_sample = None
        self.root = Path(store.path).parent / "models"
        self.root.mkdir(exist_ok=True)
        with store.connect() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS learning_labels (
                slug TEXT PRIMARY KEY, entry REAL, label_end REAL, x TEXT, y INTEGER, received REAL);
              CREATE TABLE IF NOT EXISTS model_runs (
                id INTEGER PRIMARY KEY, version TEXT UNIQUE, time REAL, training_end REAL,
                validation_start REAL, brier REAL, baseline_brier REAL, path TEXT, sha256 TEXT, details TEXT);
              CREATE TABLE IF NOT EXISTS model_shadows (
                version TEXT, slug TEXT, time REAL, up REAL, outcome INTEGER, brier REAL,
                details TEXT, PRIMARY KEY(version,slug));
            """)

    def collect_labels(self):
        with self.store.connect() as db:
            for p in db.execute("SELECT * FROM predictions WHERE up IS NOT NULL").fetchall():
                value = db.execute(
                    "SELECT payload FROM state WHERE key=?", ("outcome_" + p["slug"],)
                ).fetchone()
                if not value:
                    continue
                o = json.loads(value[0])
                d = json.loads(p["details"])
                if o.get("received", 0) < p["time"] or d.get("window_end", 0) <= p["time"]:
                    continue
                if not all(k in d for k in ("momentum_60", "trend_300", "volatility")):
                    continue
                x = [d["momentum_60"], d["trend_300"], math.log(max(d["volatility"], 1e-7))]
                db.execute(
                    "INSERT OR IGNORE INTO learning_labels VALUES (?,?,?,?,?,?)",
                    (
                        p["slug"],
                        p["time"],
                        d["window_end"],
                        json.dumps(x),
                        o["yes_payout"],
                        o["received"],
                    ),
                )
                for shadow in db.execute(
                    "SELECT * FROM model_shadows WHERE slug=? AND outcome IS NULL", (p["slug"],)
                ).fetchall():
                    db.execute(
                        "UPDATE model_shadows SET outcome=?,brier=? WHERE version=? AND slug=?",
                        (
                            o["yes_payout"],
                            (shadow["up"] - o["yes_payout"]) ** 2,
                            shadow["version"],
                            p["slug"],
                        ),
                    )

    def train(self):
        now = time.time()
        rows = []
        if self.history_path.exists():
            h = History(self.history_path, self.history_path.parent / "history-cache")
            try:
                bounds = h.db.execute("SELECT min(start),max(end) FROM markets").fetchone()
                if bounds[0] is not None:
                    rows, _ = examples(h, bounds[0], min(bounds[1], now), entry_offset=60)
            finally:
                h.close()
        with self.store.connect() as db:
            forward = [
                dict(r)
                for r in db.execute("SELECT * FROM learning_labels WHERE received<=?", (now,))
            ]
        seen = {r["slug"] for r in rows}
        for r in forward:
            if r["slug"] not in seen:
                rows.append(
                    dict(
                        slug=r["slug"],
                        decision_time=r["entry"],
                        label_end=r["label_end"],
                        x=json.loads(r["x"]),
                        y=r["y"],
                        quote_age=None,
                    )
                )
        rows = sorted(rows, key=lambda r: r["decision_time"])
        if len(rows) < 200:
            self.store.put(
                "learning_status",
                dict(status="WAITING", examples=len(rows), message="Need 200 labelled examples"),
            )
            return
        cut = rows[int(len(rows) * 0.8)]["decision_time"]
        train = [r for r in rows if r["label_end"] <= cut]
        validation = [r for r in rows if r["decision_time"] >= cut]
        model = fit_challenger(train)
        score = metrics(validation, lambda x: challenger_probability(model, x))
        baseline = metrics(validation, baseline_probability)
        fingerprint = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
        version = f"challenger-{int(now)}-{fingerprint[:8]}"
        model.update(
            version=version,
            dataset_sha256=fingerprint,
            trained_at=now,
            training_end=max(r["label_end"] for r in train),
            promoted=False,
        )
        raw = json.dumps(model, sort_keys=True).encode()
        path = self.root / (version + ".json")
        temp = path.with_suffix(".tmp")
        temp.write_bytes(raw)
        temp.replace(path)
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO model_runs(version,time,training_end,validation_start,brier,baseline_brier,path,sha256,details) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    version,
                    now,
                    model["training_end"],
                    cut,
                    score["brier_score"],
                    baseline["brier_score"],
                    str(path.resolve()),
                    hashlib.sha256(raw).hexdigest(),
                    json.dumps(
                        dict(
                            training_examples=len(train),
                            validation=score,
                            baseline=baseline,
                            training_only_preprocessing=True,
                        )
                    ),
                ),
            )
        from .models import Models
        import os

        registry = Models(self.store)
        registry.sync_logistic()
        for kind in os.getenv(
            "PROPHET_RESEARCH_MODELS", "ensemble,random_forest,gradient_boosting,xgboost,lightgbm"
        ).split(","):
            if not kind.strip():
                continue
            try:
                registry.train(kind.strip(), rows, now=now)
            except (ValueError, ImportError, OSError, RuntimeError) as exc:
                self.store.event("INFO", "Optional challenger " + kind + ": " + str(exc))
        self.store.put("shadow_version", version)
        self.store.put(
            "learning_status",
            dict(
                status="SHADOW",
                version=version,
                examples=len(rows),
                brier=score["brier_score"],
                active_model_unchanged=True,
            ),
        )

    def rollback(self, version):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM model_runs WHERE version=?", (version,)).fetchone()
        if not row:
            raise ValueError("Unknown shadow model version")
        path = Path(row["path"]).resolve()
        if (
            not path.is_relative_to(self.root.resolve())
            or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]
        ):
            raise ValueError("Model artifact integrity check failed")
        self.store.put("shadow_version", version)
        self.store.event("INFO", "Manual shadow rollback: " + version)

    def shadow(self):
        if self.store.get("mode") not in ("LEARN", "SIMULATE"):
            return
        m = self.store.get("market")
        now = time.time()
        if not m:
            return
        from .providers import validate_market

        if not validate_market(m, now):
            return
        with self.store.connect() as db:
            pred = db.execute("SELECT * FROM predictions WHERE slug=?", (m["slug"],)).fetchone()
            run = db.execute(
                "SELECT * FROM model_runs WHERE version=?", (self.store.get("shadow_version"),)
            ).fetchone()
        if not pred or not run or pred["up"] is None:
            return
        raw = Path(run["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != run["sha256"]:
            raise ValueError("Model artifact modified")
        d = json.loads(pred["details"])
        x = [d["momentum_60"], d["trend_300"], math.log(max(d["volatility"], 1e-7))]
        p = challenger_probability(json.loads(raw), x)
        with self.store.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO model_shadows VALUES (?,?,?,?,NULL,NULL,?)",
                (
                    run["version"],
                    m["slug"],
                    now,
                    p,
                    json.dumps(dict(feature_time=pred["time"], source="FORWARD_SHADOW")),
                ),
            )

    def record_learning_time(self, now):
        from .adaptive_entry import get_state
        from .v2paper import Engine

        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if get_state(db, "mode") not in ("LEARN", "SIMULATE") or get_state(db, "emergency"):
                self.last_learning_sample = None
                return
            clock = get_state(db, "competition_learning_clock") or dict(started=now, seconds=0)
            if self.last_learning_sample is not None and 0 < now-self.last_learning_sample <= 20:
                clock["seconds"] += now-self.last_learning_sample
            self.last_learning_sample = now
            Engine._state(db, "competition_learning_clock", clock)

    async def start(self):
        self.task = asyncio.create_task(self.loop())

    async def loop(self):
        while True:
            try:
                self.record_learning_time(time.time())
                self.collect_labels()
                if self.store.get("mode") in ("LEARN", "SIMULATE"):
                    status = self.store.get("learning_status") or {}
                    interval = 300 if status.get("status") == "WAITING" else 12 * 3600
                    if time.time() - self.store.get("learning_last_attempt", 0) >= interval:
                        self.store.put("learning_last_attempt", time.time())
                        await asyncio.to_thread(self.train)
                    self.shadow()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.put("learning_status", dict(status="ERROR", message=str(exc)))
                self.store.event("ERROR", "Challenger training/shadow error: " + str(exc))
            await asyncio.sleep(10)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
