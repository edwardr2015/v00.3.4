"""Run from the project root: python scripts/backup.py."""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prophet.store import Store  # noqa: E402

destination = Path("data/backups")
destination.mkdir(parents=True, exist_ok=True)
filename = destination / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".sqlite3")
Store("data/prophet.sqlite3").backup(str(filename))
print(filename.resolve())
