"""Independent retrying collectors. A failed provider never becomes fake data."""

import asyncio
import logging
import time

import httpx

from .providers import (
    COINBASE_BASE,
    KRAKEN_BASE,
    US_BASE,
    PublicAPI,
    number,
    parse_book,
    parse_candles,
    timestamp,
    validate_market,
)

log = logging.getLogger(__name__)


class Collector:
    def __init__(self, store):
        self.store = store
        self.enabled = True
        self.tasks = []
        self.client = None
        self.api = None
        self.restart_lock = asyncio.Lock()

    async def start(self):
        prior = self.api
        self.client = httpx.AsyncClient(
            timeout=15, headers={"User-Agent": "BTCProphetResearch/0.2"}
        )
        self.api = PublicAPI(self.client)
        if isinstance(prior, PublicAPI):
            self.api.retry_until.update(prior.retry_until)
            self.api.next_request.update(prior.next_request)
        self.tasks = [
            asyncio.create_task(self.loop(name, fn, interval))
            for name, fn, interval in [
                ("reference", self.reference, 2),
                ("market", self.market, 1),
                ("spot", self.spot, 3),
                ("settlement", self.settlement, 15),
            ]
        ]

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        if self.client:
            await self.client.aclose()

    async def restart(self):
        async with self.restart_lock:
            await self.close()
            if self.enabled:
                await self.start()

    async def loop(self, name, fn, interval):
        failures = 0
        last_success = (self.store.get("health_" + name) or {}).get("last_success")
        while True:
            if self.enabled:
                try:
                    if await fn() is not False:
                        failures = 0
                        last_success = time.time()
                        self.store.put("health_" + name, dict(ok=True, checked=last_success,
                                                             last_success=last_success,error=None))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failures += 1
                    error = f"{type(exc).__name__}: {exc}"[:350]
                    retry = self.api.retry_until.get(KRAKEN_BASE if name=="spot" else US_BASE,0) if isinstance(self.api,PublicAPI) else 0
                    self.store.put("health_" + name, dict(ok=False,checked=time.time(),
                        last_success=last_success,error=error,failures=failures,
                        retry_at=time.time()+max(0,retry-time.monotonic())))
                    if failures == 1 or failures % 10 == 0:
                        self.store.event("ERROR", f"{name}: {error}")
                        log.exception("%s collector failed", name)
            await asyncio.sleep(min(60, interval * 2 ** min(failures, 4)))

    async def reference(self):
        now = int(time.time())
        previous = self.store.get("reference")
        start = now - 1800 if not previous else max(now - 3600, int(previous["timestamp"]) - 5)
        data = await self.api.get(
            US_BASE,
            "/v1/asset-prices/history",
            assets="crypto:btc",
            **{"from": start, "to": now},
            bucket="ASSET_PRICE_BUCKET_1S",
        )
        candles = parse_candles(data)
        if not candles:
            raise ValueError("No BRTI publications returned")
        self.store.save_candles(candles)
        if previous and candles[-1]["time"] <= previous["timestamp"]:
            return
        self.store.put(
            "reference",
            dict(
                price=candles[-1]["close"],
                timestamp=candles[-1]["time"],
                received=time.time(),
                source="Polymarket US · CF Benchmarks BRTI",
            ),
        )

    async def market(self):
        current = self.store.get("market")
        if not current or not validate_market(current):
            if current:
                end = timestamp(current["assetPriceTerms"]["windowEnd"])
                previous = self.store.get("last_outcome", {})
                pending = self.store.get("pending_settlements", [])
                if end <= time.time() and previous.get("slug") != current["slug"]:
                    if current["slug"] not in pending:
                        pending.append(current["slug"])
                        self.store.put("pending_settlements", pending)
            # Keep observation running even when no suitable contract is listed.
            last_scan = self.store.get("last_scan", 0)
            if time.time() - last_scan < 5:
                return False
            self.store.put("last_scan", time.time())
            current = await self.api.discover()
            if not current:
                self.store.put(
                    "availability",
                    {
                        "available": False,
                        "checked": time.time(),
                        "message": "No open five-minute BTC US market found.",
                    },
                )
                return False
            self.store.event("INFO", "Verified US five-minute contract: " + current["slug"])
            self.store.put("last_market_metadata", time.time())
        elif time.time() - self.store.get("last_market_metadata", 0) >= (
            12 if current["assetPriceTerms"].get("priceToBeat") else 2
        ):
            data = await self.api.get(US_BASE, "/v1/market/slug/" + current["slug"])
            self.store.put("last_market_metadata", time.time())
            current = data["market"]
            if not validate_market(current):
                self.store.put("market", current, archive=True)
                return False
        if current != self.store.get("market"):
            self.store.put("market", current, archive=True)
        self.seed_recent_history(current)
        self.store.put(
            "availability",
            {
                "available": True,
                "checked": time.time(),
                "message": "Verified five-minute BTC contract · US production",
            },
        )
        # The bare /book route returned 404 during verification; this variant returned the book.
        data = await self.api.get(US_BASE, "/v1/markets/" + current["slug"] + "/book", limit=100)
        book = parse_book(data, current["slug"])
        prior = self.store.get("book") or {}
        if prior.get("slug") == book["slug"] and book["timestamp"] < prior["timestamp"]:
            raise ValueError("Order-book response regressed to an older timestamp")
        if book != prior:
            self.store.put("book", book, archive=True)

    def seed_recent_history(self, market):
        """Candidates only: final identity and settlement checks still apply."""
        import re
        from datetime import datetime, timezone

        import os

        if os.getenv("PROPHET_FRESH_RESEARCH") == "1" or self.store.get("history_bootstrap_v2"):
            return
        match = re.fullmatch(r"(cpc-btc-updown-5m-)\d{4}-\d{2}-\d{2}-\d{4}z", market["slug"])
        if not match:
            return
        from .analytics import initialize

        initialize(self.store)
        boundary = int(timestamp(market["assetPriceTerms"]["windowStart"]))
        pending = self.store.get("pending_settlements", [])
        with self.store.connect() as db:
            for i in range(1, 61):
                slug = match[1] + datetime.fromtimestamp(boundary - i * 300, timezone.utc).strftime(
                    "%Y-%m-%d-%H%Mz"
                )
                if (
                    slug not in pending
                    and not db.execute(
                        "SELECT 1 FROM verified_markets WHERE slug=?", (slug,)
                    ).fetchone()
                ):
                    pending.append(slug)
        self.store.put("pending_settlements", pending)
        self.store.put("history_bootstrap_v2", dict(time=time.time(), candidate_rounds=60))

    async def settlement(self):
        pending = self.store.get("pending_settlements", [])
        # Rotate the pending work so a delayed older settlement cannot starve newer rounds.
        batch = pending[:20]
        if len(pending) > len(batch):
            self.store.put("pending_settlements", pending[len(batch):] + batch)
        for slug in batch:
            try:
                data = await self.api.get(US_BASE, "/v1/market/slug/" + slug)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                # A previously observed contract can temporarily disappear from its
                # metadata endpoint. Keep its outcome queued until it is verified.
                with self.store.connect() as db:
                    known = db.execute("""SELECT 1 FROM observations
                        WHERE source='market' AND json_extract(payload,'$.slug')=?
                        LIMIT 1""", (slug,)).fetchone()
                if known:
                    continue
                self.store.event("INFO", "Historical candidate unavailable: " + slug)
                remaining = self.store.get("pending_settlements", [])
                self.store.put("pending_settlements", [x for x in remaining if x != slug])
                continue
            m = data["market"]
            if m.get("status") != "MARKET_STATUS_RESOLVED":
                continue
            terms = m["assetPriceTerms"]
            if (
                m.get("slug") != slug
                or terms.get("asset", {}).get("symbol") != "btc"
                or terms.get("marketType") != "ASSET_PRICE_MARKET_TYPE_UP_DOWN"
                or terms.get("indexSymbol") != "BRTI"
                or timestamp(terms["windowEnd"]) - timestamp(terms["windowStart"]) != 300
            ):
                raise ValueError("Settlement is not the expected US BTC five-minute BRTI contract")
            if not terms.get("priceToBeat") or not terms.get("settlementPrice"):
                continue
            opening = number(terms["priceToBeat"], True)
            closing = number(terms["settlementPrice"], True)
            expected = int(closing >= opening)
            source = "Polymarket US final settlement"
            try:
                result = await self.api.get(US_BASE, "/v1/markets/" + slug + "/settlement")
                payout = number(result["settlement"])
                if result.get("slug") != slug or payout not in (0, 1):
                    raise ValueError("Unexpected binary settlement result")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                sides = m.get("marketSides", [])
                if len(sides) != 2 or not all(type(side.get("long")) is bool for side in sides):
                    continue
                values = {side["long"]: number(side["price"]) for side in sides}
                if values != {True: expected, False: 1-expected}:
                    continue
                payout = values[True]
                source = "Polymarket US resolved sides and official BRTI terms"
            if expected != payout:
                raise ValueError("Settlement payout disagrees with official reference values")
            outcome = dict(
                slug=slug,
                outcome="UP" if payout == 1 else "DOWN",
                yes_payout=payout,
                opening=opening,
                closing=closing,
                received=time.time(),
                window_start=timestamp(terms["windowStart"]),
                window_end=timestamp(terms["windowEnd"]),
                source=source,
            )
            from .analytics import initialize, record_market

            initialize(self.store)
            record_market(
                self.store, outcome, timestamp(terms["windowStart"]), timestamp(terms["windowEnd"])
            )
            self.store.put("outcome_" + slug, outcome, archive=True)
            if outcome["window_end"] >= self.store.get("last_outcome", {}).get("window_end", 0):
                self.store.put("last_outcome", outcome)
            remaining = self.store.get("pending_settlements", [])
            self.store.put("pending_settlements", [x for x in remaining if x != slug])
            self.store.event("INFO", "Official result: " + slug + " → " + outcome["outcome"])

    async def spot(self):
        # Spot is for the dashboard. This failover never supplies BRTI or book fields.
        try:
            primary = await self.kraken_spot()
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            primary = None
        now = time.time()
        if primary and 0 <= now-primary["timestamp"] <= 15:
            self.publish_spot(primary)
            return
        ticker = await self.api.get(COINBASE_BASE, "/products/BTC-USD/ticker")
        source_time = timestamp(ticker["time"])
        if not 0 <= time.time()-source_time <= 15:
            raise ValueError("No fresh spot tick from Kraken or Coinbase")
        self.publish_spot(dict(price=number(ticker["price"],True),timestamp=source_time,
                               received=time.time(),source="Coinbase Exchange BTC/USD",fallback=True))

    def publish_spot(self, observation):
        previous = self.store.get("spot") or {}
        if (observation["source"] != previous.get("source") or
            observation["timestamp"] > previous.get("timestamp",0)):
            self.store.put("spot", observation)

    async def kraken_spot(self):
        cursor = self.store.get("kraken_cursor")
        params = dict(pair="XBTUSD", count=1000)
        if cursor:
            params["since"] = cursor
        data = await self.api.get(KRAKEN_BASE, "/0/public/Trades", **params)
        if data.get("error"):
            raise ValueError(str(data["error"]))
        result = data["result"]
        keys = [k for k in result if k != "last"]
        if len(keys) != 1 or keys[0] not in ("XXBTZUSD", "XBTUSD"):
            raise ValueError("Unexpected Kraken trade pair")
        trades = []
        now = time.time()
        for t in result[keys[0]]:
            ts, price, volume = timestamp(t[2]), number(t[0], True), number(t[1])
            if ts > now + 5 or ts < 1230768000:
                raise ValueError("Invalid Kraken trade timestamp")
            trades.append((int(t[6]), ts, price, volume, now))
        self.store.save_trades(trades)
        self.store.put("kraken_cursor", result["last"])
        if trades:
            latest = max(trades, key=lambda trade: (trade[1],trade[0]))
            return dict(price=latest[2],timestamp=latest[1],received=now,
                        source="Kraken BTC/USD · spot exchange",fallback=False)
        previous = self.store.get("spot") or {}
        return previous if previous.get("source","").startswith("Kraken") else None
