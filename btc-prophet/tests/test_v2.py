"""v0.2 tests are synthetic; none are live-API reliability claims."""

import asyncio
import io
from datetime import datetime
import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from prophet import analytics
from prophet.v2paper import Engine
from prophet.v2risk import local_day, daily_loss, entry_block
from prophet.app import create_app
from prophet.auth import configure
from test_paper import seed


def setup(tmp_path):
    s, _, now = seed(tmp_path)
    e = Engine(s)
    e.probability = lambda now, **kwargs: (0.8, dict(momentum_60=1, trend_300=1, volatility=0.001))
    return s, e, now


def verify(s, now, side="UP"):
    m = s.get("market")
    start = datetime.fromisoformat(m["assetPriceTerms"]["windowStart"]).timestamp()
    o = dict(
        slug=m["slug"],
        opening=100,
        closing=101 if side == "UP" else 99,
        outcome=side,
        yes_payout=int(side == "UP"),
        received=now + 300,
        source="SYNTHETIC official settlement fixture",
    )
    analytics.record_market(s, o, start, start + 300)


def test_timing_checkpoint_is_independent_and_does_not_trade_primary(tmp_path):
    from prophet.lab import Lab

    s, e, now = setup(tmp_path)
    lab = Lab(s, e)
    e.mode("SIMULATE", now)
    e.tick(now)
    lab.tick(now)
    e.tick(now)
    lab.tick(now)
    assert {o["account"] for o in e.summary()["orders"]} == {"adaptive-1","adaptive-2","adaptive-3"}
    with s.connect() as db:
        assert db.execute("SELECT count(*) FROM v2_decisions").fetchone()[0] == 6
        assert db.execute("SELECT count(*) FROM v2_decisions WHERE reason='MISSED_CHECKPOINT'").fetchone()[0] == 5
        assert db.execute("SELECT count(*) FROM lab_orders").fetchone()[0] == 3
        assert db.execute("SELECT count(*) FROM lab_skips WHERE config LIKE '%momentum-v2%'").fetchone()[0] == 15
    verify(s, now)
    lab.exit_and_settle(now + 300)
    with s.connect() as db:
        before = db.execute("SELECT cash FROM lab_configs ORDER BY id").fetchall()
        assert db.execute("SELECT count(*) FROM lab_orders WHERE status='SETTLED'").fetchone()[0] == 3
    lab.exit_and_settle(now + 300)
    with s.connect() as db:
        assert db.execute("SELECT cash FROM lab_configs ORDER BY id").fetchall() == before


def test_learn_emergency_review_and_restart_never_fund(tmp_path):
    s, e, now = setup(tmp_path)
    e.mode("LEARN")
    e.tick(now)
    assert not e.summary()["orders"]
    e.mode("SIMULATE", now)

    async def restart():
        await e.start()
        await e.close()

    asyncio.run(restart())
    assert s.get("mode") == "OFF"
    assert all(a["halted"] for a in e.summary()["accounts"])
    e.review()
    assert all(a["balance"] == a["starting_cash"] and not a["halted"] for a in e.summary()["accounts"])
    s.put("emergency", True)
    with pytest.raises(ValueError):
        e.mode("SIMULATE", now)
    with pytest.raises(ValueError):
        e.mode("LIVE", now)


def test_chicago_daily_limit_dst_gross_losses_and_open_exposure():
    def ts(s):
        return datetime.fromisoformat(s).timestamp()

    before = ts("2026-11-22T05:59:59+00:00")
    after = before + 1
    assert local_day(before) == "2026-11-21" and local_day(after) == "2026-11-22"
    # DST fall-back: both repeated 01:30 times belong to the same local date.
    assert local_day(ts("2026-11-01T06:30:00+00:00")) == local_day(ts("2026-11-01T07:30:00+00:00"))
    orders = [
        dict(status="SETTLED", pnl=-18, settled=before, cost=18),
        dict(status="SETTLED", pnl=50, settled=before, cost=1),
    ]
    a = dict(halted=None, balance=100, streak=20, realized=-50)
    assert entry_block(a, orders, 3, before) == "DAILY_LOSS_RESERVE"
    assert entry_block(a, orders, 2, before) is None  # no consecutive or total-loss stop
    assert entry_block(a, orders, 3, after) is None
    orders.append(dict(status="OPEN", pnl=None, settled=None, cost=3))
    assert daily_loss(orders, after) == 3
    assert entry_block(a, orders, 3, after) == "ONE_POSITION_LIMIT"
    assert entry_block(a, [], 4, after) == "MAXIMUM_STAKE_3"
    assert entry_block(dict(a, balance=2), [], 3, after) == "INSUFFICIENT_CASH"


