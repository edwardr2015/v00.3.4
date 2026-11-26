"""Bounded free-account cohort; no plan changes or purchases, stop on quota errors."""

import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prophet.depth import import_file  # noqa: E402

root = Path(__file__).resolve().parents[1]
key = json.loads((root / "data/pmdata-access.json").read_text())["api_key"]
folder = root / "data/depth-samples"
folder.mkdir(exist_ok=True)
db = sqlite3.connect(root / "data/history.sqlite3")
db.row_factory = sqlite3.Row
markets = db.execute(
    "SELECT slug,start FROM markets WHERE start>=? AND end<=? ORDER BY start LIMIT 48",
    (1794700800, 1795305600),
).fetchall()
manifest = []
for i, m in enumerate(markets):
    date = datetime.fromtimestamp(m["start"], timezone.utc).strftime("%Y/%m/%d")
    url = f"https://api.pmdata.dev/v1/polymarket_us/l2/{date}/{m['slug']}.parquet"
    dest = folder / (m["slug"] + ".parquet")
    if not dest.exists():
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers={"api_key": key}), timeout=30
            ) as r:
                raw = r.read(32 * 1024 * 1024 + 1)
                if len(raw) > 32 * 1024 * 1024 or raw[:4] != b"PAR1":
                    raise ValueError("Invalid or oversized response")
                dest.write_bytes(raw)
        except urllib.error.HTTPError as exc:
            manifest.append(dict(slug=m["slug"], status=exc.code))
            if exc.code in (401, 402, 403, 429):
                print("Stopped at access/quota limit.", flush=True)
                break
            continue
        time.sleep(0.5)
    result = import_file(db, dest, m["slug"])
    result["url"] = url
    manifest.append(result)
    print(f"Validated causal entry book {i + 1}/{len(markets)}", flush=True)
(root / "data/depth-sample-manifest.json").write_text(json.dumps(manifest, indent=2))
db.close()
