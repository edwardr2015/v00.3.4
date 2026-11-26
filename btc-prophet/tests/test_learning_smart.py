import json
import time
from pathlib import Path

import pytest

from prophet.learning import Learning
from prophet.smart import SmartResearch
from test_paper import seed
from test_reporting_backtest import historical_rows
from prophet.backtest import replay


def test_training_and_rollback_are_shadow_only(tmp_path):
    s, e, now = seed(tmp_path)
    learner = Learning(s, tmp_path / "missing.sqlite")
    start = time.time() - 100000
    with s.connect() as db:
        for i in range(210):
            db.execute(
                "INSERT INTO learning_labels VALUES (?,?,?,?,?,?)",
                (
                    f"fixture-{i}",
                    start + i * 300,
                    start + i * 300 + 240,
                    json.dumps([i % 3, i % 4, -5.0]),
                    i % 2,
                    start + i * 300 + 241,
                ),
            )
    learner.train()
    version = s.get("learning_status")["version"]
    learner.rollback(version)
    assert s.get("mode") == "OFF" and not e.summary()["orders"]
    with s.connect() as db:
        r = db.execute("SELECT * FROM model_runs").fetchone()
    assert r["training_end"] <= r["validation_start"]
    Path(r["path"]).write_text("{}")
    with pytest.raises(ValueError, match="integrity"):
        learner.rollback(version)


def test_shadow_profit_targets_and_settlement_never_change_main_cash(tmp_path):
    s, e, now = seed(tmp_path)
    e.mode("SIMULATE", now)
    e.tick(now)
    smart = SmartResearch(s, e)
    s.put("smart_exit_enabled", True)
    smart.tick(now)
    accounts = e.summary()["accounts"]
    book = s.get("book")
    book.update(
        timestamp=now + 10,
        bids=[dict(price=0.8, quantity=1000)],
        asks=[dict(price=0.81, quantity=1000)],
    )
    s.put("book", book)
    smart.tick(now + 10)
    smart.tick(now + 10)
    with s.connect() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM smart_experiments")]
    assert len(rows) == 21
    assert all(
        json.loads(r["state"])["status"] == "EXITED" for r in rows if r["kind"] == "EXIT_1.4"
    )
    s.put(
        "outcome_synthetic-test-market",
        dict(slug="synthetic-test-market", outcome="DOWN", yes_payout=0),
    )
    smart.tick(now + 300)
    with s.connect() as db:
        states = [
            json.loads(r[0])
            for r in db.execute("SELECT state FROM smart_experiments WHERE kind='HOLD'")
        ]
    assert all(r["status"] == "SETTLED" and r["pnl"] < 0 for r in states)
    assert e.summary()["accounts"] == accounts


def test_flip_once_and_impossible_target(tmp_path):
    s, e, now = seed(tmp_path)
    e.mode("SIMULATE", now)
    e.tick(now)
    smart = SmartResearch(s, e)
    s.put("smart_exit_enabled", True)
    smart.tick(now)
    e.probability = lambda now: (0.01, {})
    book = s.get("book")
    book.update(
        timestamp=now + 10,
        bids=[dict(price=0.8, quantity=1000)],
        asks=[dict(price=0.81, quantity=1000)],
    )
    s.put("book", book)
    smart.tick(now + 10)
    e.probability = lambda now: (0.99, {})
    book["timestamp"] = now + 11
    s.put("book", book)
    smart.tick(now + 11)
    with s.connect() as db:
        states = [
            json.loads(r[0])
            for r in db.execute("SELECT state FROM smart_experiments WHERE kind='FLIP'")
        ]
        assert (
            db.execute("SELECT count(*) FROM smart_actions WHERE action='FLIP'").fetchone()[0] == 3
        )
    assert all(r["flips"] == 1 for r in states)
    with s.connect() as db:
        db.execute("UPDATE orders SET cost=quantity*.9")
        db.execute("DELETE FROM smart_experiments")
    smart.tick(now)
    with s.connect() as db:
        states = [
            json.loads(r[0])
            for r in db.execute("SELECT state FROM smart_experiments WHERE kind='EXIT_1.4'")
        ]
    assert all(r["target_impossible"] for r in states)


def test_sampled_target_rescues_only_a_trade_that_reaches_it():
    rows = historical_rows(1)
    rows[0]["quote_trace"] = [dict(time=rows[0]["decision_time"] + 60, up_ask=0.8, down_ask=0.21)]
    hold = replay(rows, lambda x: 0.9, checkpoint=False)
    target = replay(rows, lambda x: 0.9, checkpoint=False, exit_factor=1.4)
    assert hold["accounts"][0]["realized"] < 0 < target["accounts"][0]["realized"]
    rows[0]["quote_trace"] = []
    assert (
        replay(rows, lambda x: 0.9, checkpoint=False, exit_factor=1.4)["accounts"][0]["realized"]
        == hold["accounts"][0]["realized"]
    )
