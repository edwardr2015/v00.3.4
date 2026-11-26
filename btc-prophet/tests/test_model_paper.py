"""Synthetic market coverage for independent autonomous paper research."""
import io
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from prophet.app import create_app
from prophet.model_paper import FAMILIES, REFERENCE_AGREEMENT, TIMED_FAMILIES, decide, probabilities_from_market
from test_timing_update import market_inputs
from test_v2 import setup, verify


def test_each_family_can_enter_at_its_own_time_and_choose_only_one_action(tmp_path,monkeypatch):
    store,_,now=setup(tmp_path)
    app=create_app(store.path,collect=False,auth_required=False)
    runner=app.state.agreement.model_paper
    app.state.paper.mode("SIMULATE",now)
    families=list(FAMILIES)
    monkeypatch.setattr(runner,"versions",lambda db,t:{f:f+"-fixture" for f in families})
    current={}
    monkeypatch.setattr(runner,"forecast",lambda *args:(dict(current),{}))
    for i,family in enumerate(families):
        at=now+i*5
        direction="UP" if i%2==0 else "DOWN"
        if family == REFERENCE_AGREEMENT:
            # The last family points DOWN, and the same market side must be favored.
            market_inputs(store,at,bid=.44,ask=.45)
        else:
            market_inputs(store,at,bid=.48,ask=.51)
        current.clear()
        current[family]=.91 if direction=="UP" else .09
        runner.tick(at)
        runner.tick(at)
        with store.connect() as db:
            orders=db.execute("SELECT family,side,entered FROM model_paper_positions WHERE family=?",(family,)).fetchall()
            assert [tuple(r) for r in orders]==[(family,direction,at)]
            assert db.execute("""SELECT count(*) FROM model_paper_decisions
              WHERE family=? AND time=? AND action IN ('UP','DOWN')""",(family,at)).fetchone()[0]==1
    assert len(families)==12
    verify(store,now,"UP")
    runner.tick(now+215)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM model_paper_positions WHERE status='OPEN'").fetchone()[0]==12
    runner.tick(now+301)
    runner.tick(now+302)
    with store.connect() as db:
        for account,cash,pnl in db.execute("""SELECT a.family,a.cash,p.pnl FROM model_paper_accounts a
                             JOIN model_paper_positions p USING(family)"""):
            assert cash==pytest.approx(1000+pnl),account
    with TestClient(app) as client:
        rows=client.get("/api/model-paper").json()
        assert len(rows)==12 and all(r["closed"]==1 for r in rows)
        response=client.get("/api/export.xlsx")
    with io.BytesIO(response.content) as contents:
        book=load_workbook(contents,read_only=True)
        assert len(list(book["Autonomous Model Trades"].values))==13
        assert len(list(book["Autonomous Model Accounts"].values))==13
        assert len(list(book["Autonomous Model Decisions"].values))>12
        book.close()


def test_stale_quote_pause_and_concurrent_shadow_exit_cannot_debit_twice(tmp_path,monkeypatch):
    store,_,now=setup(tmp_path)
    app=create_app(store.path,collect=False,auth_required=False)
    runner=app.state.agreement.model_paper
    monkeypatch.setattr(runner,"versions",lambda *args:{"momentum":"momentum-fixture"})
    p=[.91]
    monkeypatch.setattr(runner,"forecast",lambda *args:({"momentum":p[0]},{}))
    market_inputs(store,now,bid=.29,ask=.30)
    app.state.paper.mode("LEARN",now)
    runner.tick(now)
    app.state.paper.mode("SIMULATE",now)
    store.put("health_market",dict(ok=False,checked=now))
    runner.tick(now)
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM model_paper_positions").fetchone()
    market_inputs(store,now+5,bid=.29,ask=.30)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(runner.tick,[now+5]*12))
    p[0]=.05
    market_inputs(store,now+10,bid=.60,ask=.61)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(runner.tick,[now+10]*12))
    market_inputs(store,now+15,bid=.29,ask=.30)
    p[0]=.99
    runner.tick(now+15)
    with store.connect() as db:
        position=db.execute("SELECT * FROM model_paper_positions").fetchone()
        assert position["status"]=="OPEN"
        assert db.execute("SELECT count(*) FROM model_paper_positions").fetchone()[0]==1
        assert db.execute("SELECT count(*) FROM model_paper_decisions WHERE action='CASH_OUT'").fetchone()[0]==0
        assert db.execute("""SELECT count(*) FROM exit_research_results
            WHERE source='autonomous' AND policy='PROBABILITY_EXIT' AND action='EXIT'""").fetchone()[0]==1
        assert db.execute("SELECT cash FROM model_paper_accounts WHERE family='momentum'").fetchone()[0]==pytest.approx(1000-position["cost"])
    verify(store,now,"DOWN")
    runner.tick(now+300)
    with store.connect() as db:
        assert db.execute("SELECT outcome,pnl FROM model_paper_positions").fetchone()[0]=="DOWN"
        assert db.execute("SELECT count(*) FROM model_paper_decisions WHERE observation='final'").fetchone()[0]==10


