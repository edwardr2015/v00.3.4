"""SQLite persistence. Network responses retain their source and observation time."""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY, payload TEXT NOT NULL, received REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY, source TEXT NOT NULL, received REAL NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candles (
                    source TEXT NOT NULL, time INTEGER NOT NULL, open REAL NOT NULL,
                    high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
                    ticks INTEGER NOT NULL, PRIMARY KEY(source,time)
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY, time REAL NOT NULL, price REAL NOT NULL,
                    volume REAL NOT NULL, received REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS trades_time ON trades(time);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, time REAL NOT NULL, level TEXT NOT NULL,
                    message TEXT NOT NULL
                );

            """)

            if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                db.execute("PRAGMA user_version=1")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, key, payload, archive=False):
        now = time.time()
        raw = json.dumps(payload, allow_nan=False)
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES (?,?,?)", (key, raw, now))
            if archive:
                db.execute(
                    "INSERT INTO observations(source,received,payload) VALUES (?,?,?)",
                    (key, now, raw),
                )

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def event(self, level, message):
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(time,level,message) VALUES (?,?,?)",
                (time.time(), level, message),
            )

    def events(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 20")]

    def save_candles(self, candles):
        with self.connect() as db:
            db.executemany(
                "INSERT OR REPLACE INTO candles VALUES ('BRTI',?,?,?,?,?,?)",
                [
                    (c["time"], c["open"], c["high"], c["low"], c["close"], c["ticks"])
                    for c in candles
                ],
            )

    def candles(self, interval=15, limit=120, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM candles WHERE source='BRTI' AND time<=? ORDER BY time DESC LIMIT ?",
                (now, interval * limit),
            ).fetchall()
        buckets = {}
        for row in reversed(rows):
            t = row["time"] // interval * interval
            if t not in buckets:
                buckets[t] = dict(
                    time=t,
                    open=row["open"],
                    high=row["high"],
                    low=row["low"],
                    close=row["close"],
                    samples=0,
                )
            b = buckets[t]
            b.update(
                high=max(b["high"], row["high"]),
                low=min(b["low"], row["low"]),
                close=row["close"],
                samples=b["samples"] + 1,
            )
        return [
            dict(b, complete=b["samples"] == interval and b["time"] + interval <= now)
            for b in list(buckets.values())[-limit:]
        ]

    def save_trades(self, trades):
        with self.connect() as db:
            db.executemany("INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?)", trades)

    def backup(self, destination):
        with self.connect() as db, sqlite3.connect(destination) as target:
            db.backup(target)
