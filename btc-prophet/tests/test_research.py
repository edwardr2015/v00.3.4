"""Deterministic tests use synthetic fixtures only; they are never dashboard data."""

import asyncio
import time
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from prophet.app import create_app
from prophet.collector import Collector
from prophet.providers import PublicAPI, parse_book, parse_candles, validate_market
from prophet.store import Store


def market(now=None):
    now = time.time() if now is None else now
    start = int(now) // 300 * 300
    return dict(
        id="fixture",
        slug="fixture-us-market",
        active=True,
        closed=False,
        status="MARKET_STATUS_OPEN",
        assetPriceTerms=dict(
            asset={"symbol": "btc", "assetClass": "ASSET_CLASS_CRYPTO"},
            marketType="ASSET_PRICE_MARKET_TYPE_UP_DOWN",
            indexSymbol="BRTI",
            windowStart=datetime.fromtimestamp(start, timezone.utc).isoformat(),
            windowEnd=datetime.fromtimestamp(start + 300, timezone.utc).isoformat(),
            priceToBeat={"value": "100", "currency": "USD"},
        ),
    )


def test_contract_identity_and_exact_window():
    m = market()
    assert validate_market(m)
    m["assetPriceTerms"]["indexSymbol"] = "Chainlink"
    assert not validate_market(m)
    m = market()
    m["assetPriceTerms"]["windowEnd"] = m["assetPriceTerms"]["windowStart"]
    assert not validate_market(m)
    assert not validate_market(market(), now=time.time() + 600)


def test_discovery_checks_later_pages():
    calls = []

    def handler(request):
        offset = int(request.url.params["offset"])
        calls.append(offset)
        rows = [dict(id=str(i)) for i in range(100)] if offset == 0 else [market()]
        return httpx.Response(200, json={"markets": rows})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await PublicAPI(c).discover()

    assert asyncio.run(run())["id"] == "fixture"
    assert calls == [0, 100]


def test_repeated_pagination_is_unknown_not_absent():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, json={"markets": [{"id": str(i)} for i in range(100)]}
                )
            )
        ) as c:
            return await PublicAPI(c).discover()

    with pytest.raises(ValueError, match="pagination repeated"):
        asyncio.run(run())


def candle_payload():
    return {
        "series": [
            {
                "asset": {"assetClass": "ASSET_CLASS_CRYPTO", "symbol": "btc"},
                "bucket": "ASSET_PRICE_BUCKET_1S",
                "candles": [
                    dict(
                        timestamp=1700000000,
                        open="100",
                        high="102",
                        low="99",
                        close="101",
                        tickCount=1,
                    )
                ],
            }
        ]
    }


@pytest.mark.parametrize(
    "field,value", [("close", "NaN"), ("high", "90"), ("open", "-1"), ("timestamp", 9999999999)]
)
def test_bad_candles_rejected(field, value):
    data = candle_payload()
    data["series"][0]["candles"][0][field] = value
    with pytest.raises(ValueError):
        parse_candles(data)


def test_missing_publications_not_filled():
    data = candle_payload()
    assert len(parse_candles(data)) == 1
    data["series"][0]["candles"][0]["tickCount"] = 0
    assert parse_candles(data) == []


def test_candles_deduplicate_and_keep_gaps(tmp_path):
    s = Store(str(tmp_path / "test.sqlite"))
    rows = [
        dict(time=1700000010 + i, open=100 + i, high=100 + i, low=100 + i, close=100 + i, ticks=1)
        for i in range(15)
    ]
    s.save_candles(rows)
    s.save_candles(rows)
    bars = s.candles(15)
    assert len(bars) == 1 and bars[0]["samples"] == 15 and bars[0]["complete"]
    assert bars[0]["open"] == 100 and bars[0]["close"] == 114
    s.save_candles([dict(time=1700000040, open=200, high=200, low=200, close=200, ticks=1)])
    bars = s.candles(15)
    assert len(bars) == 2 and not bars[1]["complete"]  # No invented intervening candle.


def test_order_book_mismatch_and_crossed_quotes():
    data = {
        "marketData": {
            "marketSlug": "fixture",
            "transactTime": "2026-01-01T00:00:00Z",
            "bids": [{"px": {"value": "0.6"}, "qty": "5"}],
            "offers": [{"px": {"value": "0.5"}, "qty": "5"}],
        }
    }
    with pytest.raises(ValueError, match="another market"):
        parse_book(data, "other")
    with pytest.raises(ValueError, match="Crossed"):
        parse_book(data, "fixture")


def test_controls_freshness_and_live_lock(tmp_path):
    app = create_app(str(tmp_path / "test.sqlite"), collect=False, auth_required=False)
    app.state.store.put("reference", {"price": 100, "timestamp": time.time() - 50})
    with TestClient(app) as c:
        assert c.get("/").status_code == 200
        state = c.get("/api/state").json()
        assert state["health"]["reference"]["status"] == "refreshing"
        assert state["live_locked"] and state["prediction"] is None
        assert c.post("/api/control/stop").status_code == 403
        headers = {"X-Prophet-Control": "local-dashboard"}
        assert c.post("/api/control/stop", headers=headers).status_code == 200
        assert c.get("/api/state").json()["collecting"] is True
        c.post("/api/control/emergency", headers=headers)
        state = c.get("/api/state").json()
        assert state["emergency"] and state["collecting"] and state["mode"] == "OFF"
        review = c.get("/api/interventions").json()[0]
        assert c.post(f"/api/interventions/{review['id']}/approve", headers=headers, json={}).status_code == 200
        assert c.post("/api/control/review-reset", headers=headers).status_code == 200
        assert not c.get("/api/state").json()["emergency"]
        assert c.post("/api/control/live", headers=headers).status_code == 404
        assert (
            c.post(
                "/api/control/start", headers={**headers, "Origin": "https://bad.example"}
            ).status_code
            == 403
        )
        assert c.get("/api/candles?interval=17").status_code == 422
        assert c.get("/static/vendor/lightweight-charts.js").status_code == 200


def test_database_backup_and_trade_deduplication(tmp_path):
    s = Store(str(tmp_path / "test.sqlite"))
    trade = [(1, 1700000000, 100, 0.1, 1700000001)]
    s.save_trades(trade)
    s.save_trades(trade)
    dest = str(tmp_path / "backup.sqlite")
    s.backup(dest)
    with Store(dest).connect() as db:
        assert db.execute("SELECT count(*) FROM trades").fetchone()[0] == 1


@pytest.mark.parametrize("payout,expected_error", [(1, False), (0, True)])
def test_settlement_requires_resolved_status_and_agreeing_index_values(
    tmp_path, payout, expected_error
):
    s = Store(str(tmp_path / "test.sqlite"))
    s.put("pending_settlements", ["fixture-us-market"])
    m = market()
    m["status"] = "MARKET_STATUS_RESOLVED"
    m["assetPriceTerms"]["settlementPrice"] = {"value": "101"}

    def handler(request):
        data = (
            {"slug": m["slug"], "settlement": payout}
            if request.url.path.endswith("/settlement")
            else {"market": m}
        )
        return httpx.Response(200, json=data)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            worker = Collector(s)
            worker.api = PublicAPI(c)
            await worker.settlement()

    if expected_error:
        with pytest.raises(ValueError, match="disagrees"):
            asyncio.run(run())
        assert s.get("last_outcome") is None
    else:
        asyncio.run(run())
        assert s.get("last_outcome")["outcome"] == "UP"
        assert s.get("pending_settlements") == []