def test_challenger_probabilities_and_executable_depth_guard():
    p=probabilities_from_market(.6,100,100,.001,120,.5)
    assert p["market_odds_adjustment"]==pytest.approx(.57)
    assert p["volatility_reference_gap"]==.5
    assert probabilities_from_market(.6,101,100,.001,120,.5)["volatility_reference_gap"]>.5
    position=dict(side="UP",quantity=10,cost=3,terms='{"theta":0.095,"tick":0.01,"quantum":0.01}')
    action,reason,_=decide(position,900,dict(bids=[dict(price=.8,quantity=1)],asks=[]),{},.05,None)
    assert action=="HOLD" and reason=="FULL_EXIT_DEPTH_UNAVAILABLE"


def test_all_models_hold_and_compare_matched_exits_and_timing(tmp_path, monkeypatch):
    store, _, now = setup(tmp_path)
    app = create_app(store.path, collect=False, auth_required=False)
    runner = app.state.agreement.model_paper
    app.state.paper.mode("SIMULATE", now)
    families = ("volatility_reference_gap", "market_odds_adjustment")
    monkeypatch.setattr(runner, "versions", lambda *args: {f: f+"-fixture" for f in families})
    p = [.91]
    monkeypatch.setattr(runner, "forecast", lambda *args: ({f: p[0] for f in families}, {}))
    market_inputs(store, now, bid=.29, ask=.30)
    runner.tick(now)
    p[0] = .05
    market_inputs(store, now+10, bid=.60, ask=.61)
    runner.tick(now+10)
    with store.connect() as db:
        assert dict(db.execute("SELECT family,status FROM model_paper_positions")) == {
            families[0]: "OPEN", families[1]: "OPEN"}
        assert tuple(db.execute("""SELECT action,reason,json_extract(evidence,'$.shadow_exit_action')
            FROM model_paper_decisions WHERE family=? ORDER BY id DESC LIMIT 1""",
            (families[0],)).fetchone()) == ("HOLD", "HOLD_TO_SETTLEMENT_POLICY", "CASH_OUT")
    verify(store, now, "UP")
    runner.tick(now+301)
    app.state.agreement.exits.tick(now+301)
    with store.connect() as db:
        matched = db.execute("""SELECT policy,action,pnl FROM exit_research_results
            WHERE source='autonomous' AND policy IN ('HOLD','PROBABILITY_EXIT','FLIP')""").fetchall()
        assert len(matched) == 6
        assert {r["policy"]:r["action"] for r in matched} == {
            "HOLD": None, "PROBABILITY_EXIT": "EXIT", "FLIP": "FLIP"}
        assert db.execute("""SELECT count(*) FROM model_paper_timing
            WHERE checkpoint=90 AND quantity IS NOT NULL""").fetchone()[0] == 2
        assert db.execute("""SELECT count(*) FROM model_paper_timing
            WHERE checkpoint=75 AND reason='MISSED_CHECKPOINT'""").fetchone()[0] == len(TIMED_FAMILIES)
        assert db.execute("SELECT count(*) FROM model_paper_positions WHERE status='SETTLED'").fetchone()[0] == 2
        row = db.execute("SELECT status,pnl,quantity,cost FROM model_paper_positions WHERE family=?",
                         (families[0],)).fetchone()
        assert row["status"] == "SETTLED"
        assert row["pnl"] == pytest.approx(row["quantity"]-row["cost"])
    with TestClient(app) as client:
        response = client.get("/api/export.xlsx")
        assert response.is_success
    with io.BytesIO(response.content) as contents:
        book = load_workbook(contents, read_only=True)
        assert any(row[0] == "autonomous" and row[4] == "FLIP" and row[6] == 1
                   for row in book["Matched Exit Results"].values if row)
        assert any(row[0] == families[0] and row[2] == 90 and row[6] == 1 and row[14] == 0
                   for row in book["Autonomous Timing Results"].values if row)
        book.close()


