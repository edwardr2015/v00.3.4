"""Synthetic feed failure, discovery and adaptive mode boundaries."""
import asyncio
from datetime import datetime, timezone
import time

import httpx
from fastapi.testclient import TestClient

from prophet.app import create_app
from prophet.collector import Collector
from prophet.providers import PublicAPI, US_BASE
from prophet.store import Store
from test_timing_update import market_inputs
from test_v2 import setup


def test_retry_after_blocks_queued_requests_until_the_response_is_known():
    async def run():
        started = asyncio.Event()
        complete = asyncio.Event()
        requests = []

        async def handler(request):
            requests.append(request.url.path)
            if request.url.path == "/limit":
                started.set()
                await complete.wait()
                return httpx.Response(429, headers={"Retry-After": "1"})
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            api = PublicAPI(client)
            first = asyncio.create_task(api.get(US_BASE, "/limit"))
            await started.wait()
            second = asyncio.create_task(api.get(US_BASE, "/queued"))
            try:
                await asyncio.sleep(.18)
                assert requests == ["/limit"]
                complete.set()
                try:
                    await first
                except httpx.HTTPStatusError as exc:
                    assert exc.response.status_code == 429
                await asyncio.sleep(.05)
                assert requests == ["/limit"]
                assert await second == {"ok": True}
            finally:
                complete.set()
                await asyncio.gather(first, second, return_exceptions=True)

    asyncio.run(run())


def test_temporary_404_keeps_observed_contract_until_verified(tmp_path):
    async def run():
        store = Store(str(tmp_path/"queue.sqlite"))
        collector = Collector(store)
        slug = "observed-real-shape-fixture"
        store.put("market", dict(slug=slug), archive=True)
        store.put("pending_settlements", [slug, "never-observed"])
        end = time.time()-20
        iso = lambda epoch: datetime.fromtimestamp(epoch, timezone.utc).isoformat()
        resolved = dict(slug=slug, status="MARKET_STATUS_RESOLVED", assetPriceTerms=dict(
            asset={"symbol":"btc"},marketType="ASSET_PRICE_MARKET_TYPE_UP_DOWN",indexSymbol="BRTI",
            windowStart=iso(end-300),windowEnd=iso(end),priceToBeat={"value":"100"},
            settlementPrice={"value":"101"}))
        class API:
            missing = True

            async def get(self, origin, path):
                if self.missing:
                    request = httpx.Request("GET", origin+path)
                    response = httpx.Response(404, request=request)
                    response.raise_for_status()
                if path.endswith("/settlement"):
                    return dict(slug=slug,settlement=1)
                return dict(market=resolved)

        collector.api = API()
        await collector.settlement()
        assert store.get("pending_settlements") == [slug]
        collector.api.missing = False
        await collector.settlement()
        assert store.get("pending_settlements") == []
        assert store.get("outcome_"+slug)["outcome"] == "UP"

    asyncio.run(run())


def test_no_listed_market_is_refreshing_but_a_book_from_another_round_is_hidden(tmp_path, monkeypatch):
    store, _, now = setup(tmp_path)
    app = create_app(store.path,collect=False,auth_required=False)
    monkeypatch.setattr("time.time",lambda: now)
    with TestClient(app) as client:
        assert "Nine-model paper leaderboard" in client.get("/").text
        assert "toggle-paper" in client.get("/research-dashboard").text
        market = store.get("market")
        market["slug"] = "new-market-without-book"
        store.put("market",market)
        state = client.get("/api/state").json()
        assert state["health"]["market"]["status"]=="refreshing" and state["book"] is None
        market["closed"] = True
        store.put("market",market)
        store.put("health_market",dict(ok=False,checked=now-40,last_success=now-45))
        store.put("availability",dict(available=False,checked=now))
        assert client.get("/api/state").json()["health"]["market"]["status"]=="refreshing"
        app.state.collector.enabled = False
        assert client.get("/api/state").json()["health"]["market"]["status"]=="disconnected"


def test_adaptive_mode_keeps_observer_bankroll_separate(tmp_path):
    store, _, now = setup(tmp_path)
    app = create_app(store.path,collect=False,auth_required=False)
    p, research = app.state.paper, app.state.agreement
    store.put("observer_enabled", True)
    p.mode("SIMULATE", now)
    market_inputs(store,now,bid=.28,ask=.30)
    p.tick(now)
    research.tick(now)
    assert {r["account"] for r in p.summary()["orders"]}=={"adaptive-1","adaptive-2","adaptive-3"}
    with store.connect() as db:
        assert db.execute("SELECT cash FROM observer_accounts WHERE account='observer'").fetchone()[0]==1000
        assert not db.execute("SELECT 1 FROM observer_rounds WHERE account='observer'").fetchone()
        assert db.execute("SELECT count(*) FROM lab_orders").fetchone()[0]==3
