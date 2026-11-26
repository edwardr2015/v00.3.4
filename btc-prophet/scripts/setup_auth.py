"""Run locally. Never paste your dashboard password into chat."""

import getpass
import os
from pathlib import Path
from prophet.auth import configure

if __name__ == "__main__":
    password = getpass.getpass("Create dashboard admin password (12+ characters): ")
    if password != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords do not match; no change made")
    configure(Path(os.getenv("PROPHET_DB", "data/prophet.sqlite3")).parent / "auth.json", password)
    print("Private dashboard login created. Username: admin. Password remains on this computer.")