def test_shadow_stakes_use_observed_depth_and_reject_weak_edge():
    from prophet.model_paper import shadow_stake_fills

    terms = dict(theta=.095,tick=.01,quantum=.01)
    votes = dict(up_votes=8,down_votes=0,missing=[],mean_up=.82)
    book = dict(bids=[dict(price=.54,quantity=30)],asks=[dict(price=.55,quantity=30)])
    small,large = shadow_stake_fills(votes,book,terms)
    assert small["reason"] == "SHADOW_FILL"
    assert large["reason"] == "UNDERSIZED_FILL"
    votes["mean_up"] = .53
    assert shadow_stake_fills(votes,book,terms)[0]["reason"] == "NO_POSITIVE_EDGE"
    votes["up_votes"] = 7
    assert shadow_stake_fills(votes,book,terms)[0]["reason"] == "NO_UNANIMOUS_VOTE"


def test_sizing_shadow_exports_both_rules_without_larger_paper_orders(tmp_path, monkeypatch):
    store, engine, now = setup(tmp_path)
    app = create_app(store.path,collect=False,auth_required=False)
    runner = app.state.agreement.model_paper
    monkeypatch.setattr(runner,"versions",lambda *args:{"model_vote":"model-vote-v1"})
    monkeypatch.setattr(runner,"forecast",lambda *args:(
        {"model_vote":1.0},{"vote":dict(up_votes=8,down_votes=0,missing=[],mean_up=.84)}))
    at = now-45
    engine.mode("SIMULATE",at)
    market_inputs(store,at,bid=.54,ask=.55)
    engine.tick(at)
    runner.tick(at)
    with store.connect() as db:
        assert db.execute("SELECT max(cost) FROM v2_orders").fetchone()[0] <= 3
        assert db.execute("SELECT max(cost) FROM model_paper_positions").fetchone()[0] <= 3
    verify(store,now,"UP")
    engine.tick(now+301)
    runner.tick(now+301)
    with TestClient(app) as client:
        result = client.get("/api/export.xlsx")
        assert result.is_success
    workbook=load_workbook(io.BytesIO(result.content),read_only=True)
    rows = list(workbook["Paired Shadow Stakes"].values)[1:]
    assert {row[0] for row in rows} == {"Unanimous Model Vote","Adaptive model and market agree"}
    assert all(row[3]>row[2]>0 for row in rows)
    workbook.close()


def test_reference_gap_large_shadow_uses_agreement_edge_and_depth():
    from prophet.model_paper import reference_gap_stake

    terms = dict(theta=.095, tick=.01, quantum=.01)
    book = dict(bids=[dict(price=.59, quantity=200)], asks=[dict(price=.60, quantity=200)])
    votes = dict(up_votes=7, down_votes=1, missing=[])
    selected = dict(side="UP", probability=.85)
    trial = reference_gap_stake(book, terms, selected, votes)
    assert trial["reason"] == "SHADOW_FILL"
    assert trial["modeled_fill"]["total"] == pytest.approx(20, abs=.02)
    assert trial["votes_for_side"] == 7
    assert reference_gap_stake(book, terms, dict(selected, probability=.66), votes)["reason"] == (
        "BELOW_10_PERCENT_EXPECTED_RETURN"
    )
    assert reference_gap_stake(book, terms, dict(selected, side="DOWN"), votes)["reason"] == (
        "MODEL_MARKET_DISAGREEMENT"
    )
    book["asks"][0]["quantity"] = 30
    assert reference_gap_stake(book, terms, selected, votes)["reason"] == "UNDERSIZED_FILL"


