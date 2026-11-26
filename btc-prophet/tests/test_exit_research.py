"""Synthetic post-entry books and settlement for matched paper exit research."""
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from prophet.app import create_app
from prophet.exit_research import POLICIES, matched_rows
from test_timing_update import market_inputs
from test_v2 import setup, verify


def test_live_paths_split_into_matched_shadows_and_preserve_original_ledgers(tmp_path, monkeypatch):
    store, _, now = setup(tmp_path)
    app = create_app(store.path, collect=False, auth_required=False)
    paper, agreement = app.state.paper, app.state.agreement
    paper.mode("SIMULATE", now)
    monkeypatch.setattr(agreement.lab, "predict", lambda *args: .8)
    monkeypatch.setattr(paper, "probability", lambda *a, **kw: (
        .8, dict(momentum_60=1,trend_300=1,volatility=.002)))
    market_inputs(store, now, bid=.24, ask=.25)
    paper.tick(now)
    agreement.tick(now)
    with store.connect() as db:
        count = db.execute("SELECT count(*) FROM exit_research_entries WHERE source!='autonomous'").fetchone()[0]
        assert count == 6
        assert db.execute("SELECT count(*) FROM exit_research_results WHERE source!='autonomous'").fetchone()[0] == count*len(POLICIES)
        balances = dict(db.execute("SELECT id,balance FROM v2_accounts"))
        lab_cash = dict(db.execute("SELECT id,cash FROM lab_configs"))

    monkeypatch.setattr(agreement.exits,"estimate",lambda *args: .02)
    monkeypatch.setattr(paper,"probability",lambda *a,**kw:(.02,dict(momentum_60=-1,trend_300=-1,volatility=.002)))
    market_inputs(store, now+10, bid=.60, ask=.61)
    store.put("health_market", dict(ok=False,checked=now+10))
    agreement.tick(now+10)
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM exit_research_results WHERE source!='autonomous' AND eligible_quotes>0").fetchone()

    market_inputs(store, now+11, bid=.60, ask=.61)
    book=store.get("book")
    book["bids"][0]["quantity"]=.01
    store.put("book",book)
    agreement.tick(now+11)
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM exit_research_results WHERE source!='autonomous' AND acted_at IS NOT NULL").fetchone()

    market_inputs(store, now+12, bid=.60, ask=.61)
    agreement.tick(now+12)
    agreement.tick(now+12)
    with store.connect() as db:
        assert dict(db.execute("SELECT id,balance FROM v2_accounts")) == balances
        assert dict(db.execute("SELECT id,cash FROM lab_configs")) == lab_cash
        assert not db.execute("SELECT 1 FROM paper_flips").fetchone()
        assert db.execute("SELECT count(*) FROM exit_research_results WHERE source!='autonomous' AND action='FLIP'").fetchone()[0] == count
        assert db.execute("SELECT count(*) FROM exit_research_results WHERE source!='autonomous' AND action='EXIT'").fetchone()[0] == count*6
        assert db.execute("SELECT max(eligible_quotes) FROM exit_research_results WHERE source!='autonomous'").fetchone()[0] == 2

    verify(store, now, "DOWN")
    agreement.tick(now+220)
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM exit_research_results WHERE pnl IS NOT NULL").fetchone()
    paper.tick(now+300)
    agreement.tick(now+300)
    with store.connect() as db:
        rows=[r for r in matched_rows(db) if r["source"]!="autonomous"]
        assert all(r["settled"] == now+300 and r["outcome"]=="DOWN" for r in rows)
        for r in rows:
            assert r["hold_pnl"] == pytest.approx(-r["cost"])
            assert r["improvement"] == pytest.approx(r["pnl"]-r["hold_pnl"])
            if r["policy"]!="HOLD":
                assert r["improvement"] > 0
        assert db.execute("SELECT count(*) FROM v2_orders WHERE pnl<0").fetchone()[0]==3

    with TestClient(app) as client:
        response=client.get("/api/export.xlsx")
        assert response.status_code==200
    book=load_workbook(BytesIO(response.content),read_only=True)
    assert len([r for r in book["Matched Exit Audit"].values if r[0]!="autonomous"])==1+count*len(POLICIES)
    assert len([r for r in book["Matched Exit Results"].values if r[0]!="autonomous"])==1+count*len(POLICIES)
    book.close()


