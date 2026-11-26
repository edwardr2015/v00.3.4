"""Verify recent settled rounds against official US endpoints; never alter live pending queues."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
import httpx
from prophet.analytics import initialize
from prophet.collector import Collector
from prophet.providers import PublicAPI
from prophet.store import Store


async def run(path, rounds):
    target = Store(path)
    initialize(target)
    boundary = int(time.time()) // 300 * 300
    verified = 0
    errors = []
    with tempfile.TemporaryDirectory(prefix="prophet-settlement-check-") as folder:
        staging = Store(str(Path(folder) / "staging.sqlite"))
        worker = Collector(staging)
        async with httpx.AsyncClient(
            timeout=15, headers={"User-Agent": "BTCProphetResearch/0.2"}
        ) as client:
            worker.api = PublicAPI(client)
            for i in range(1, rounds + 1):
                dt = datetime.fromtimestamp(boundary - i * 300, timezone.utc)
                slug = "cpc-btc-updown-5m-" + dt.strftime("%Y-%m-%d-%H%Mz")
                with target.connect() as db:
                    if db.execute(
                        "SELECT 1 FROM verified_markets WHERE slug=?", (slug,)
                    ).fetchone():
                        verified += 1
                        continue
                staging.put("pending_settlements", [slug])
                try:
                    await worker.settlement()
                    with staging.connect() as db:
                        result = db.execute(
                            "SELECT * FROM verified_markets WHERE slug=?", (slug,)
                        ).fetchone()
                    if result:
                        with target.connect() as db:
                            db.execute(
                                "INSERT OR IGNORE INTO verified_markets VALUES (?,?,?,?,?,?,?,?,?)",
                                tuple(result),
                            )
                        verified += 1
                except Exception as exc:
                    errors.append(dict(slug=slug, error=f"{type(exc).__name__}: {exc}"))
                if i % 10 == 0:
                    print(f"Checked {i}/{rounds}; verified {verified}", flush=True)
                await asyncio.sleep(0.5)
    report = dict(
        checked_utc=datetime.now(timezone.utc).isoformat(),
        requested=rounds,
        verified=verified,
        errors=errors,
        evidence="Official US typed BTC/BRTI/300s resolved contract + settlement endpoint + reference-price agreement",
    )
    (Path(path).parent / "verified-history-check.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="data/prophet.sqlite3")
    parser.add_argument("--rounds", type=int, default=60)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 288:
        parser.error("rounds must be between 1 and 288")
    asyncio.run(run(args.database, args.rounds))
