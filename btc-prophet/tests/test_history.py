"""Historical fixtures are synthetic and never imported into the user's dataset."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from prophet.app import create_app
from prophet.history import History, day, historical_market, iso, quote_rows, reference_rows


def resolved(start):
    return dict(
        slug="cpc-btc-updown-5m-2026-11-15-0000z",
        status="MARKET_STATUS_RESOLVED",
        assetPriceTerms=dict(
            asset={"symbol": "btc"},
            indexSymbol="BRTI",
            marketType="ASSET_PRICE_MARKET_TYPE_UP_DOWN",
            windowStart=iso(start),
            windowEnd=iso(start + 300),
            priceToBeat={"value": "100"},
            settlementPrice={"value": "100"},
        ),
    )


def test_historical_identity_and_ties():
    start = day("2026-11-15")
    m = resolved(start)
    assert historical_market(m, start, start + 300)["outcome"] == 1
    m["status"] = "MARKET_STATUS_OPEN"
    assert historical_market(m, start, start + 300) is None
    m["status"] = "MARKET_STATUS_RESOLVED"
    m["assetPriceTerms"]["indexSymbol"] = "Chainlink"
    assert historical_market(m, start, start + 300) is None


def test_reference_exclusive_end_and_missing_publications():
    start = day("2026-11-15")
    data = {
        "series": [
            dict(
                asset={"assetClass": "ASSET_CLASS_CRYPTO", "symbol": "btc"},
                bucket="ASSET_PRICE_BUCKET_15S",
                candles=[
                    dict(
                        timestamp=start + i * 15,
                        open="100",
                        high="101",
                        low="99",
                        close="100",
                        tickCount=ticks,
                    )
                    for i, ticks in enumerate([15, 0, 15])
                ],
            )
        ]
    }
    rows = reference_rows(data, start, start + 30)
    assert len(rows) == 1 and rows[0][0] == start


def test_quote_semantics_and_time_bounds():
    data = {"history": [dict(timestamp=t, longPrice=0.55, shortPrice=0.46) for t in [0, 60, 300]]}
    assert quote_rows(data, 0, 300) == [(0, 0.55, 0.46), (60, 0.55, 0.46)]
    data["history"][0]["shortPrice"] = 0.3
    with pytest.raises(ValueError):
        quote_rows(data, 0, 300)
    issues = []
    assert quote_rows(data, 0, 300, issues=issues) == [(60, 0.55, 0.46)]
    assert len(issues) == 1 and issues[0][0] == 0


def test_import_validates_batch_filter_and_resumes_cache(tmp_path):
    start = day("2026-11-15")
    h = History(tmp_path / "history.sqlite", tmp_path / "cache")
    calls = []

    def handler(request):
        calls.append(str(request.url))
        assert request.url.params.get_list("slug") == [resolved(start)["slug"]]
        return httpx.Response(200, json={"markets": [resolved(start)]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await h.import_markets(c, start, start + 300)
            await h.import_markets(c, start, start + 300)

    try:
        asyncio.run(run())
        assert len(calls) == 1
        assert h.db.execute("SELECT count(*) FROM markets").fetchone()[0] == 1
        report = h.report(start, start + 300)
        assert report["days"][0]["resolved_contracts"] == 1
        assert report["days"][0]["observed_reference_candles"] == 0
        assert not report["fills_or_pnl_simulated"]
    finally:
        h.close()


def test_ignored_slug_filter_rejected(tmp_path):
    start = day("2026-11-15")
    h = History(tmp_path / "h.sqlite", tmp_path / "cache")
    m = resolved(start)
    m["slug"] = "unrequested-market"

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"markets": [m]}))
        ) as c:
            await h.import_markets(c, start, start + 300)

    try:
        with pytest.raises(ValueError, match="ignored"):
            asyncio.run(run())
    finally:
        h.close()


def test_dashboard_research_status_is_read_only(tmp_path, monkeypatch):
    path = tmp_path / "history.sqlite"
    h = History(path, tmp_path / "cache")
    h.close()
    monkeypatch.setenv("PROPHET_HISTORY_DB", str(path))
    with TestClient(
        create_app(str(tmp_path / "live.sqlite"), collect=False, auth_required=False)
    ) as c:
        s = c.get("/api/research").json()
        assert s["available"] and s["resolved_contracts"] == 0
        assert not s["active_model_changed"] and not s["live_trading"]
        assert s["baseline"] is None and s["coverage"] is None
