import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import continuity_audit as ca  # noqa: E402

T0 = 1_790_000_000 // 300 * 300


def slug(i):
    import datetime as dt
    t = dt.datetime.fromtimestamp(T0 + i * 300, dt.timezone.utc)
    return "cpc-btc-updown-5m-" + t.strftime("%Y-%m-%d-%H%Mz").lower()


def make_store(path, labels=(), predictions=()):
    db = sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE state (key TEXT PRIMARY KEY, payload TEXT NOT NULL, received REAL NOT NULL);
      CREATE TABLE predictions (slug TEXT, time REAL, up REAL, details TEXT);
      CREATE TABLE learning_labels (slug TEXT PRIMARY KEY, entry REAL, label_end REAL, x TEXT,
                                    y INTEGER, received REAL);
      CREATE TABLE lab_orders (id INTEGER PRIMARY KEY);
      CREATE TABLE candles (source TEXT, time INTEGER);
    """)
    for s, entry, end, received, x, y in labels:
        db.execute("INSERT INTO learning_labels VALUES (?,?,?,?,?,?)",
                   (s, entry, end, json.dumps(x), y, received))
    for s, t, end, received, y in predictions:
        d = dict(momentum_60=0.1, trend_300=-0.2, volatility=0.001, window_end=end)
        db.execute("INSERT INTO predictions VALUES (?,?,?,?)", (s, t, 0.6, json.dumps(d)))
        db.execute("INSERT INTO state VALUES (?,?,?)",
                   ("outcome_" + s, json.dumps(dict(yes_payout=y, received=received)), received))
    db.commit()
    db.close()


def make_history(path, markets, full_bars=True):
    db = sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE reference15 (time INTEGER PRIMARY KEY, open REAL, high REAL, low REAL,
                                close REAL, ticks INTEGER);
      CREATE TABLE markets (slug TEXT PRIMARY KEY, start INTEGER, end INTEGER, opening REAL,
                            closing REAL, outcome INTEGER, provenance TEXT, payload TEXT);
    """)
    for i, y in markets:
        start = T0 + i * 300
        db.execute("INSERT INTO markets VALUES (?,?,?,?,?,?,?,?)",
                   (slug(i), start, start + 300, 1, 1, y, "test", "{}"))
        if full_bars:
            for k, t in enumerate(range(start + 60 - 315, start + 60, 15)):
                c = 100000 + (k * 7919 % 13)
                db.execute("INSERT OR IGNORE INTO reference15 VALUES (?,?,?,?,?,?)",
                           (t, c, c, c, c, 15))
    db.commit()
    db.close()


def digest(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def label(i, y=1, x=(0.1, 0.2, -6.0), received_delta=5, entry_offset=60):
    start = T0 + i * 300
    return (slug(i), start + entry_offset, start + 300, start + 300 + received_delta, list(x), y)


def test_dedup_conflict_leakage_and_read_only(tmp_path):
    old, new, hist = tmp_path / "old.db", tmp_path / "new.db", tmp_path / "h.db"
    make_store(old, labels=[label(0), label(1, y=0), label(2), label(3, received_delta=-10),
                            label(4, x=(0.1, float("nan"), 1))],
               predictions=[(slug(5), T0 + 5 * 300 + 30, T0 + 6 * 300, T0 + 6 * 300 + 2, 1)])
    make_store(new, labels=[label(0), label(1, y=1)])   # dup of 0, conflict on 1
    make_history(hist, [(2, 1), (6, 0), (7, None)])     # dup of 2, new 6, unverified 7
    before = {p: digest(p) for p in (old, new, hist)}
    report, rows = ca.audit([("old", old), ("new", new)], [("hist", hist)])
    assert {p: digest(p) for p in (old, new, hist)} == before
    assert [c["slug"] for c in report["conflicts"]] == [slug(1)]
    assert {r["slug"] for r in rows} == {slug(0), slug(2), slug(5), slug(6)}
    assert report["unique_eligible_markets"] == 4
    old_info = report["databases"][0]
    assert old_info["rejected"] == {"outcome_before_close": 1, "missing_features": 1}
    assert report["databases"][2]["rejected"] == {"unverified_outcome": 1}
    assert report["gates"]["learning_challenger"]["shortage"] == 196


def test_decision_after_close_rejected_and_history_bars_required(tmp_path):
    s, h = tmp_path / "s.db", tmp_path / "h.db"
    make_store(s, labels=[label(0, entry_offset=300)])
    make_history(h, [(1, 1)], full_bars=False)
    report, rows = ca.audit([("s", s)], [("h", h)])
    assert rows == []
    assert report["databases"][0]["rejected"] == {"decision_not_before_close": 1}
    assert report["databases"][1]["rejected"] == {"incomplete_reference_bars": 1}


def test_gates_and_export(tmp_path):
    s = tmp_path / "s.db"
    make_store(s, labels=[label(i, y=i % 2) for i in range(320)])
    report, rows = ca.audit([("s", s)], [])
    assert report["gates"]["learning_challenger"]["ready"]
    assert report["gates"]["optional_challengers"]["ready"]
    assert not report["gates"]["lstm"]["ready"]
    out = tmp_path / "isolated.sqlite3"
    ca.export(rows, out)
    with sqlite3.connect(out) as db:
        assert db.execute("SELECT count(DISTINCT slug) FROM examples").fetchone()[0] == 320
    with pytest.raises(FileExistsError):
        ca.export(rows, out)


def test_missing_db_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        ca.audit([("x", tmp_path / "nope.db")], [])


def test_v03_verified_markets_labels_predictions_and_detects_conflict(tmp_path):
    s = tmp_path / "v03.db"
    db = sqlite3.connect(s)
    db.executescript("""
      CREATE TABLE state (key TEXT PRIMARY KEY, payload TEXT NOT NULL, received REAL NOT NULL);
      CREATE TABLE predictions (slug TEXT PRIMARY KEY, time REAL, model TEXT, up REAL,
        decision TEXT, reason TEXT, checkpoint INTEGER, details TEXT);
      CREATE TABLE verified_markets (slug TEXT PRIMARY KEY, start REAL, end REAL, outcome TEXT,
        opening REAL, closing REAL, verified_at REAL, source TEXT, evidence TEXT);
    """)
    feats = json.dumps(dict(momentum_60=.1, trend_300=.2, volatility=.001))
    for i, (outcome, state_payout) in enumerate([("UP", None), ("DOWN", 1), ("UP", 1)]):
        start = T0 + i * 300
        db.execute("INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?)",
                   (slug(i), start + 60, "m", .6, "UP", "", 0, feats))
        db.execute("INSERT INTO verified_markets VALUES (?,?,?,?,?,?,?,?,?)",
                   (slug(i), start, start + 300, outcome, 1, 1, start + 305, "t", "{}"))
        if state_payout is not None:
            db.execute("INSERT INTO state VALUES (?,?,?)", ("outcome_" + slug(i),
                       json.dumps(dict(yes_payout=state_payout, received=start + 305)), 0))
    db.commit()
    db.close()
    report, rows = ca.audit([("v03", s)], [])
    assert {r["slug"] for r in rows} == {slug(0), slug(2)}
    assert report["databases"][0]["rejected"] == {"label_source_conflict": 1}
    assert report["databases"][0]["counts"]["verified_markets"] == 3