def test_reference_gap_large_shadow_settles_in_excel_only(tmp_path, monkeypatch):
    from prophet.model_paper import REFERENCE_GAP

    store, engine, now = setup(tmp_path)
    app = create_app(store.path, collect=False, auth_required=False)
    runner = app.state.agreement.model_paper
    monkeypatch.setattr(runner, "versions", lambda *args: {REFERENCE_GAP: "reference-gap-fixture"})
    monkeypatch.setattr(runner, "forecast", lambda *args: (
        {REFERENCE_GAP: .92}, {"vote": dict(up_votes=7, down_votes=1, missing=[])}
    ))
    engine.mode("SIMULATE", now)
    market_inputs(store, now, bid=.59, ask=.60)
    runner.tick(now)
    verify(store, now, "UP")
    runner.tick(now + 301)
    with store.connect() as db:
        cost, actual_pnl = db.execute("SELECT cost,pnl FROM model_paper_positions").fetchone()
        assert cost <= 3
        cash = db.execute("SELECT cash FROM model_paper_accounts WHERE family=?", (REFERENCE_GAP,)).fetchone()[0]
        assert cash == pytest.approx(1000 + actual_pnl)
    with TestClient(app) as client:
        response = client.get("/api/export.xlsx")
        assert response.is_success
    with io.BytesIO(response.content) as stream:
        book = load_workbook(stream, read_only=True)
        [row] = list(book["Reference Gap 20 Audit"].values)[1:]
        assert row[11:13] == ("SHADOW_FILL", "UP")
        assert row[14] > row[13] == pytest.approx(actual_pnl)
        results = {row[0]: row for row in list(book["Reference Gap 20 Results"].values)[1:]}
        assert results["Agreement and model score ≥90%"][4:7] == (1, 0, 1)
        assert results["Agreement and at least 7 of 8 votes"][4:7] == (1, 0, 1)
        book.close()


def test_reference_agreement_waits_then_enters_once_and_holds_to_verified_settlement(tmp_path, monkeypatch):
    from prophet.model_paper import REFERENCE_GAP, agreement_rounds

    store, engine, now = setup(tmp_path)
    app = create_app(store.path, collect=False, auth_required=False)
    runner = app.state.agreement.model_paper
    monkeypatch.setattr(runner, "forecast", lambda *args: (
        {REFERENCE_GAP: .78, REFERENCE_AGREEMENT: .78}, {}
    ))
    engine.mode("SIMULATE", now-20)
    market_inputs(store, now-20, bid=.44, ask=.45)
    runner.tick(now-20)
    market_inputs(store, now-10, bid=.81, ask=.82)
    runner.tick(now-10)
    market_inputs(store, now, bid=.59, ask=.60)
    store.put("health_market",dict(ok=False,checked=now))
    runner.tick(now)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM model_paper_positions WHERE family=?",
                          (REFERENCE_AGREEMENT,)).fetchone()[0] == 0
        reasons = [r[0] for r in db.execute("SELECT reason FROM model_paper_decisions WHERE family=? ORDER BY id",
                                           (REFERENCE_AGREEMENT,))]
        assert reasons[:2] == ["MODEL_MARKET_DISAGREEMENT", "NO_EXECUTABLE_EDGE"]
    at = now+7
    market_inputs(store, at, bid=.59, ask=.60)
    runner.tick(at)
    runner.tick(at)
    market_inputs(store, now+8, bid=.83, ask=.84)
    runner.tick(now+8)
    with store.connect() as db:
        [fill] = db.execute("SELECT * FROM model_paper_positions WHERE family=?", (REFERENCE_AGREEMENT,)).fetchall()
        assert (fill["entered"],fill["side"],fill["status"]) == (at,"UP","OPEN")
        assert 2.85 <= fill["cost"] <= 3
    verify(store, now, "UP")
    runner.tick(now+301)
    with store.connect() as db:
        results = {r["label"]:r for r in agreement_rounds(db)}
        assert results["Reference Gap + Market Agreement"]["pnl"] == pytest.approx(fill["quantity"]-fill["cost"])
        assert results["Reference Gap baseline"]["entered"] == now-20
        assert results["LightGBM +45s"]["pnl"] is None
    with TestClient(app) as client:
        assert client.get("/reports/excel").status_code == 200
        response = client.get("/api/export.xlsx")
        assert response.is_success
    with io.BytesIO(response.content) as stream:
        book = load_workbook(stream, read_only=True)
        assert len(list(book["Agreement Forward Rounds"].values)) == 4
        summary = {row[0]:row for row in list(book["Agreement Forward Results"].values)[1:]}
        assert summary["Reference Gap + Market Agreement"][1:6] == (1,1,1,0,0)
        assert summary["LightGBM +45s"][1:6] == (1,0,0,0,1)
        book.close()


