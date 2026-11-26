"""Synthetic forward timing, recovery and safety checks."""

import asyncio
import io
import time
from collections import Counter

import httpx
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from prophet.app import create_app
from prophet.collector import Collector
from prophet.lab import Lab
from prophet.providers import PublicAPI, US_BASE
from prophet.v2paper import Engine
from test_v2 import setup, verify


def market_inputs(store, now, *, ask=.90, bid=.89):
    book = store.get("book")
    book.update(timestamp=now, bids=[dict(price=bid,quantity=200)], asks=[dict(price=ask,quantity=200)])
    store.put("book", book)
    store.put("reference", dict(timestamp=now,price=100))
    for name in ("market","reference"):
        store.put("health_"+name,dict(ok=True,checked=now,error=None))


def test_six_timers_choose_the_model_side_even_with_negative_edge(tmp_path, monkeypatch):
    store, paper, now = setup(tmp_path)
    lab = Lab(store,paper)
    start = now-90
    # Complete quiet feature candles for the earliest checkpoints.
    store.save_candles([dict(time=t,open=100,high=100,low=100,close=100,ticks=1)
                        for t in range(start-315,start+120)])
    paper.mode("SIMULATE",start)
    monkeypatch.setattr(paper,"probability",lambda *args,**kwargs: (.50, {}))
    monkeypatch.setattr(lab,"predict",lambda version,x,checkpoint,slug,now: 0.51 if checkpoint<=45 else 0.49)
    for second in (15,30,45,60,75,90):
        t = start+second
        market_inputs(store,t,ask=.51,bid=.49)
        paper.tick(t-1)
        paper.tick(t)
        lab.tick(t)
    paper.tick(start+300)
    lab.tick(start+300)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM lab_orders").fetchone()[0] == 18
        assert dict(db.execute("SELECT side,count(*) FROM lab_orders GROUP BY side")) == {"UP":9,"DOWN":9}
        assert all(row[0]>=0 for row in db.execute("SELECT cash FROM lab_configs"))
        assert db.execute("SELECT max(time) FROM lab_orders").fetchone()[0] == start+90
    verify(store,now)
    Lab(store,Engine(store)).tick(now+300)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM lab_orders WHERE status='SETTLED'").fetchone()[0] == 18


def test_delayed_skip_recovers_without_manual_reset(tmp_path):
    store, paper, now = setup(tmp_path)
    lab = Lab(store,paper)
    paper.mode("SIMULATE",now-90)
    market_inputs(store,now-58)
    paper.tick(now-25)  # Stale input near the 60-second checkpoint.
    lab.tick(now-25)
    assert store.get("mode") == "SIMULATE"
    assert not any(a["halted"] for a in paper.summary()["accounts"])
    market_inputs(store,now)
    paper.tick(now)
    lab.tick(now)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM lab_skips WHERE reason LIKE 'STALE%'").fetchone()[0]>0
        assert db.execute("SELECT count(*) FROM lab_orders").fetchone()[0]>0


def test_rate_limit_shared_by_venue_and_automatic_refresh(tmp_path):
    from prophet.store import Store
    
    real_sleep = asyncio.sleep
    counter = Counter()

    async def run():
        store = Store(str(tmp_path/"refresh.sqlite3"))
        collector = Collector(store)

        async def handle(request):
            counter[request.url.path] += 1
            return httpx.Response(429,headers={"Retry-After":"1"}) if request.url.path=="/limit" else httpx.Response(200,json={})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            api = collector.api = PublicAPI(client)
            try:
                await api.get(US_BASE,"/limit")
            except httpx.HTTPStatusError:
                pass
            assert api.retry_until[US_BASE]-time.monotonic()>.8
            pending = asyncio.create_task(api.get(US_BASE,"/after"))
            await real_sleep(.02)
            assert counter["/after"] == 0
            await pending
            checks = Counter()

            async def transient():
                checks["calls"] += 1
                if checks["calls"] == 1:
                    raise httpx.ConnectError("Temporary disconnect")
                return None

            task=asyncio.create_task(collector.loop("reference",transient,.01))
            await real_sleep(.08)
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            assert checks["calls"]>=2
            assert store.get("health_reference")["ok"]
    asyncio.run(run())