def test_verified_history_skips_streaks_and_analysis_readonly(tmp_path):
    s, e, now = setup(tmp_path)
    assert analytics.analyze(s)["status"] == "Insufficient Data"
    for i in range(55):
        o = dict(
            slug=f"fixture-{i}",
            opening=100,
            closing=101,
            outcome="UP",
            yes_payout=1,
            received=now + i * 300 + 300,
            source="SYNTHETIC",
        )
        analytics.record_market(s, o, now + i * 300, now + i * 300 + 300)
    board = analytics.history(s)
    assert board["count"] == 50 and board["markets"][0]["slug"] == "fixture-5"
    assert (
        analytics.streaks(["WIN", "SKIP", "WIN", "LOSS", "SKIP", "LOSS"], "WIN", "LOSS")["length"]
        == 2
    )
    assert analytics.analyze(s)["status"] == "Trend Detected"
    assert not e.summary()["orders"]
    o.update(slug="bad", closing=99)
    with pytest.raises(ValueError):
        analytics.record_market(s, o, now, now + 300)
    assert analytics.history(s)["count"] == 50


def test_auth_readonly_controls_logout_and_export(tmp_path):
    app = create_app(str(tmp_path / "private.sqlite"), collect=False)
    configure(app.state.auth.path, "strong-test-password", "viewer-test-password")
    headers = {"X-Prophet-Control": "local-dashboard"}
    with TestClient(app) as c:
        assert c.get("/api/paper").status_code == 401
        assert c.get("/", follow_redirects=False).status_code == 303
        assert (
            c.post(
                "/api/login", headers=headers, json=dict(username="admin", password="wrong")
            ).status_code
            == 401
        )
        r = c.post(
            "/api/login",
            headers=headers,
            json=dict(username="admin", password="strong-test-password"),
        )
        assert (
            r.status_code == 200
            and "HttpOnly" in r.headers["set-cookie"]
            and "SameSite=strict" in r.headers["set-cookie"]
        )
        assert c.post("/api/control/stop", headers=headers).status_code == 200
        x = c.get("/api/export.xlsx")
        assert x.status_code == 200
        wb = load_workbook(io.BytesIO(x.content))
        assert "Primary Equity" in wb.sheetnames
        assert wb["Trade History"].max_row == 1
        assert (
            c.post("/api/analyze-trends", headers=headers).json()["status"] == "Insufficient Data"
        )
        c.post("/api/logout", headers=headers)
        assert c.get("/api/paper").status_code == 401
        assert (
            c.post(
                "/api/login",
                headers=headers,
                json=dict(username="viewer", password="viewer-test-password"),
            ).status_code
            == 200
        )
        assert c.get("/api/paper").status_code == 200
        assert c.post("/api/control/emergency", headers=headers).status_code == 403


def test_lab_162_manifest_unknown_results_and_learn_restriction(tmp_path):
    from prophet.lab import Lab

    s, e, now = setup(tmp_path)
    lab = Lab(s, e)
    ranking = lab.leaderboard()
    assert len(ranking["configurations"]) == 162
    assert all(c["net_profit"] is None for c in ranking["configurations"])
    e.mode("LEARN")
    e.tick(now)
    lab.tick(now)
    with s.connect() as db:
        assert db.execute("SELECT count(*) FROM lab_orders").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM lab_predictions").fetchone()[0] > 0
    assert not e.summary()["orders"]