def test_pooled_100_waits_for_all_models_depth_and_freshness_and_settles_without_debit(tmp_path, monkeypatch):
    from prophet.model_paper import POOLED_MODELS

    store, engine, now = setup(tmp_path)
    app = create_app(store.path, collect=False, auth_required=False)
    runner = app.state.agreement.model_paper
    assert len(POOLED_MODELS) == 10
    values = {family: .86 for family in POOLED_MODELS}
    missing = POOLED_MODELS[-1]
    values.pop(missing)
    monkeypatch.setattr(runner, "versions", lambda *args: {f:f+"-fixture" for f in POOLED_MODELS})
    monkeypatch.setattr(runner, "forecast", lambda *args: (dict(values),{}))
    engine.mode("SIMULATE", now)

    def tick(at, size, *, stale=False):
        market_inputs(store, at, bid=.58, ask=.59)
        book = store.get("book")
        for level in book["bids"] + book["asks"]:
            level["quantity"] = size
        store.put("book", book)
        if stale:
            store.put("health_market", dict(ok=False, checked=at))
        runner.tick(at)
        with store.connect() as db:
            return db.execute("SELECT * FROM pooled_consensus_rounds").fetchone()

    assert tick(now, 2000)["reason"] == "POOL_MODELS_NOT_READY"
    values[missing] = .25
    assert tick(now+1, 2000)["reason"] == "POOL_NOT_UNANIMOUS"
    values[missing] = .86
    assert tick(now+2, 200)["reason"] == "UNDERSIZED_FILL"
    assert tick(now+3, 2000, stale=True)["status"] == "WAITING"
    fill = tick(now+4, 2000)
    assert fill["status"] == "FILLED"
    assert 95 <= fill["cost"] <= 100
    assert 2.85 <= fill["baseline_cost"] <= 3
    assert fill["probability"] == pytest.approx(.86)
    assert tick(now+5, 1000)["observed"] == now+4

    with store.connect() as db:
        assert db.execute("SELECT max(cost) FROM model_paper_positions").fetchone()[0] <= 3
        assert db.execute("SELECT count(*) FROM pooled_consensus_rounds").fetchone()[0] == 1
    verify(store, now, "UP")
    runner.tick(now+301)
    with TestClient(app) as client:
        summary = client.get("/api/pooled-consensus").json()
        assert (summary["wins"],summary["losses"],summary["paired"]) == (1,0,1)
        assert summary["net_pnl"] == pytest.approx(fill["quantity"] - fill["cost"])
        response = client.get("/api/export.xlsx")
        assert response.is_success
    with io.BytesIO(response.content) as stream:
        workbook = load_workbook(stream, read_only=True)
        audit = list(workbook["Pooled 100 Round Audit"].values)[1:]
        assert len(audit) == 1 and audit[0][12] == "UP"
        assert audit[0][13] == pytest.approx(summary["net_pnl"])
        assert "Pooled 100 Results" in workbook.sheetnames
        workbook.close()


def test_pooled_100_rejects_weak_edge_and_records_skip_past_close(tmp_path):
    from prophet.pooled_consensus import finalize, observe, summary

    app = create_app(tmp_path/"pool.sqlite3", collect=False, auth_required=False)
    with app.state.store.connect() as db:
        models = {"a":"a-1", "b":"b-1"}
        forecasts = {"a": .55, "b": .56}
        book = dict(bids=[dict(price=.58,quantity=2000)], asks=[dict(price=.59,quantity=2000)])
        terms = dict(theta=.095,tick=.01,quantum=.01)
        observe(db,"round",0,300,200,models,forecasts,models,book,terms,.03,None)
        assert summary(db)["latest"]["reason"] == "NO_POSITIVE_EDGE"
        finalize(db,300)
        observe(db,"round",0,300,300,models,{"a": .95,"b": .95},models,book,terms,.03,None)
        result = summary(db)
        assert result["skips"] == 1
        assert result["open"] == 0
        assert result["net_pnl"] is None