def test_denial_requests_followup_and_excel_audits(tmp_path):
    app=create_app(str(tmp_path/"review.sqlite3"),collect=False,auth_required=False)
    headers={"X-Prophet-Control":"local-dashboard"}
    with TestClient(app) as client:
        assert client.post("/api/control/emergency",headers=headers).status_code==200
        request=client.get("/api/interventions").json()[0]
        assert client.post(f"/api/interventions/{request['id']}/deny",headers=headers,json={"note":"Need details"}).status_code==200
        follow=client.get("/api/interventions").json()[0]
        assert follow["parent"]==request["id"] and "still unresolved" in follow["request"]
        assert client.post("/api/control/review-reset",headers=headers).status_code==409
        assert client.post(f"/api/interventions/{follow['id']}/approve",headers=headers,json={}).status_code==200
        assert client.get("/api/state").json()["emergency"] is True
        assert client.post("/api/control/review-reset",headers=headers).status_code==200
        app.state.store.event("ERROR","Synthetic test fault")
        sheets=load_workbook(io.BytesIO(client.get("/api/export.xlsx").content),read_only=True)
        assert {"Bug Log","Human Review","Timing vs 90s Matched"}<=set(sheets.sheetnames)


def test_equal_probability_and_no_cash_record_skips(tmp_path, monkeypatch):
    store,paper,now=setup(tmp_path)
    lab=Lab(store,paper)
    paper.mode("SIMULATE",now)
    with store.connect() as db:
        db.execute("UPDATE lab_configs SET cash=0 WHERE status='FORWARD SHADOW' AND stake=3")
    monkeypatch.setattr(lab,"predict",lambda *args: .5)
    paper.tick(now)
    lab.tick(now)
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM lab_orders").fetchone()
        reasons=dict(db.execute("SELECT reason,count(*) FROM lab_skips GROUP BY reason"))
        assert reasons["MODEL_TIE_NO_DIRECTION"]==2
        assert reasons["INSUFFICIENT_CASH"]==2


def test_volatility_does_not_increase_budget_or_allow_post_close(tmp_path, monkeypatch):
    store,paper,now=setup(tmp_path)
    lab=Lab(store,paper)
    paper.mode("SIMULATE",now)
    monkeypatch.setattr(lab,"predict",lambda *args: .9)
    start=now-90
    store.save_candles([dict(time=t,open=100,high=100,low=100,close=100,ticks=1)
                        for t in range(start-315,start)])
    store.save_candles([dict(time=t,open=130,high=130,low=130,close=130,ticks=1)
                        for t in range(start+60,start+90)])
    paper.tick(now)
    lab.tick(start+300)  # A formerly valid signal may not backdate a late fill.
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM lab_orders").fetchone()
        assert db.execute("SELECT 1 FROM lab_skips WHERE reason='MISSED_CHECKPOINT'").fetchone()
    market=store.get("market")
    market["slug"]="test-volatility"
    store.put("market",market)
    book=store.get("book")
    book["slug"]=market["slug"]
    store.put("book",book)
    paper.tick(now)
    lab.tick(now)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM lab_orders").fetchone()[0]>0
        assert all(cost<=stake for cost,stake in db.execute(
            "SELECT o.cost,c.stake FROM lab_orders o JOIN lab_configs c ON o.config=c.id"))


def test_spot_fails_over_and_recovers_without_touching_official_feeds(tmp_path):
    from datetime import datetime, timezone
    from prophet.providers import COINBASE_BASE, KRAKEN_BASE
    from prophet.store import Store

    store=Store(str(tmp_path/"spot.sqlite3"))
    collector=Collector(store)
    store.put("reference",{"timestamp":1,"price":100})
    store.put("book",{"timestamp":2,"slug":"unchanged"})
    now=time.time()

    class Feed:
        failed=True

        async def get(self,base,path,**params):
            if base==KRAKEN_BASE:
                if self.failed:
                    raise httpx.ConnectError("test failure")
                return {"error":[],"result":{"XXBTZUSD":[["101","1",now,"b","m","",111]],"last":"next"}}
            assert base==COINBASE_BASE
            return {"price":"102","time":datetime.fromtimestamp(now,timezone.utc).isoformat()}

    async def run():
        collector.api=Feed()
        await collector.spot()
        assert store.get("spot")["fallback"] is True
        assert store.get("spot")["timestamp"] == datetime.fromtimestamp(now,timezone.utc).timestamp()
        collector.api.failed=False
        await collector.spot()
        assert store.get("spot")["fallback"] is False
        assert store.get("spot")["price"]==101
        assert store.get("book")["slug"]=="unchanged" and store.get("reference")["price"]==100
    asyncio.run(run())