def test_lab_holds_even_when_a_higher_bid_would_exit(tmp_path):
    from prophet.lab import Lab

    s, e, now = setup(tmp_path)
    lab = Lab(s, e)
    e.mode("SIMULATE", now)
    e.tick(now)
    lab.tick(now)
    before = e.summary()["accounts"]
    with s.connect() as db:
        assert db.execute("SELECT count(*) FROM lab_orders").fetchone()[0] == 3
    b = s.get("book")
    b.update(
        timestamp=now + 1,
        bids=[dict(price=0.8, quantity=1000)],
        asks=[dict(price=0.81, quantity=1000)],
    )
    s.put("book", b)
    r = s.get("reference")
    r["timestamp"] = now + 1
    s.put("reference", r)
    lab.tick(now + 1)
    with s.connect() as db:
        exited = db.execute(
            "SELECT count(*) FROM lab_orders o JOIN lab_configs c ON o.config=c.id WHERE o.status='SETTLED' AND c.exit='TAKE_PROFIT_1.4'"
        ).fetchone()[0]
        assert exited == 0
        assert db.execute("SELECT count(*) FROM lab_orders WHERE status='OPEN'").fetchone()[0] >= 3
    assert e.summary()["accounts"] == before
    verify(s, now)
    lab.tick(now + 300)
    assert any(c["net_profit"] is not None for c in lab.leaderboard()["configurations"])


def test_model_chronology_artifact_integrity_and_promotion_blocked(tmp_path):
    from prophet.models import Models
    from prophet.promotion import promote
    from prophet.lab import Lab

    s, e, now = setup(tmp_path)
    models = Models(s)
    rows = [
        dict(
            slug=f"m-{i}",
            decision_time=1000 + i * 300,
            label_end=1300 + i * 300,
            x=[i % 5, i % 7, -5],
            y=i % 2,
        )
        for i in range(350)
    ]
    version, metrics = models.train("logistic", rows, now=now)
    assert metrics["training_end"] <= metrics["validation_start"] < metrics["test_start"]
    assert 0 <= models.predict(version, [1, 2, -5]) <= 1
    lab = Lab(s, e)
    config = next(
        c
        for c in lab.leaderboard()["configurations"]
        if c["version"] == version and c["exit"] == "HOLD"
    )
    with pytest.raises(ValueError, match="fresh verified"):
        promote(s, config["id"], "Test promotion evidence", now)
    row, artifact = models.load(version)
    from pathlib import Path

    Path(row["path"]).write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="integrity"):
        models.predict(version, [1, 2, -5])
    assert not e.summary()["orders"]


def test_utc_timestamps_and_candles_never_include_future(tmp_path):
    from prophet.providers import timestamp

    s, e, now = setup(tmp_path)
    with pytest.raises(ValueError):
        timestamp("2026-11-22T12:00:00")
    with pytest.raises(ValueError):
        timestamp(float("nan"))
    assert timestamp("2026-11-22T12:00:00Z") == timestamp("2026-11-22T06:00:00-06:00")
    s.save_candles([dict(time=int(now) + 1000, open=999, high=999, low=999, close=999, ticks=1)])
    assert all(b["time"] <= now for b in s.candles(now=now))


def test_stale_model_change_and_empty_book_prevent_primary_entries(tmp_path):
    s, e, now = setup(tmp_path)
    e.mode("SIMULATE", now)
    e.tick(now + 31)
    assert s.get("mode") == "SIMULATE" and not e.summary()["orders"]
    assert not any(a["halted"] for a in e.summary()["accounts"])
    e.review()
    e.mode("SIMULATE", now)
    s.put("v2_experiment",dict(start=now,end=now+7*86400,model_hash="changed",status="RUNNING"))
    e.tick(now)
    assert s.get("mode") == "OFF"
    assert all(a["halted"] == "ACTIVE_MODEL_CHANGED" for a in e.summary()["accounts"])


def test_public_display_never_grants_control_access(tmp_path, monkeypatch):
    monkeypatch.setenv("PROPHET_PUBLIC_DISPLAY", "1")
    app = create_app(str(tmp_path / "display.sqlite"), collect=False)
    with TestClient(app) as c:
        assert c.get("/api/display").status_code == 200
        assert "accounts" not in c.get("/api/display").json()
        assert c.get("/display").status_code == 200
        assert c.get("/api/strategies").status_code == 401
        assert (
            c.post(
                "/api/control/start", headers={"X-Prophet-Control": "local-dashboard"}
            ).status_code
            == 401
        )


