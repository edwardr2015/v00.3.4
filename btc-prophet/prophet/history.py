"""Auditable public-history import for research, isolated from live observations.

python -m prophet.history --start 2026-11-15 --end 2026-11-22
Dates are UTC and the end is exclusive. No historical fills are invented.
"""

import argparse
import asyncio
import gzip
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .providers import US_BASE, number, timestamp


def day(value):
    return int(datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def historical_market(m, start, end):
    t = m.get("assetPriceTerms") or {}
    try:
        a, b = timestamp(t["windowStart"]), timestamp(t["windowEnd"])
    except (KeyError, TypeError, ValueError):
        return None
    if (
        (t.get("asset") or {}).get("symbol") != "btc"
        or t.get("indexSymbol") != "BRTI"
        or t.get("marketType") != "ASSET_PRICE_MARKET_TYPE_UP_DOWN"
        or b - a != 300
        or not start <= a < end
        or b > end
        or m.get("status") != "MARKET_STATUS_RESOLVED"
    ):
        return None
    opening, closing = number(t["priceToBeat"], True), number(t["settlementPrice"], True)
    return dict(
        slug=m["slug"],
        start=int(a),
        end=int(b),
        opening=opening,
        closing=closing,
        outcome=int(closing >= opening),
        provenance="official resolved BRTI reference values",
    )


def reference_rows(payload, start, end):
    series = [
        s
        for s in payload["series"]
        if s.get("asset") == {"assetClass": "ASSET_CLASS_CRYPTO", "symbol": "btc"}
    ]
    if len(series) != 1 or series[0].get("bucket") != "ASSET_PRICE_BUCKET_15S":
        raise ValueError("Unexpected historical reference series")
    result = []
    for c in series[0]["candles"]:
        t = int(c["timestamp"])
        if not start <= t < end:
            continue  # Endpoint may include the bucket starting at the exclusive end.
        if t % 15:
            raise ValueError("Unaligned historical candle")
        ticks = int(c.get("tickCount", 0))
        if ticks <= 0:
            continue
        o, h, low, close = [number(c[k], True) for k in ("open", "high", "low", "close")]
        if not low <= min(o, close) <= max(o, close) <= h:
            raise ValueError("Invalid historical OHLC bounds")
        result.append((t, o, h, low, close, ticks))
    return result


def quote_rows(payload, start, end, issues=None):
    result = []
    for p in payload["history"]:
        t = int(p["timestamp"])
        if not start <= t < end:
            continue
        try:
            up, down = number(p["longPrice"]), number(p["shortPrice"])
            if up > 1 or down > 1 or up + down < 1 - 1e-6:
                raise ValueError("Inconsistent or out-of-range historical display quotes")
        except (ValueError, TypeError, KeyError) as exc:
            if issues is None:
                raise ValueError("Invalid historical display quotes") from exc
            issues.append((t, json.dumps(p), str(exc)))
            continue
        result.append((t, up, down))
    return result


class History:
    def __init__(self, path="data/history.sqlite3", cache="data/history-cache"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.rate_lock = asyncio.Lock()
        self.next_request = 0.0
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS reference15 (
              time INTEGER PRIMARY KEY, open REAL, high REAL, low REAL, close REAL, ticks INTEGER);
            CREATE TABLE IF NOT EXISTS markets (
              slug TEXT PRIMARY KEY, start INTEGER, end INTEGER, opening REAL, closing REAL,
              outcome INTEGER, provenance TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS quotes (
              slug TEXT, time INTEGER, up_ask REAL, down_ask REAL, PRIMARY KEY(slug,time));
            CREATE TABLE IF NOT EXISTS requests (
              url TEXT PRIMARY KEY, retrieved REAL, sha256 TEXT, file TEXT);
            CREATE TABLE IF NOT EXISTS imports (
              start INTEGER, end INTEGER, report TEXT, PRIMARY KEY(start,end));
            CREATE TABLE IF NOT EXISTS quote_issues (
              slug TEXT, time INTEGER, payload TEXT, reason TEXT, PRIMARY KEY(slug,time));
        """)

    def close(self):
        self.db.close()

    async def fetch(self, client, path, params):
        url = str(httpx.URL(US_BASE + path, params=params))
        key = hashlib.sha256(url.encode()).hexdigest()
        cached = self.db.execute("SELECT * FROM requests WHERE url=?", (url,)).fetchone()
        if cached:
            file = self.cache / (key + ".json.gz")
            if file.exists():
                raw = gzip.decompress(file.read_bytes())
                if hashlib.sha256(raw).hexdigest() == cached["sha256"]:
                    return json.loads(raw)
        for attempt in range(4):
            async with self.rate_lock:
                await asyncio.sleep(max(0, self.next_request - time.monotonic()))
                self.next_request = time.monotonic() + 0.15  # Shared limit under 7 requests/s.
            try:
                r = await client.get(url)
                r.raise_for_status()
                payload = r.json()
                raw = r.content
                file = self.cache / (key + ".json.gz")
                file.write_bytes(gzip.compress(raw, mtime=0))
                self.db.execute(
                    "INSERT OR REPLACE INTO requests VALUES (?,?,?,?)",
                    (url, time.time(), hashlib.sha256(raw).hexdigest(), str(file)),
                )
                self.db.commit()
                return payload
            except (httpx.HTTPError, ValueError):
                if attempt == 3:
                    raise
                retry = r.headers.get("Retry-After", "0") if "r" in locals() else "0"
                await asyncio.sleep(
                    min(60, max(2**attempt, float(retry) if retry.isdigit() else 0))
                )

    async def import_reference(self, client, start, end):
        # 12-hour chunks at 15 seconds: 2880 buckets, below the 3600-point cap.
        for left in range(start, end, 43200):
            right = min(end, left + 43200)
            data = await self.fetch(
                client,
                "/v1/asset-prices/history",
                {
                    "assets": "crypto:btc",
                    "from": left,
                    "to": right,
                    "bucket": "ASSET_PRICE_BUCKET_15S",
                },
            )
            rows = reference_rows(data, left, right)
            self.db.executemany("INSERT OR REPLACE INTO reference15 VALUES (?,?,?,?,?,?)", rows)
            self.db.commit()
            print(f"Reference {iso(left)}: {len(rows)} observed candles", flush=True)

    async def import_markets(self, client, start, end):
        # Candidate names follow the verified US naming pattern. Every returned
        # record is still checked for exact venue fields and a 300-second window.
        # An unreturned candidate is a coverage gap, never a fabricated market.
        candidates = [
            "cpc-btc-updown-5m-"
            + datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d-%H%Mz")
            for t in range(start, end, 300)
        ]
        for offset in range(0, len(candidates), 20):
            batch = candidates[offset : offset + 20]
            data = await self.fetch(
                client,
                "/v1/markets",
                {"limit": 100, "slug": batch},
            )
            markets = data["markets"]
            if any(m["slug"] not in batch for m in markets):
                raise ValueError("Server ignored the market slug filter; discovery is incomplete")
            added = 0
            for m in markets:
                row = historical_market(m, start, end)
                if not row:
                    continue
                self.db.execute(
                    "INSERT OR REPLACE INTO markets VALUES (?,?,?,?,?,?,?,?)",
                    (*row.values(), json.dumps(m)),
                )
                added += 1
            self.db.commit()
            print(
                f"Contract candidates {offset + len(batch)}/{len(candidates)}: {added} verified resolved rounds",
                flush=True,
            )

    async def import_quotes(self, client, start, end):
        markets = self.db.execute(
            "SELECT * FROM markets WHERE start>=? AND end<=? ORDER BY start", (start, end)
        ).fetchall()
        completed = 0
        queue = asyncio.Queue()
        for m in markets:
            queue.put_nowait(m)

        async def one(m):
            nonlocal completed
            data = await self.fetch(
                client,
                "/v1/price-history",
                {
                    "symbol": m["slug"],
                    "timestamp.startTimestamp": m["start"],
                    "timestamp.endTimestamp": m["end"],
                    "fidelity": 1,
                },
            )
            issues = []
            rows = quote_rows(data, m["start"], m["end"], issues=issues)
            self.db.executemany(
                "INSERT OR REPLACE INTO quote_issues VALUES (?,?,?,?)",
                [(m["slug"], *row) for row in issues],
            )
            self.db.executemany(
                "INSERT OR REPLACE INTO quotes VALUES (?,?,?,?)",
                [(m["slug"], *row) for row in rows],
            )
            self.db.commit()
            completed += 1
            if completed % 100 == 0 or completed == len(markets):
                print(f"Contract quote histories: {completed}/{len(markets)}", flush=True)

        async def worker():
            while not queue.empty():
                m = queue.get_nowait()
                try:
                    await one(m)
                finally:
                    queue.task_done()

        async with asyncio.TaskGroup() as group:
            for _ in range(4):
                group.create_task(worker())

    def report(self, start, end):
        days = []
        for left in range(start, end, 86400):
            right = min(end, left + 86400)
            count = self.db.execute(
                "SELECT count(*) FROM reference15 WHERE time>=? AND time<?", (left, right)
            ).fetchone()[0]
            complete = self.db.execute(
                "SELECT count(*) FROM reference15 WHERE time>=? AND time<? AND ticks=15",
                (left, right),
            ).fetchone()[0]
            markets = self.db.execute(
                "SELECT count(*) FROM markets WHERE start>=? AND end<=?", (left, right)
            ).fetchone()[0]
            quotes = self.db.execute(
                "SELECT count(DISTINCT q.slug) FROM quotes q JOIN markets m ON q.slug=m.slug WHERE m.start>=? AND m.end<=?",
                (left, right),
            ).fetchone()[0]
            issues = self.db.execute(
                "SELECT count(*) FROM quote_issues q JOIN markets m ON q.slug=m.slug WHERE m.start>=? AND m.end<=?",
                (left, right),
            ).fetchone()[0]
            days.append(
                dict(
                    date=iso(left)[:10],
                    expected_reference_candles=(right - left) // 15,
                    observed_reference_candles=count,
                    fully_published_candles=complete,
                    possible_five_minute_windows=(right - left) // 300,
                    resolved_contracts=markets,
                    contracts_with_display_quotes=quotes,
                    quarantined_quote_points=issues,
                )
            )
        report = dict(
            start=iso(start),
            end_exclusive=iso(end),
            days=days,
            historical_order_book_depth_available=False,
            fills_or_pnl_simulated=False,
            quote_semantics="Book-derived display ask prices, no quantities or guaranteed fills",
            outcome_semantics="Derived from official resolved opening/closing BRTI values",
            generated_at=iso(time.time()),
        )
        self.db.execute(
            "INSERT OR REPLACE INTO imports VALUES (?,?,?)", (start, end, json.dumps(report))
        )
        self.db.commit()
        return report


async def run(args):
    start, end = day(args.start), day(args.end)
    if not start < end <= time.time() or (end - start) > 31 * 86400:
        raise ValueError("Choose an elapsed range of at most 31 days")
    history = History(args.db, args.cache)
    try:
        async with httpx.AsyncClient(
            timeout=30, headers={"User-Agent": "BTCProphetResearch/0.1"}
        ) as c:
            await history.import_reference(c, start, end)
            await history.import_markets(c, start, end)
            await history.import_quotes(c, start, end)
        report = history.report(start, end)
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2), flush=True)
    finally:
        history.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--db", default="data/history.sqlite3")
    parser.add_argument("--cache", default="data/history-cache")
    parser.add_argument("--report", default="data/history-coverage.json")
    asyncio.run(run(parser.parse_args()))