def test_broken_model_does_not_block_healthy_models(tmp_path, monkeypatch):
    store,paper,now=setup(tmp_path)
    lab=Lab(store,paper)
    with store.connect() as db:
        db.execute("INSERT INTO model_registry VALUES ('bad','synthetic',?,'fixture','file','hash','{}')", (now,))
        db.execute("INSERT INTO lab_configs VALUES (?,?,?,?,?,?,?,?,?)",
                   ("timing/v2/bad/90/HOLD/1","synthetic","bad",90,"HOLD",1,"FORWARD SHADOW",100,now))
    def predict(version,*args):
        if version=="bad":
            raise RuntimeError("Synthetic inference fault")
        return .7
    monkeypatch.setattr(lab,"predict",predict)
    paper.mode("SIMULATE",now)
    paper.tick(now)
    lab.tick(now)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM lab_orders").fetchone()[0]==3
        assert db.execute("SELECT 1 FROM lab_skips WHERE config LIKE 'timing/v2/bad/%' AND reason='MODEL_PREDICTION_UNAVAILABLE'").fetchone()
        assert db.execute("SELECT 1 FROM events WHERE level='ERROR' AND message LIKE '%Synthetic inference fault%'").fetchone()
    assert store.get("mode")=="SIMULATE"


def test_direction_display_waits_tallies_twenty_and_counts_only_benchmark(tmp_path, monkeypatch):
    import json
    from datetime import datetime, timezone

    monkeypatch.setenv("PROPHET_PUBLIC_DISPLAY","1")
    app=create_app(str(tmp_path/"display.sqlite3"),collect=False,auth_required=False)
    store=app.state.store
    start=time.time()-90
    def iso(epoch):
        return datetime.fromtimestamp(epoch,timezone.utc).isoformat()
    market=dict(slug="display-test",active=True,closed=False,status="MARKET_STATUS_OPEN",
                assetPriceTerms=dict(asset={"symbol":"btc"},marketType="ASSET_PRICE_MARKET_TYPE_UP_DOWN",
                   indexSymbol="BRTI",windowStart=iso(start),windowEnd=iso(start+300)))
    store.put("market",market)
    with TestClient(app) as client:
        app.state.paper.mode("SIMULATE",start-10000)
        assert client.get("/api/display").json()["decision_display"]["call"] is None
        with store.connect() as db:
            for i in range(23):
                direction="UP" if i>=8 else "DOWN"
                db.execute("INSERT INTO v2_decisions VALUES (?,?,?,?,?,?,?,?)",
                           (f"old-{i}",15,start-60+i,"fixture",.6 if direction=="UP" else .4,
                            direction,"fixture",json.dumps({"protocol":"fixed-timing-v1"})))
            db.execute("INSERT INTO v2_decisions VALUES (?,?,?,?,?,?,?,?)",
                       ("display-test",90,start+89,"fixture",.4,"DOWN","fixture",
                        json.dumps({"protocol":"fixed-timing-v1"})))
            config=db.execute("""SELECT id FROM lab_configs WHERE version='momentum-v2' AND
                               checkpoint=90 AND stake=1 AND exit='HOLD'""").fetchone()[0]
            for i,pnl in enumerate([.5,.5,-1,-1,-1]):
                db.execute("""INSERT INTO lab_orders(config,slug,side,time,status,pnl,settled)
                             VALUES (?,?,'UP',?,'SETTLED',?,?)""",
                           (config,f"settled-{i}",start-90+i,pnl,start-20+i))
        data=client.get("/api/display").json()
        assert data["decision_display"]["call"]["decision"]=="DOWN"
        assert data["decision_display"]["count"]==20 and data["decision_display"]["up_percent"]==75
        assert data["decision_display"]["down_percent"]==25
        result=data["benchmark_results"]
        assert (result["wins"],result["losses"],result["longest_win"],result["longest_loss"],result["pnl"])==(2,3,2,3,-2)
        assert client.get("/api/state").json()["benchmark_results"]==result
        app.state.paper.mode("OFF")
        assert client.get("/api/display").json()["decision_display"]["call"] is None