def test_take_profit_requires_executable_size_and_a_possible_payout():
    from prophet.exit_research import ExitResearch

    inputs=dict(side="UP",quantity=1,cost=.9,policy="TAKE_PROFIT_1.2",stake=1)
    assert ExitResearch.evaluate(inputs,.8,{}, {},dict(quantity=1,total=1))[0] is None
    inputs["cost"] = .7
    assert ExitResearch.evaluate(inputs,.8,{}, {},dict(quantity=.5,total=.9))[0] is None
    assert ExitResearch.evaluate(inputs,.8,{}, {},dict(quantity=1,total=.84))[0] == ("UP",0,.84,0)


@pytest.mark.parametrize(("up_count","complete","expected"), [(5,True,.625),(4,True,.5),(5,False,None)])
def test_model_vote_records_same_checkpoint_votes_without_guessing(tmp_path, monkeypatch, up_count, complete, expected):
    import json
    from prophet import pattern_odds
    from prophet.models import BASE_PARTICIPANTS, VOTE_VERSION
    store, _, now=setup(tmp_path)
    app=create_app(store.path,collect=False,auth_required=False)
    engine,lab=app.state.paper,app.state.agreement.lab
    lab.refreshed=now
    versions={family:pattern_odds.VERSION if family=="pattern_odds" else f"test-{family}"
              for family in BASE_PARTICIPANTS}
    by_version={version:(.7 if i<up_count else .3) for i,version in enumerate(versions.values())}
    with store.connect() as db:
        db.execute("UPDATE lab_configs SET status='ARCHIVED VERSION' WHERE family!='model_vote'")
        for family,version in versions.items():
            db.execute("INSERT OR IGNORE INTO model_registry VALUES (?,?,?,'fixture','file','hash','{}')",
                       (version,family,now-1))
            db.execute("INSERT OR REPLACE INTO lab_configs VALUES (?,?,?,?,?,?,?,?,?)",
                       (f"timing/v2/{version}/90/HOLD/1",family,version,90,"HOLD",1,"FORWARD SHADOW",1000,now-1))
    def estimate(version,*args):
        if version==versions["momentum"] and not complete:
            raise ValueError("Fixture model unavailable")
        return by_version[version]
    monkeypatch.setattr(lab,"predict",estimate)
    monkeypatch.setattr(pattern_odds,"forecast",lambda *args: (estimate(pattern_odds.VERSION), {}))
    market_inputs(store, now, bid=.24, ask=.25)
    engine.mode("SIMULATE", now)
    engine.tick(now)
    app.state.agreement.tick(now)
    with store.connect() as db:
        vote=json.loads(db.execute("SELECT evidence FROM model_vote_forecasts WHERE checkpoint=90").fetchone()[0])
        assert vote["up_share"]==expected and vote["up_votes"]==up_count-(not complete)
        assert vote["down_votes"]==8-up_count
        assert vote["missing"]==([] if complete else ["momentum"])
        result=db.execute("SELECT up FROM lab_predictions WHERE version=?", (VOTE_VERSION,)).fetchone()
        assert (result[0] if result else None)==expected
        orders=db.execute("SELECT side FROM lab_orders WHERE config LIKE '%model-vote%'").fetchall()
        assert [r[0] for r in orders]==(["UP"]*3 if expected==.625 else [])
        skips=db.execute(
            "SELECT DISTINCT s.reason FROM lab_skips s JOIN lab_configs c ON c.id=s.config WHERE c.version=? AND c.checkpoint=90",
            (VOTE_VERSION,)).fetchall()
        assert [r[0] for r in skips]==([] if expected==.625 else
               ["MODEL_TIE_NO_DIRECTION"] if complete else ["MISSING_SYNCHRONIZED_VOTES"])
    verify(store,now,"UP")
    engine.tick(now+300)
    app.state.agreement.tick(now+300)
    with TestClient(app) as client:
        response=client.get("/api/export.xlsx")
        assert response.status_code==200
    book=load_workbook(BytesIO(response.content),read_only=True)
    audit=list(book["Model Vote Audit"].values)
    rows=[r for r in audit[1:] if r[1]==90]
    assert len(rows)==1 and rows[0][5]==expected
    assert len(list(book["Model Vote Trends"].values))>1 if complete else len(list(book["Model Vote Trends"].values))==1
    book.close()
