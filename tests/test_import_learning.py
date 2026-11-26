import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "btc-prophet"))
sys.path.insert(0, str(ROOT / "tests"))
import import_learning as il  # noqa: E402
from test_continuity_audit import label, make_history, make_store, slug  # noqa: E402


def digest(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def current_db(folder):
    from prophet.app import create_app
    from prophet.history import History
    path = Path(folder) / "timing" / "prophet.sqlite3"
    path.parent.mkdir(parents=True)
    app = create_app(str(path), collect=False, auth_required=False)
    app.state.paper.recover_startup(1000)
    hist = History(Path(folder) / "timing" / "history.sqlite3", Path(folder) / "timing" / "cache")
    hist.close()
    return path, Path(folder) / "timing" / "history.sqlite3"


def test_imports_labels_into_new_copy_without_touching_inputs(tmp_path):
    old = tmp_path / "old.sqlite3"
    make_store(old, labels=[label(i, y=i % 2) for i in range(40)] + [label(40, received_delta=-5)],
               predictions=[(slug(41), 0, 0, 0, 1)])           # unusable prediction timing -> rejected
    target, _ = current_db(tmp_path)
    with sqlite3.connect(target) as db:                        # target already knows two markets
        db.execute("INSERT INTO learning_labels VALUES (?,?,?,?,?,?)", (slug(0), 1, 2, "[0,0,0]", 0, 3))
        db.execute("INSERT INTO learning_labels VALUES (?,?,?,?,?,?)", (slug(1), 1, 2, "[0,0,0]", 0, 3))  # conflict
        balance = db.execute("SELECT balance FROM unified_account").fetchone()[0]
    before = {p: digest(p) for p in (old, target)}
    out = tmp_path / "timing" / "prophet-imported.sqlite3"
    r = il.import_labels(old, target, out)
    assert {p: digest(p) for p in (old, target)} == before
    assert r["imported"] == 38 and r["duplicates"] == 1
    assert [c["slug"] for c in r["conflicts"]] == [slug(1)]
    assert r["source_rejected"]["outcome_before_close"] == 1
    assert r["target_labels_after"] == 40 and r["integrity"] == "ok"
    with sqlite3.connect(out) as db:
        assert db.execute("SELECT balance FROM unified_account").fetchone()[0] == balance
        assert db.execute("SELECT y FROM learning_labels WHERE slug=?", (slug(1),)).fetchone()[0] == 0
    with pytest.raises(FileExistsError):
        il.import_labels(old, target, out)


def test_app_trains_on_the_imported_copy(tmp_path, monkeypatch):
    for name in ("PROPHET_MIN_TRAINING_MARKETS", "PROPHET_MIN_FIT_EXAMPLES", "PROPHET_MIN_SPLIT_MARKETS"):
        monkeypatch.delenv(name, raising=False)
    import time
    from prophet.learning import Learning
    from prophet.store import Store
    old = tmp_path / "old.sqlite3"
    base = time.time() - 200000
    make_store(old)
    with sqlite3.connect(old) as s:
        for i in range(45):
            t = base + i * 300
            s.execute("INSERT INTO learning_labels VALUES (?,?,?,?,?,?)",
                      (slug(i), t + 60, t + 300, json.dumps([i % 3 - 1, (i % 5) / 2 - 1, -6.0]), i % 2, t + 305))
    target, _ = current_db(tmp_path)
    out = tmp_path / "timing" / "prophet-imported.sqlite3"
    assert il.import_labels(old, target, out)["imported"] == 45
    store = Store(str(out))
    Learning(store, tmp_path / "missing-history.sqlite3").train()
    status = store.get("learning_status")
    assert status["status"] == "SHADOW" and status["examples"] == 45


def test_history_import_merges_markets_and_bars_and_flags_conflicts(tmp_path):
    old_h = tmp_path / "old-history.sqlite3"
    make_history(old_h, [(0, 1), (1, 0), (2, None)])
    _, target_h = current_db(tmp_path)
    make_history_target = sqlite3.connect(target_h)
    make_history_target.execute("INSERT INTO markets(slug,start,end,opening,closing,outcome,provenance,payload) "
                                "VALUES (?,?,?,?,?,?,?,?)", (slug(1), 0, 0, 1, 1, 1, "t", "{}"))   # conflict
    make_history_target.commit(); make_history_target.close()
    out = tmp_path / "timing" / "history-imported.sqlite3"
    before = digest(target_h)
    r = il.import_history(old_h, target_h, out)
    assert digest(target_h) == before
    assert r["tables"]["markets"] == dict(imported=1, duplicates=0, conflicts=1)
    assert r["tables"]["reference15"]["imported"] > 0 and r["integrity"] == "ok"