def test_promotion_freeze_forward_evidence_segments_and_exact_rollback(tmp_path):
    import json
    from prophet.models import Models
    from prophet.lab import Lab
    from prophet.promotion import promote, rollback_strategy

    s, e, now = setup(tmp_path)
    registry = Models(s)
    training = [
        dict(
            slug=f"train-{i}",
            decision_time=1000 + i * 300,
            label_end=1300 + i * 300,
            x=[i % 3, i % 5, -5],
            y=i % 2,
        )
        for i in range(350)
    ]
    version, _ = registry.train("logistic", training, now=now - 40000)
    lab = Lab(s, e)
    c = next(
        c
        for c in lab.leaderboard()["configurations"]
        if c["version"] == version
        and c["checkpoint"] == 60
        and c["exit"] == "HOLD"
        and c["stake"] == 3
    )
    for i in range(100):
        t = now - 39900 + i * 300
        up = i % 2 == 0
        o = dict(
            slug=f"forward-{i}",
            opening=100,
            closing=101 if up else 99,
            outcome="UP" if up else "DOWN",
            yes_payout=int(up),
            received=t + 241,
            source="SYNTHETIC FORWARD TEST",
        )
        analytics.record_market(s, o, t - 60, t + 240)
        with s.connect() as db:
            db.execute(
                "INSERT INTO lab_predictions VALUES (?,?,?,?,?,?,?)",
                ("logistic", version, o["slug"], 60, t, 0.9 if up else 0.1, json.dumps([0, 0, -5])),
            )
            if i < 30:
                db.execute(
                    """INSERT INTO lab_orders(config,slug,side,time,quantity,remaining,cost,proceeds,fees,status,pnl,settled,details)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        c["id"],
                        o["slug"],
                        o["outcome"],
                        t,
                        1.5,
                        0,
                        1,
                        1.1,
                        0,
                        "SETTLED",
                        0.1,
                        t + 241,
                        "{}",
                    ),
                )
    balances = e.summary()["accounts"]
    e.mode("SIMULATE", now)
    s.put("v2_initial_freeze_end", now+1)
    with pytest.raises(ValueError, match="frozen"):
        promote(s, c["id"], "Fresh test candidate", now)
    s.put("v2_initial_freeze_end", now - 1)
    s.put("v2_experiment",dict(start=now-8*86400,end=now-1,status="ENDED_REVIEW_REQUIRED"))
    config = promote(s, c["id"], "Fresh test candidate", now)
    lab.adaptive.enable(True, now)
    assert s.get("adaptive_enabled") is True
    lab.adaptive.enable(False, now)
    assert config["model"] == version and s.get("mode") == "OFF" and s.get("v2_experiment") is None
    assert e.summary()["accounts"] == balances
    prior = e.summary()["strategy_history"][-1]["id"]
    restored = rollback_strategy(s, prior, "Restore original baseline", now + 1)
    assert restored["exit"] == "HOLD" and restored["stake"] == 3
    assert e.summary()["strategy"]["action"] == "ROLLBACK"
    assert e.summary()["accounts"] == balances
    assert e.summary()["strategy"]["model"] != version
    with s.connect() as db:
        assert db.execute("SELECT count(*) FROM experiment_segments").fetchone()[0] == 1


@pytest.mark.parametrize("kind", ["random_forest", "xgboost", "lightgbm", "hmm"])
def test_optional_model_training_and_saved_inference(tmp_path, kind):
    import importlib.util
    from prophet.models import Models, CANDIDATES

    if importlib.util.find_spec(CANDIDATES[kind]) is None:
        pytest.skip("Optional research dependency not installed")
    s, e, now = setup(tmp_path)
    rows = [
        dict(
            slug=f"m-{i}",
            decision_time=1000 + i * 300,
            label_end=1300 + i * 300,
            x=[i % 5, i % 7, -5],
            y=i % 2,
        )
        for i in range(350)
    ]
    registry = Models(s)
    version, metrics = registry.train(kind, rows, now=now)
    assert 0 <= registry.predict(version, [1, 2, -5]) <= 1
    assert metrics["test"]["n"] >= 30
    assert sum(b["n"] for b in metrics["test"]["calibration"]) == metrics["test"]["n"]


def test_daily_pause_and_review_preserve_cash_and_streak(tmp_path):
    s, e, now = setup(tmp_path)
    with s.connect() as db:
        db.execute("UPDATE v2_accounts SET balance=82,realized=-18,streak=6 WHERE id='primary'")
        for i in range(6):
            db.execute(
                """INSERT INTO v2_orders(account,slug,side,time,model,probability,quantity,cost,fee,status,result,pnl,settled,details)
              VALUES ('primary',?,'UP',?,'fixture',.5,1,3,0,'SETTLED','DOWN',-3,?,'{}')""",
                (f"past-{i}", now - 300, now - 1),
            )
    e.mode("SIMULATE", now)
    e.tick(now)
    assert s.get("mode") == "SIMULATE"
    assert not [
        o for o in e.summary()["orders"] if o["account"] == "primary" and o["status"] == "OPEN"
    ]
    assert not e.summary()["skips"]  # The prior primary ledger was left intact.
    e.halt("TEST_CRITICAL")
    e.review()
    a = next(a for a in e.summary()["accounts"] if a["id"] == "primary")
    assert a["balance"] == 82 and a["streak"] == 6


def test_hourly_skip_is_shadow_only(tmp_path):
    s, _, now = seed(tmp_path, checkpoint=True)
    e = Engine(s)
    e.probability = lambda now, **kwargs: (0.8, dict(momentum_60=1, trend_300=1, volatility=0.001))
    e.mode("SIMULATE", now)
    e.tick(now)
    accounts = {o["account"] for o in e.summary()["orders"]}
    assert accounts == {"adaptive-1", "adaptive-2", "adaptive-3"}
    with s.connect() as db:
        assert db.execute("SELECT count(*) FROM v2_hourly WHERE up IS NOT NULL").fetchone()[0] == 1


def test_research_install_preserves_existing_bankroll(tmp_path):
    import json
    from scripts.install_research import install
    from prophet.store import Store

    destination = tmp_path / "local"
    destination.mkdir()
    store = Store(str(destination / "prophet.sqlite3"))
    Engine(store)
    with store.connect() as db:
        db.execute("UPDATE v2_accounts SET balance=91,realized=-9 WHERE id='primary'")
    source = tmp_path / "bundle"
    source.mkdir()
    (source / "verified-markets.json").write_text(json.dumps([]))
    install(source, destination)
    install(source, destination)
    with store.connect() as db:
        assert db.execute("SELECT balance FROM v2_accounts WHERE id='primary'").fetchone()[0] == 91
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    Store(str(destination / "prophet.sqlite3"))
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2


def test_future_source_data_and_auth_rate_limit(tmp_path):
    from prophet.auth import Auth, configure

    s, e, now = setup(tmp_path)
    r = s.get("reference")
    r["timestamp"] = now + 1
    s.put("reference", r)
    assert e.data_block(now) == "FUTURE_REFERENCE"
    auth = Auth(s)
    configure(auth.path, "test-rate-limit-password")
    for _ in range(5):
        with pytest.raises(ValueError, match="Invalid"):
            auth.login("admin", "wrong", "fixture-ip", now)
    with pytest.raises(ValueError, match="Too many"):
        auth.login("admin", "test-rate-limit-password", "fixture-ip", now)
    token, role = auth.login("admin", "test-rate-limit-password", "fixture-ip", now + 901)
    assert token and role == "admin"


def test_v2_smart_flip_is_shadow_only_and_cannot_repeat(tmp_path):
    import json
    from prophet.lab import Lab

    s, e, now = setup(tmp_path)
    lab = Lab(s, e)
    e.mode("SIMULATE", now)
    e.tick(now)
    lab.tick(now)
    # A legacy position remains supported for existing research and settlement.
    with s.connect() as db:
        db.execute("""INSERT INTO v2_orders(account,slug,side,time,model,probability,quantity,
                      cost,fee,status,details) VALUES ('primary',?,'UP',?,'fixture',.8,10,3,.1,'OPEN',?)""",
                   (s.get("market")["slug"],now,json.dumps(dict(prediction=dict(
                       window_end=now+210,theta=.095,tick=.01,quantum=.01)))))
    accounts = e.summary()["accounts"]
    e.probability = lambda now, **kwargs: (0.01, dict(momentum_60=-1, trend_300=-1, volatility=0.001))
    b = s.get("book")
    b.update(
        timestamp=now + 1,
        bids=[dict(price=0.8, quantity=1000)],
        asks=[dict(price=0.81, quantity=1000)],
    )
    s.put("book", b)
    lab.flip.tick(now + 1)
    lab.flip.tick(now + 1)
    with s.connect() as db:
        states = [
            json.loads(r[0])
            for r in db.execute("SELECT state FROM v2_smart_experiments WHERE kind='FLIP'")
        ]
        assert states and all(state["flips"] <= 1 for state in states)
        assert any(state["flips"] == 1 for state in states)
    assert e.summary()["accounts"] == accounts
    assert all(o["side"] == "UP" for o in e.summary()["orders"])


def test_adaptive_is_locked_until_initial_experiment_and_approval(tmp_path):
    from prophet.lab import Lab

    s, e, now = setup(tmp_path)
    lab = Lab(s, e)
    assert s.get("adaptive_enabled", False) is False
    with pytest.raises(ValueError, match="seven-day"):
        lab.adaptive.enable(True, now)
    s.put("v2_initial_freeze_end", now - 1)
    with pytest.raises(ValueError, match="No independently"):
        lab.adaptive.enable(True, now)
    lab.adaptive.enable(False, now)
    assert s.get("mode") == "OFF" and not e.summary()["orders"]


def test_collect_recent_candidates_preserves_existing_queue_and_rejects_wrong_market(tmp_path):
    from prophet.collector import Collector

    s, e, now = setup(tmp_path)
    m = s.get("market")
    m["slug"] = "cpc-btc-updown-5m-2027-01-15-0805z"
    s.put("pending_settlements", ["previous-pending"])
    c = Collector(s)
    c.seed_recent_history(m)
    pending = s.get("pending_settlements")
    assert pending[0] == "previous-pending" and len(pending) == 61
    c.seed_recent_history(m)
    assert s.get("pending_settlements") == pending
    assert not analytics.history(s)["markets"]  # Candidates are never verified outcomes.


def test_concurrent_ticks_cannot_double_debit(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from prophet.lab import Lab

    s, e, now = setup(tmp_path)
    lab = Lab(s,e)
    e.mode("SIMULATE", now)

    def tick():
        e.tick(now)
        lab.tick(now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(tick) for _ in range(2)]
        for future in futures:
            future.result()
    with s.connect() as db:
        assert db.execute("SELECT count(*) FROM lab_orders").fetchone()[0] == 3
        for row in db.execute("SELECT c.cash,o.cost FROM lab_orders o JOIN lab_configs c ON c.id=o.config"):
            assert abs(row["cash"] + row["cost"] - 1000) < 1e-9


def test_neural_backend_is_explicitly_optional(tmp_path):
    pytest.importorskip(
        "torch", reason="Optional LSTM backend absent; no neural training/accuracy claim"
    )
    from prophet.models import Models

    s, e, now = setup(tmp_path)
    rows = [
        dict(
            slug=f"n-{i}",
            decision_time=1000 + i * 300,
            label_end=1300 + i * 300,
            x=[i % 3, i % 5, -5],
            y=i % 2,
        )
        for i in range(350)
    ]
    with pytest.raises(ValueError, match="2000 training"):
        Models(s).train("lstm", rows, now)


def test_countdown_and_market_boundary_are_utc(tmp_path, monkeypatch):
    import importlib

    appmodule = importlib.import_module("prophet.app")
    s, e, now = setup(tmp_path)
    start = now - 90
    app = create_app(s.path, collect=False, auth_required=False)
    with TestClient(app) as client:
        monkeypatch.setattr(appmodule.time, "time", lambda: start)
        state = client.get("/api/state").json()
        assert state["seconds_remaining"] == 300 and state["market_active"]
        monkeypatch.setattr(appmodule.time, "time", lambda: start + 300)
        state = client.get("/api/state").json()
        assert state["seconds_remaining"] == 0 and not state["market_active"]


def test_excel_export_keeps_dashboard_available_and_recovers_from_errors(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from prophet import app as app_module

    async def run():
        started = asyncio.Event()
        finish = asyncio.Event()

        async def slow_export(database, destination):
            started.set()
            await finish.wait()
            return 1

        monkeypatch.setattr(app_module, "build_excel", slow_export)
        app = create_app(tmp_path / "export.sqlite3", collect=False, auth_required=False)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            pending = asyncio.create_task(client.get("/api/export.xlsx"))
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
                state = await asyncio.wait_for(client.get("/api/state"), timeout=2)
                assert state.status_code == 200
                duplicate = await asyncio.wait_for(client.get("/api/export.xlsx"), timeout=2)
                assert duplicate.status_code == 409
            finally:
                finish.set()
                result = await pending
            assert result.status_code == 500
            assert "Excel export failed" in result.json()["detail"]

            async def timed_out(database, destination):
                raise TimeoutError

            monkeypatch.setattr(app_module, "build_excel", timed_out)
            retry = await client.get("/api/export.xlsx")
            assert retry.status_code == 504

    asyncio.run(run())


def test_quick_excel_is_bounded_and_keeps_full_audit_available(tmp_path):
    import time

    app = create_app(tmp_path / "quick.sqlite3", collect=False, auth_required=False)
    now = time.time()
    with app.state.store.connect() as db:
        db.executemany("""INSERT INTO v2_orders (account,slug,side,time,quantity,cost,status,result,pnl,settled,details)
            VALUES ('adaptive-1',?,'UP',?,2,1,?,?,?,?,'{}')""", (
            (f"synthetic-{i}", now+i, "OPEN" if i==509 else "SETTLED",
             None if i==509 else "UP", None if i==509 else 1.0, None if i==509 else now+i+1)
            for i in range(510)
        ))
    with TestClient(app) as client:
        quick = client.get("/api/export/quick.xlsx")
        assert quick.is_success
        assert "BTC-Prophet-Quick-Report.xlsx" in quick.headers["content-disposition"]
        full = client.get("/api/export.xlsx")
        assert full.is_success
    workbook = load_workbook(io.BytesIO(quick.content), read_only=True)
    ledger = list(workbook["Recent Paper Trades"].iter_rows(max_col=8, values_only=True))[1:]
    assert len(ledger) == 500
    assert ledger[0][1] == "synthetic-509" and ledger[0][-1] is None
    assert ledger[-1][1] == "synthetic-10"
    [row] = list(workbook["Recent Performance"].values)[1:]
    assert row[1:] == (500,499,499,0,499)
    workbook.close()
    workbook = load_workbook(io.BytesIO(full.content), read_only=True)
    history = list(workbook["Trade History"].values)[1:]
    assert len(history) == 510
    workbook.close()


def test_primary_signal_names_actual_adaptive_entry_and_its_forecast_model(tmp_path, monkeypatch):
    store, _, now = setup(tmp_path)
    app = create_app(store.path, collect=False, auth_required=False)
    store.put("mode", "SIMULATE")
    monkeypatch.setattr("prophet.app.time.time", lambda: now)
    slug = store.get("market")["slug"]
    for account, model, up in (("adaptive-1","forecast-v2",.80),("adaptive-3","other-account-v3",.20)):
        with store.connect() as db:
            db.execute("""INSERT INTO adaptive_decisions (slug,account,time,model,up,decision,reason,observation,details)
                VALUES (?,?,?,?,?,'WAIT','test','test','{}')""", (slug,account,now-1,model,up))
    with TestClient(app) as client:
        signal = client.get("/api/state").json()["primary_signal"]
        assert (signal["kind"],signal["side"],signal["model"]) == ("FORECAST","UP","forecast-v2")
        with store.connect() as db:
            db.execute("""INSERT INTO v2_orders (account,slug,side,time,model,probability,quantity,cost,status,details)
                VALUES ('adaptive-1',?,'DOWN',?,'entry-v1',.72,2,1,'OPEN','{}')""", (slug,now-2))
        store.put("emergency", True)
        signal = client.get("/api/state").json()["primary_signal"]
        assert (signal["kind"],signal["side"],signal["model"]) == ("ENTRY","DOWN","entry-v1")
        with store.connect() as db:
            db.execute("DELETE FROM v2_orders WHERE account='adaptive-1'")
        assert client.get("/api/state").json()["primary_signal"]["kind"] == "WAITING"
        store.put("emergency", False)
        store.put("health_market", {"ok":False,"checked":now})
        assert client.get("/api/state").json()["primary_signal"]["kind"] == "WAITING"