def test_pooled_alternatives_keep_old_history_and_wait_for_their_own_agreement(tmp_path):
    from prophet.pooled_consensus import REFERENCE, comparisons, observe, summary

    app = create_app(tmp_path / "comparison.sqlite3", collect=False, auth_required=False)
    terms = dict(theta=.095, tick=.01, quantum=.01)
    versions = {REFERENCE: "gap-fixture", "other": "other-fixture"}
    up_market = dict(bids=[dict(price=.55, quantity=2000)], asks=[dict(price=.56, quantity=2000)])
    down_market = dict(bids=[dict(price=.44, quantity=2000)], asks=[dict(price=.45, quantity=2000)])
    unanimous = {REFERENCE: .85, "other": .86}
    with app.state.store.connect() as db:
        observe(db, "archived", 0, 300, 60, versions, unanimous, versions, up_market, terms, .03, None)
        observe(db, "new", 300, 600, 350, versions, unanimous, versions, down_market, terms, .03, None, compare=True)
        control, all_and_market, reference_and_market = comparisons(db)
        assert summary(db)["observed"] == 2
        assert control["observed"] == 1 and control["open"] == 1
        assert all_and_market["latest"]["reason"] == "MODEL_MARKET_DISAGREEMENT"
        assert reference_and_market["open"] == 0
        observe(db, "new", 300, 600, 355, versions, {REFERENCE: .85}, versions, up_market, terms, .03, None, compare=True)
        control, all_and_market, reference_and_market = comparisons(db)
        assert all_and_market["latest"]["reason"] == "POOL_MODELS_NOT_READY"
        assert reference_and_market["open"] == 1
        observe(db, "new", 300, 600, 360, versions, unanimous, versions, up_market, terms, .03, None, compare=True)
        assert [row["open"] for row in comparisons(db)] == [1, 1, 1]
        assert [tuple(row) for row in db.execute(
            "SELECT scenario,observed FROM pooled_agreement_rounds ORDER BY scenario"
        )] == [("reference_market",355),("unanimous_market",360)]
    with TestClient(app) as client:
        response = client.get("/api/export.xlsx")
        assert response.is_success
    with io.BytesIO(response.content) as stream:
        workbook = load_workbook(stream, read_only=True)
        rows = list(workbook["100 Agreement Comparisons"].values)[1:]
        assert [row[1] for row in rows] == [1,1,1]
        assert len(list(workbook["100 Agreement Round Audit"].values)) == 4
        workbook.close()


def test_selective_90_shadow_uses_only_completed_bars_and_settles(tmp_path):
    from prophet.selective_forecast import summary

    store,_,now = setup(tmp_path)
    app = create_app(store.path,collect=False,auth_required=False)
    store.put("mode","SIMULATE")
    market_inputs(store,now,bid=.53,ask=.54)
    store.save_candles([
        dict(time=t,open=p,high=p,low=p,close=p,ticks=1)
        for t in range(now-315,now+1)
        for p in [100+(t-now+315)*.002 if t<now else 50]
    ])
    runner = app.state.agreement.model_paper
    runner.tick(now-1)
    with store.connect() as db:
        assert summary(db)["latest"]["status"] == "WAITING"
    runner.tick(now)
    runner.tick(now+1)
    with store.connect() as db:
        latest=summary(db)["latest"]
        assert latest["side"]=="UP" and latest["gap_bps"]>12
        assert latest["status"]=="CALL" and latest["observed"]==now
        assert latest["fill_reason"]=="SHADOW_FILL"
        assert not db.execute("SELECT 1 FROM v2_orders WHERE model='selective-90'").fetchone()
    verify(store,now,"UP")
    runner.tick(now+301)
    with TestClient(app) as client:
        info=client.get("/api/selective-forecast").json()
        assert info["observed"]==1 and info["right"]==1 and info["settled_fills"]==1
        assert info["net_pnl"]==pytest.approx(latest["quantity"]-latest["cost"])
        r=client.get("/api/export/quick.xlsx")
    assert r.is_success
    with io.BytesIO(r.content) as stream:
        workbook=load_workbook(stream,read_only=True)
        assert "Selective 90 Summary" in workbook.sheetnames
        workbook.close()


def test_selective_90_learn_leaves_gap_visible_without_modeling_a_fill(tmp_path):
    from prophet.selective_forecast import summary

    store,_,now=setup(tmp_path)
    app=create_app(store.path,collect=False,auth_required=False)
    store.put("mode","LEARN")
    # A missing second in the last closed candle prevents a call.
    with store.connect() as db:
        db.execute("DELETE FROM candles WHERE source='BRTI' AND time=?",(now-1,))
    runner=app.state.agreement.model_paper
    runner.tick(now)
    runner.tick(now+6)
    with store.connect() as db:
        rule=summary(db)
        assert rule["skips"]==1 and rule["latest"]["reason"]=="MISSED_CHECKPOINT"
        assert rule["latest"]["cost"] is None
