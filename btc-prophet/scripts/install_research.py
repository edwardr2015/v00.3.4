"""Install bundled research without overwriting existing histories or model records."""

import hashlib
import json
from pathlib import Path
import shutil
from prophet.store import Store
from prophet.models import Models
from prophet.v2paper import Engine


def install(source="research", destination="data"):
    source, destination = Path(source), Path(destination)
    if not source.is_dir():
        return
    destination.mkdir(parents=True, exist_ok=True)
    history = source / "history.sqlite3"
    if history.is_file() and not (destination / "history.sqlite3").exists():
        shutil.copy2(history, destination / "history.sqlite3")
    cache = source / "history-cache"
    if cache.is_dir():
        target_cache = destination / "history-cache"
        target_cache.mkdir(exist_ok=True)
        for file in cache.iterdir():
            if file.is_file() and not (target_cache / file.name).exists():
                shutil.copy2(file, target_cache / file.name)
    store = Store(str(destination / "prophet.sqlite3"))
    Engine(store)
    models = Models(store)
    registry = source / "model-registry.json"
    if registry.is_file():
        for r in json.loads(registry.read_text()):
            name = r["filename"]
            if Path(name).name != name:
                raise ValueError("Invalid bundled artifact name")
            raw = (source / "models" / name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != r["sha256"]:
                raise ValueError("Bundled model checksum failure")
            target = models.root / name
            if target.exists() and target.read_bytes() != raw:
                continue
            target.write_bytes(raw)
            with store.connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO model_registry VALUES (?,?,?,?,?,?,?)",
                    (
                        r["version"],
                        r["kind"],
                        r["trained"],
                        r["dataset"],
                        str(target.resolve()),
                        r["sha256"],
                        r["metadata"],
                    ),
                )
    verified = source / "verified-markets.json"
    if verified.is_file():
        from prophet.analytics import record_market

        for r in json.loads(verified.read_text()):
            record_market(store, json.loads(r["evidence"]), r["start"], r["end"])
    with store.connect() as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Research import integrity failure")
    print(
        "Research installed; existing balances and database records preserved. Decisions remain OFF on first install."
    )


if __name__ == "__main__":
    install()
