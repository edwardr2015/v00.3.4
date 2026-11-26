"""Validated public API reads. US and international markets are never mixed."""

import asyncio
from collections import defaultdict
from email.utils import parsedate_to_datetime
import math
import time
from datetime import datetime

US_BASE = "https://gateway.polymarket.us"
KRAKEN_BASE = "https://api.kraken.com"
COINBASE_BASE = "https://api.exchange.coinbase.com"


def timestamp(value):
    if isinstance(value, (float, int)):
        result = float(value)
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Timestamp must include a UTC offset")
        result = parsed.timestamp()
    if not math.isfinite(result):
        raise ValueError("Invalid timestamp")
    return result


def number(value, positive=False):
    if isinstance(value, dict):
        value = value["value"]
    v = float(value)
    if not math.isfinite(v) or v < 0 or (positive and v == 0):
        raise ValueError("Invalid numeric price or quantity")
    return v


def validate_market(m, now=None):
    now = time.time() if now is None else now
    t = m.get("assetPriceTerms") or {}
    if (
        (t.get("asset") or {}).get("symbol") != "btc"
        or t.get("marketType") != "ASSET_PRICE_MARKET_TYPE_UP_DOWN"
        or t.get("indexSymbol") != "BRTI"
    ):
        return False
    try:
        start, end = timestamp(t["windowStart"]), timestamp(t["windowEnd"])
    except (ValueError, KeyError, TypeError):
        return False
    return (
        end - start == 300
        and start <= now < end
        and m.get("active") is True
        and not m.get("closed")
        and m.get("status") == "MARKET_STATUS_OPEN"
    )


def parse_candles(payload, now=None):
    now = time.time() if now is None else now
    series = [
        s
        for s in payload["series"]
        if s.get("asset") == {"assetClass": "ASSET_CLASS_CRYPTO", "symbol": "btc"}
    ]
    if len(series) != 1 or series[0]["bucket"] != "ASSET_PRICE_BUCKET_1S":
        raise ValueError("Unexpected reference-price series")
    result = []
    for c in series[0]["candles"]:
        t = int(c["timestamp"])
        if t > now + 5 or t < 1230768000:
            raise ValueError("Invalid reference-price timestamp")
        if int(c.get("tickCount", 0)) <= 0:
            continue  # Never draw invented prices for a missing publication.
        o, h, low, close = [number(c[k], True) for k in ("open", "high", "low", "close")]
        if not low <= min(o, close) <= max(o, close) <= h:
            raise ValueError("Invalid OHLC bounds")
        result.append(dict(time=t, open=o, high=h, low=low, close=close, ticks=int(c["tickCount"])))
    return sorted(result, key=lambda x: x["time"])


def parse_book(payload, slug):
    book = payload["marketData"]
    if book["marketSlug"] != slug:
        raise ValueError("Order book belongs to another market")
    ts = timestamp(book["transactTime"])
    if ts > time.time() + 5:
        raise ValueError("Order-book timestamp is in the future")
    sides = {}
    for source, target, reverse in [("bids", "bids", True), ("offers", "asks", False)]:
        levels = [
            {"price": number(x["px"]), "quantity": number(x["qty"])} for x in book.get(source, [])
        ]
        if any(x["price"] > 1 for x in levels):
            raise ValueError("Contract price outside [0,1]")
        sides[target] = sorted(
            [x for x in levels if x["quantity"] > 0], key=lambda x: x["price"], reverse=reverse
        )
    if sides["bids"] and sides["asks"] and sides["bids"][0]["price"] >= sides["asks"][0]["price"]:
        raise ValueError("Crossed or locked order book")
    return dict(**sides, timestamp=ts, slug=slug, state=book.get("state"))


class PublicAPI:
    def __init__(self, client):
        self.client = client
        self.locks = defaultdict(asyncio.Lock)
        self.next_request = defaultdict(float)
        self.retry_until = defaultdict(float)

    async def get(self, base, path, **params):
        async with self.locks[base]:
            while True:
                delay = max(self.next_request[base], self.retry_until[base])-time.monotonic()
                if delay <= 0:
                    break
                await asyncio.sleep(delay)
            self.next_request[base] = time.monotonic() + (0.125 if base == US_BASE else 1)
            response = await self.client.get(base + path, params=params)
            if response.status_code in (429,503):
                header = response.headers.get("Retry-After", "")
                try:
                    retry = float(header) if header.isdigit() else parsedate_to_datetime(header).timestamp()-time.time()
                except (TypeError, ValueError, OverflowError):
                    retry = 1
                self.retry_until[base] = max(self.retry_until[base],time.monotonic()+max(1,retry))
            response.raise_for_status()
            return response.json()

    async def discover(self):
        """Complete bounded pagination; absence is never inferred from page one."""
        seen = set()
        for offset in range(0, 10000, 100):
            data = await self.get(
                US_BASE,
                "/v1/markets",
                categories="crypto",
                closed="false",
                limit=100,
                offset=offset,
            )
            markets = data["markets"]
            ids = {m["id"] for m in markets}
            if markets and ids.issubset(seen):
                raise ValueError("Market pagination repeated; availability is unknown")
            seen.update(ids)
            matches = [m for m in markets if validate_market(m)]
            if matches:
                return matches[0]
            if len(markets) < 100:
                return None
        raise ValueError("Market scan limit reached; availability is unknown")
