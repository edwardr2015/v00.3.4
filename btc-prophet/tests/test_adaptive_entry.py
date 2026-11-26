"""Synthetic paper runs. No external feeds or fabricated production rows."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import io
import json

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from prophet.app import create_app
from prophet.execution import paper_buy
from prophet.lab import Lab
from prophet.v2paper import Engine
from test_v2 import setup, verify
from test_timing_update import market_inputs


def read(store, query, args=()):
    with store.connect() as db:
        return [dict(row) for row in db.execute(query, args)]


def test_quiet_round_waits_then_final_skip_is_recorded_once(tmp_path):
    store, paper, now = setup(tmp_path)
    start = now - 90
    paper.mode("SIMULATE", start)
    # Use the production momentum model on real-shaped, explicitly synthetic flat candles.
    paper = Engine(store)
    store.save_candles([dict(time=t, open=100, high=100, low=100, close=100, ticks=1)
                        for t in range(start-315,start+300)])
    for elapsed in (0, 15, 67, 151, 299, 300, 305):
        market_inputs(store, start+elapsed, bid=.49, ask=.51)
        paper.tick(start+elapsed)
    assert not paper.summary()["orders"]
    decisions = read(store, "SELECT DISTINCT decision,reason FROM adaptive_decisions")
    assert {"decision":"WAIT","reason":"NO_POSITIVE_EDGE"} in decisions
    assert len(read(store, "SELECT * FROM v2_skips WHERE account LIKE 'adaptive-%'")) == 3
    assert len(read(store, "SELECT * FROM adaptive_decisions WHERE decision='SKIP'")) == 3


def test_spike_late_daily_reserve_disabled_duplicate_and_official_settlement(tmp_path):
    store, paper, now = setup(tmp_path)
    paper.mode("SIMULATE", now-90)
    slug = store.get("market")["slug"]
    with store.connect() as db:
        db.execute("UPDATE v2_accounts SET balance=0 WHERE id='adaptive-3'")
        for i in range(12):
            db.execute("""INSERT INTO v2_orders(account,slug,side,time,model,probability,quantity,
                cost,fee,status,result,pnl,settled,details)
                VALUES ('adaptive-2',?,'UP',?,'old',.8,1,2,0,'SETTLED','DOWN',-2,?,'{}')""",
                (f"prior-{i}", now-80, now-50))
    t = now+111                  # 201 seconds after open, past all six shadow checkpoints.
    paper.probability = lambda *args, **kwargs: (.91, dict(volatility=.04,momentum_60=.05))
    market_inputs(store, t, bid=.35, ask=.38)
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(paper.tick, [t]*3))
    rows = read(store, "SELECT o.*,a.budget,a.balance FROM v2_orders o JOIN v2_accounts a ON o.account=a.id WHERE slug=?", (slug,))
    assert len(rows) == 2
    assert all(o["time"] == t and .95*o["budget"] <= o["cost"] <= o["budget"] for o in rows)
    assert all(o["balance"] == pytest.approx(1000-o["cost"]) for o in rows)
    assert read(store, "SELECT reason FROM adaptive_decisions WHERE account='adaptive-3'")[-1]["reason"] == "INSUFFICIENT_CASH"
    again = Engine(store)
    # A much higher current bid can never sell an existing entry.
    market_inputs(store,t+3,bid=.88,ask=.90)
    again.tick(t+3)
    again.tick(now+210)
    assert all(o["status"] == "OPEN" for o in read(store,"SELECT status FROM v2_orders WHERE slug=?",(slug,)))
    verify(store, now)
    again.tick(now+300)
    again.tick(now+301)
    after = read(store, "SELECT o.*,a.balance FROM v2_orders o JOIN v2_accounts a ON o.account=a.id WHERE slug=?", (slug,))
    assert all(o["status"]=="SETTLED" and o["balance"]==pytest.approx(1000+o["pnl"]) for o in after)


def test_stale_feed_recovers_and_critical_lock_stays_locked(tmp_path):
    store, paper, now = setup(tmp_path)
    paper.mode("SIMULATE", now-90)
    paper.tick(now+31)
    assert not paper.summary()["orders"]
    assert paper.summary()["adaptive_decision"]["reason"] == "STALE_REFERENCE"
    assert paper.summary()["adaptive_decision"]["up"] is not None
    market_inputs(store, now+32,bid=.25,ask=.28)
    paper.tick(now+32)
    assert len(paper.summary()["orders"]) == 3
    paper.halt("CRITICAL_TEST")
    market_inputs(store,now+34,bid=.25,ask=.28)
    with pytest.raises(ValueError, match="Review critical"):
        paper.mode("SIMULATE",now+34)
    assert store.get("mode") == "OFF"


def test_no_entry_at_or_after_close(tmp_path):
    store, paper, now = setup(tmp_path)
    paper.mode("SIMULATE", now-90)
    market_inputs(store,now+210,bid=.25,ask=.28)
    paper.tick(now+210)
    paper.tick(now+211)
    assert not paper.summary()["orders"]


def test_later_entry_matches_observed_price_and_skips_unobserved_times(tmp_path):
    store, paper, now = setup(tmp_path)
    paper.mode("SIMULATE", now-90)
    t = now-46                   # Enter 44 seconds after open.
    market_inputs(store,t,bid=.28,ask=.30)
    paper.tick(t)
    market_inputs(store,t+15,bid=.18,ask=.20)
    paper.tick(t+15)             # Better same-direction price.
    market_inputs(store,t+30,bid=.39,ask=.40)
    paper.tick(t+30)             # Worse same-direction price.
    paper.tick(t+49)             # No usable observation for +45.
    market_inputs(store,t+75,bid=.23,ask=.25)
    paper.tick(t+75)
    verify(store, now)
    paper.tick(now+300)
    app = create_app(store.path,collect=False,auth_required=False)
    with TestClient(app) as client:
        book = load_workbook(io.BytesIO(client.get("/api/export.xlsx").content),read_only=True)
        rows = list(book["Later Entry Comparison"].iter_rows(max_col=18,values_only=True))
        values = {row[5]: row for row in rows[1:] if row[1]=="adaptive-1"}
        assert len(values) == 6
        assert values[15][4] == 44 and values[15][13]>0 and values[30][13]<0
        assert values[45][9]=="LATER_OBSERVATION_MISSED" and values[45][12] is None
        assert {"Adaptive Decisions","Adaptive Labeled Observations","Bug Log"} <= set(book.sheetnames)


def test_penny_quote_and_small_fill_are_rejected():
    assert paper_buy({"asks":[{"price":.01,"quantity":500}],"bids":[]},"UP",1)[1]=="ONE_CENT_QUOTE"
    assert paper_buy({"asks":[],"bids":[{"price":.99,"quantity":500}]},"DOWN",1)[1]=="ONE_CENT_QUOTE"
    fill, reason = paper_buy({"asks":[{"price":.06,"quantity":.8}],"bids":[]},"UP",1)
    assert fill is None and reason=="UNDERSIZED_FILL"


def test_upgrade_preserves_shadow_assets_and_cash(tmp_path):
    store,paper,now = setup(tmp_path)
    Lab(store,paper)
    with store.connect() as db:
        db.execute("DELETE FROM state WHERE key='continuous_entry_v1'")
        db.execute("INSERT INTO lab_configs VALUES ('timing/old/90/DYNAMIC/1','logistic','old',90,'DYNAMIC',1,'FORWARD SHADOW',84,?)",(now,))
    store.put("v2_experiment",dict(start=now,end=now+44,model_hash="old"))
    Engine(store)
    Lab(store,paper)
    old = read(store,"SELECT * FROM lab_configs WHERE id='timing/old/90/DYNAMIC/1'")[0]
    assert old["cash"] == 84 and old["status"] == "ARCHIVED PROTOCOL"
    assert {r["cash"] for r in read(store,"SELECT cash FROM lab_configs WHERE id LIKE 'timing/v2/%'")} == {1000}
    assert store.get("pre_continuous_experiment")["model_hash"] == "old"
    assert store.get("mode") == "OFF"


def test_rank_uses_actual_net_dollars_and_lowest_loss(tmp_path):
    store, paper, now = setup(tmp_path)
    lab = Lab(store,paper)
    paper.mode("SIMULATE", now-91)
    with store.connect() as db:
        for family, version, loss in [("momentum","momentum-v2",-2),("ensemble","fixture-ensemble",-.35)]:
            config=f"timing/v2/{version}/90/HOLD/2"
            db.execute("INSERT OR IGNORE INTO lab_configs VALUES (?,?,?,?,?,?,?,?,?)",
                       (config,family,version,90,"HOLD",2,"FORWARD SHADOW",1000,now))
            db.execute("""INSERT INTO lab_orders(config,slug,side,time,status,pnl,settled,details)
                          VALUES (?,'synthetic-test','DOWN',?,'SETTLED',?,?,'{}')""",(config,now,loss,now+300))
    rank = lab.model_performance()["models"]
    assert rank[0]["model"] == "ensemble" and rank[0]["best"]["pnl"] == -.35
    assert rank[0]["best"]["trades"] == 1 and rank[0]["best"]["losses"] == 1
    assert rank[2]["best"] is None


def test_password_free_dashboard_is_loopback_and_excludes_cross_origin(tmp_path):
    app = create_app(str(tmp_path/"auth.sqlite"),collect=False,auth_required=False)
    with TestClient(app) as client:
        assert client.get("/").status_code==200
        assert client.get("/api/session").json()["auth_required"] is False
        assert client.get("/api/model-performance").status_code==200
        assert client.post("/api/mode/SIMULATE").status_code==403
        assert client.post("/api/mode/SIMULATE",headers={"X-Prophet-Control":"local-dashboard",
                           "Origin":"https://example.com"}).status_code==403
    with TestClient(app,client=("192.168.1.2",50000)) as remote:
        assert remote.get("/api/paper").status_code==403


def test_unchanged_book_is_usable_when_recently_confirmed(tmp_path):
    store, paper, now = setup(tmp_path)
    paper.mode("SIMULATE",now-90)
    book=store.get("book")
    book["timestamp"]=now-27
    store.put("book",book)
    paper.tick(now)
    assert len(paper.summary()["orders"]) == 3
    assert store.get("book")["timestamp"] == now-27


def test_skip_diagnostics_and_one_sided_price_edge(tmp_path):
    from prophet.adaptive_entry import MIN_EDGE, opportunities

    store, paper, now = setup(tmp_path)
    book = dict(bids=[dict(price=.30,quantity=100)],asks=[dict(price=.34,quantity=100)])
    options = opportunities(book,1,.75,dict(theta=.095,tick=.01,quantum=.01))
    assert [option["side"] for option in options if option["ev_rate"]>=MIN_EDGE]==["UP"]
    app = create_app(store.path,collect=False,auth_required=False)
    app.state.paper.mode("SIMULATE",now-90)
    market_inputs(store,now,bid=.15,ask=.85)
    app.state.paper.tick(now)
    app.state.paper.tick(now+210)
    with TestClient(app) as client:
        data = client.get("/api/research-diagnostics").json()
    assert data["adaptive_rounds"]==1
    assert {r["reason"] for r in data["adaptive"]}=={"NO_POSITIVE_EDGE"}
    assert sum(r["rounds"] for r in data["adaptive"])==3
