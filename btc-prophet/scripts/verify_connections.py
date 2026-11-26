"""Opt-in, read-only live checks. No account credentials or trading calls."""

import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import time
import httpx
from prophet.collector import Collector
from prophet.adaptive_entry import BOOK_CONFIRM_MAX_AGE, REFERENCE_MAX_AGE
from prophet.providers import PublicAPI
from prophet.store import Store


async def verify(output):
    with tempfile.TemporaryDirectory(prefix="prophet-live-check-") as folder:
        store = Store(str(Path(folder) / "check.sqlite"))
        worker = Collector(store)
        report = dict(
            checked_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            scope="PUBLIC READ-ONLY; no order endpoints",
            providers={},
        )
        async with httpx.AsyncClient(
            timeout=20, headers={"User-Agent": "BTCProphetResearch/0.2"}
        ) as client:
            worker.api = PublicAPI(client)
            for name, method, key in [
                ("reference", worker.reference, "reference"),
                ("market", worker.market, "book"),
                ("spot", worker.spot, "spot"),
            ]:
                try:
                    await method()
                    value = store.get(key)
                    if not value:
                        raise ValueError("No validated observation returned")
                    report["providers"][name] = dict(
                        ok=True,
                        source_timestamp=value["timestamp"],
                        age_seconds=time.time() - value["timestamp"],
                        slug=value.get("slug"),
                        source=value.get("source"),
                        observed_at=time.time(),
                    )
                except Exception as exc:
                    report["providers"][name] = dict(ok=False, error=f"{type(exc).__name__}: {exc}")
        ref, market = (report["providers"][name] for name in ("reference", "market"))
        now = time.time()
        report["simulation_ready_now"] = (
            ref.get("ok") and market.get("ok")
            and 0 <= now-ref["source_timestamp"] <= REFERENCE_MAX_AGE
            and market["source_timestamp"] <= now
            and 0 <= now-market["observed_at"] <= BOOK_CONFIRM_MAX_AGE
        )
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="data/live-check.json")
    asyncio.run(verify(parser.parse_args().output))
